#!/usr/bin/env python3
# OpenSakura voice server: a MekaHime build of GPT-SoVITS.
#
# Copyright (c) 2026 MekaHime Inc.
# SPDX-License-Identifier: MIT  (full text in LICENSE next to this file)
#
# Runs on an unmodified upstream GPT-SoVITS checkout (MIT, RVC-Boss) that
# scripts/setup_tts.py downloads at a pinned commit; see THIRD_PARTY_NOTICES.md.
"""OpenSakura voice server, a MekaHime build of GPT-SoVITS (v2ProPlus).

Serves a speaker from ``Speakers/<character_id>/`` over the Socket.IO + HTTP contract
that ``sakura/tts_client.py`` speaks:

    emit  tts_request_gateway {task_id, turn_id, text: "【happy2】Hello!", character_id, language, audio_format}
    on    task_queued         {task_id, turn_id, sentence_count, queue_position}
    on    audio_ready         {task_id, turn_id, audio: <bytes>, audio_format, audio_duration, sample_rate,
                               is_final, text, character_id, language, emotions, elapsed_ms}
    on    error               {task_id, turn_id, error}
    GET   /ping -> "ok" (503 "loading" while the model loads)
    GET   /api/characters -> {"characters": {...}, "current": id}
    GET   /api/health -> device, model, VRAM, queue, timings

Text may carry several emotion tags (``【neutral1】Well. 【happy1】Hi!``); each run is spoken with the
reference clip of its tag and the pieces are joined into one audio. A tag without a recording falls
back to ``neutral1``. A run of only punctuation or symbols ("……", "~") is left out, and a text with
nothing to speak at all (only tags or emoji) gets 0.3 s of silence and a ``warning``. An English run that
contains kana, Hangul or Chinese characters is read in upstream's "auto" mode, so those words are spoken
(see NON_LATIN_RE). Inside a run, each sentence is its own fragment for the GPT stage: the fragments are
decoded together and each is checked for skipped words (see TEXT_SPLITS and the dropout guard).
A non-empty ``turn_id`` (turn mode) returns one audio for the whole text;
without it every sentence is sent as its own ``audio_ready`` and the last one has ``is_final``.
Audio over 3 MiB is sent as consecutive byte chunks of one file (``is_final`` on the last chunk).
Requests are spoken one at a time, in the order they arrive; a request's sentences are never
interleaved with another request's. When a client disconnects, its requests that have not started
(and the sentences not yet spoken) are dropped.

    python main.py --port 8002 --host 127.0.0.1 [--device auto|cuda|cuda:1|xpu|cpu|mps] [--no-half]
"""
from __future__ import annotations

import __future__
import argparse
import asyncio
import copy
import gc
import hashlib
import hmac
import inspect
import io
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import uuid
import wave
import warnings
from collections import OrderedDict
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ENGINE_DIR = Path(__file__).resolve().parent
SERVER_NAME = "OpenSakura voice server (a MekaHime build of GPT-SoVITS)"
SERVER_VERSION = "1.0.0"
IS_WIN = os.name == "nt"

LOG = logging.getLogger("voice")

# language names the client sends -> GPT-SoVITS language codes
LANGUAGES = {
    "english": "en", "en": "en",
    "chinese": "zh", "mandarin": "zh", "zh": "zh",
    "japanese": "ja", "ja": "ja", "jp": "ja",
    "korean": "ko", "ko": "ko",
    "cantonese": "yue", "yue": "yue",
    "auto": "auto", "multilingual": "auto", "mixed": "auto", "auto_yue": "auto_yue",
    "all_zh": "all_zh", "all_ja": "all_ja", "all_ko": "all_ko", "all_yue": "all_yue",
}
# Upstream's English front-end reads Latin letters only: a word in kana, Hangul or Chinese characters becomes one
# unknown phone, so it is not spoken. Sakura's emotion mapper writes her name, "Master", the family words and the
# -san / -chan honorifics in kana and "haha" as 哈 inside English text, so an English run that contains any of those
# scripts goes through upstream's "auto" mode instead, which splits it by language and reads each part with its own
# front-end (Latin text stays English there). Pure-Latin English runs keep the "en" front-end.
NON_LATIN_RE = re.compile("[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9d"   # kana
                          "\u1100-\u11ff\u3130-\u318f\uac00-\ud7af"                             # Hangul
                          "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")                           # CJK ideographs
SPEAKABLE_RE = re.compile(r"[^\W_]")   # a letter or digit in any script; upstream skips text that has none
SILENT_REPLY_S = 0.3                   # audio sent for a text with nothing to speak (only tags, punctuation, emoji)
DEFAULT_TAG = "neutral1"
TAG_RE = re.compile(r"【\s*([^】]*?)\s*】")
SENTENCE_END = set("。！？!?…")
CLOSERS = set("\"'”’）)」』】")
SMALL_KANA_RE = re.compile(r"[\u3041\u3043\u3045\u3047\u3049\u3083\u3085\u3087\u308e"
                           r"\u30a1\u30a3\u30a5\u30a7\u30a9\u30e3\u30e5\u30e7\u30ee]")
# emoji and pictographs: the text front-ends turn them into noise
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]")
# upstream prints "prompt\ttext\tgpt\tsovits" seconds after each non-streaming run
STAGES_RE = re.compile(r"^(\d+\.\d+)\t(\d+\.\d+)\t(\d+\.\d+)\t(\d+\.\d+)\s*$", re.M)
CHUNK_BYTES = 3 * 1024 * 1024        # the python-socketio client accepts 4 MiB websocket frames
REF_MIN_S, REF_MAX_S = 3.0, 10.0     # upstream rejects reference clips outside 3-10 s
SPEED_RANGE = (0.5, 2.0)             # speed_factor accepted from the command line and from requests
# sampling options a request may send: (whole number?, lowest, highest, lowest allowed?). Upstream does not check
# them: temperature 0 or a seed outside 0..2**32-1 raises deep inside a synthesis.
OPTIONS = {
    "top_k": (True, 1, 100, True),
    "top_p": (False, 0.0, 1.0, False),
    "temperature": (False, 0.0, 2.0, False),
    "speed": (False, SPEED_RANGE[0], SPEED_RANGE[1], True),
    "seed": (True, -1, 2**32 - 1, True),            # -1: a new random seed per request
}
# Silence trimming, on 10 ms frames: a frame is silence when it is more than TRIM_REL_DB below the loudest frame of
# the clip or quieter than SILENCE_FLOOR_DB. The model's own silence sits at -87 dBFS and below; the softest onsets
# and word endings of her whispered lines stay within 45 dB of their loudest frame. KEEP_SILENCE_S is left on each side.
TRIM_REL_DB, SILENCE_FLOOR_DB, KEEP_SILENCE_S = 45.0, -80.0, 0.12
# Skipped sentences. The GPT (text-to-semantic) stage reads a fragment's text after the reference transcript and
# sometimes starts at the fragment's second sentence, as if the reference clip had already said the first one: with a
# whole turn as one fragment (upstream's cut1) speech recognition found a phrase missing in 18 of 125 takes of 25
# multi-sentence replies, three replies lost their opening sentence in 5 of 5 takes, and no sampling setting (nor
# upstream's plain, non-CUDA-graph GPT loop) helped. So each sentence is its own fragment (TEXT_SPLITS "sentence", the
# default): upstream decodes a run's fragments together (--batch-size) and the dropout guard checks each one. That
# gave 0 of the same 125 takes, and 0 of 70 takes of 14 other replies (cut1: 8).
TEXT_SPLITS = ("sentence", "cut0", "cut1", "cut2", "cut3", "cut4", "cut5")   # "sentence" + upstream's split methods
FRAGMENT_MIN_UNITS = 4.0      # a sentence under this many syllables ("okay!", "you're late.") joins the next one
# Upstream's plain GPT loop does not stop before 11 tokens ("at least 10 tokens, 0.4 s"); its CUDA-graph loop
# (GPT_SoVITS/Accel) only forbids EOS on the first token, and a sentence alone then sometimes ends after 1-2 tokens
# (0.08 s): "did something happen at work?" did in 3 of 4 takes. install_eos_floor() gives it the same floor.
MIN_GPT_TOKENS = 11
# A "." before every fragment: the GPT stage starts with a short pause, as it does for upstream's own short first
# sentences. Not before a 1-3 letter (or 1-2 character) head followed by a comma, such as "oh," or "嗯，", which the
# pause can swallow, nor before a fragment that is only such a word.
RISKY_HEAD_RE = re.compile(r"^[\s\"'“”‘’「」『』]*(?:[\u3040-\u30ff\u3400-\u9fff]{1,2}|[A-Za-z]{1,3})[,，、]")
TINY_RE = re.compile(r"^[\s\"'“”‘’「」『』]*(?:[\u3040-\u30ff\u3400-\u9fff]{1,2}|[A-Za-z]{1,3})[\s。！？!?.…,，、~]*$")
# Dropout guard: a sentence (with one of upstream's split methods, the whole run) is synthesized again when its speech
# (first to last frame within GUARD_REL_DB of the loudest) lasts under DROPOUT_FRACTION of the time its text takes at
# DROPOUT_RATE syllables per second (see speech_units). Measured over 143 natural takes of six English / Chinese
# lines: 1.9-6.4 syllables per second (medians 3.4 English, 4.9 Chinese); the short takes caught so far came out at
# 9-14. 4.4 / 0.55 puts the limit at 8.0 syllables per second.
GUARD_REL_DB = 40.0                  # stricter than trimming: low murmurs around a skipped sentence do not count
DROPOUT_RATE, DROPOUT_FRACTION = 4.4, 0.55
DROPOUT_MIN_UNITS = 6.0              # shorter texts: the estimate is too rough, so only a silent take counts as short
# A short text (1-5 syllables: "Bye now.", "Thank you!") sometimes came out with no speech at all: 7 of 100 takes of
# five such lines had 0.08 s, while every other take had 0.41 s or more. Such a take is synthesized again too.
SILENT_TAKE_S = 0.2
SEED_SPAN = 2 ** 32                  # upstream also seeds numpy, which takes 0 .. 2**32 - 1


# ----------------------------------------------------------------------------- CLI
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=SERVER_NAME)
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--device", default="auto", help="auto (cuda/rocm > xpu > mps > cpu), cuda (also AMD ROCm builds), cuda:N, xpu (Intel, experimental), cpu or mps")
    ap.add_argument("--half", action=argparse.BooleanOptionalAction, default=None,
                    help="fp16 inference (default: on for CUDA, off elsewhere)")
    ap.add_argument("--character", default=None, help="speaker to load at start (default: first in Speakers/)")
    ap.add_argument("--gpt-sovits-dir", type=Path, default=ENGINE_DIR / "GPT-SoVITS")
    ap.add_argument("--speakers-dir", type=Path, default=ENGINE_DIR / "Speakers")
    ap.add_argument("--cache-dir", type=Path, default=ENGINE_DIR / "cache")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg binary for Opus output (default: ffmpeg/ next to main.py, "
                                                     "then PATH); WAV output and speed changes do not need it")
    ap.add_argument("--text-split", default="sentence", choices=TEXT_SPLITS,
                    help="how a run of text is cut into GPT fragments: sentence (default: one per sentence, decoded "
                         "together, each checked by the dropout guard) or one of upstream's methods for the whole "
                         "run: cut0 (none), cut1 (every 4 phrases), cut2 (~50 chars), cut3/cut4 (per sentence), "
                         "cut5 (every punctuation mark)")
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument("--top-p", type=float, default=1.0)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--repetition-penalty", type=float, default=1.35)
    ap.add_argument("--speed", type=float, default=1.0,
                    help=f"speaking rate, {SPEED_RANGE[0]}-{SPEED_RANGE[1]} (applied inside the SoVITS decoder)")
    ap.add_argument("--fragment-interval", type=float, default=0.25,
                    help="pause between sentences (fragments), seconds")
    ap.add_argument("--batch-size", type=int, default=4,
                    help="fragments (sentences) the GPT stage decodes at once (default: 4; each slot holds ~50 MB of "
                         "VRAM)")
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--opus-bitrate", default="64k")
    ap.add_argument("--trim-silence", action=argparse.BooleanOptionalAction, default=True,
                    help=f"cut silence before and after the speech to {KEEP_SILENCE_S:.2f} s (default: on)")
    ap.add_argument("--dropout-guard", action=argparse.BooleanOptionalAction, default=True,
                    help="synthesize a run again when its audio is too short for its text, the sign of a skipped "
                         "sentence (default: on)")
    ap.add_argument("--dropout-retries", type=int, default=2,
                    help="at most this many extra takes of a run that came out too short (default: 2)")
    ap.add_argument("--no-cuda-graph", action="store_true", help="disable upstream's CUDA-graph AR acceleration")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--token", default=os.environ.get("SAKURA_SERVICE_TOKEN", ""),
                    help="shared secret (default: env SAKURA_SERVICE_TOKEN). When set, every route except / and /ping, and the "
                         "Socket.IO connection, needs 'Authorization: Bearer <token>' or 'X-Api-Key: <token>'. Use it whenever "
                         "--host is not 127.0.0.1 (see distributed/README.md)")
    ap.add_argument("--cors", action="store_true", help="allow browser pages from any origin (local testing)")
    ap.add_argument("--verbose", action="store_true", help="show upstream GPT-SoVITS console output")
    args = ap.parse_args(argv)
    for name in OPTIONS:
        try:
            setattr(args, name, check_option(name, getattr(args, name)))
        except ValueError as e:
            ap.error(f"--{name.replace('_', '-')}: {e}")
    if args.dropout_retries < 0:
        ap.error("--dropout-retries must be 0 or more")
    if args.batch_size < 1:
        ap.error("--batch-size must be 1 or more")
    return args


def check_option(name: str, value: Any) -> Any:
    """``value`` for the sampling option ``name`` (see OPTIONS), or ValueError naming the accepted range."""
    whole, lo, hi, lo_ok = OPTIONS[name]
    if name == "speed":
        what = f"{name} must be between {lo} and {hi}"
    elif whole:
        what = f"{name} must be a whole number from {lo} to {hi}" + (" (-1: random)" if name == "seed" else "")
    else:
        what = f"{name} must be above {lo:g} and at most {hi:g}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} (got {value!r})")
    if whole:
        if value != int(value):
            raise ValueError(f"{what} (got {value!r})")
        value = int(value)
    if not ((lo <= value) if lo_ok else (lo < value)) or value > hi:
        raise ValueError(f"{what} (got {value!r})")
    return value


def setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:  # upstream prints Chinese status lines; never die on a cp1252 console or log file
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    for noisy in ("aiohttp.access", "engineio", "socketio", "urllib3", "httpx", "numba", "matplotlib",
                  "jieba", "fsspec", "filelock", "h5py", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not verbose:
        warnings.filterwarnings("ignore", category=FutureWarning)
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        warnings.filterwarnings("ignore", message=".*weight_norm.*")


# ----------------------------------------------------------------------------- speakers
@dataclass
class Emotion:
    tag: str
    audio: Path
    text: str


@dataclass
class Speaker:
    id: str
    dir: Path
    name: str
    language: str
    gpt_path: Path
    sovits_path: Path
    emotions: dict[str, Emotion] = field(default_factory=dict)
    no_clip: list[str] = field(default_factory=list)     # tags listed in emotions.json whose clip is missing

    @property
    def prompt_lang(self) -> str:
        return LANGUAGES.get(self.language.strip().lower(), "zh")

    @classmethod
    def load(cls, d: Path) -> "Speaker":
        # character.json also carries free-text fields (description, default_text, ...). Only the fields below are
        # read; nothing else from a voice pack is ever served over HTTP.
        meta = json.loads((d / "character.json").read_text(encoding="utf-8-sig"))
        cid = str(meta.get("character_id") or d.name)
        gpt = d / str(meta.get("llm_model") or f"{cid}.ckpt")
        sovits = d / str(meta.get("vocoder_model") or f"{cid}.pth")
        for p in (gpt, sovits):
            if not p.is_file():
                raise FileNotFoundError(f"speaker {cid}: missing {p.name}")
        sp = cls(id=cid, dir=d, name=str(meta.get("name") or cid), language=str(meta.get("language") or "Chinese"),
                 gpt_path=gpt, sovits_path=sovits)
        ref_dir = d / "references"
        emo_file = ref_dir / "emotions.json"
        if emo_file.is_file():
            table = json.loads(emo_file.read_text(encoding="utf-8-sig"))
            for tag, entry in table.items():
                if tag.startswith("_") or not isinstance(entry, dict) or not entry.get("audio"):
                    continue
                audio = ref_dir / str(entry["audio"])
                if not audio.is_file():
                    LOG.warning("speaker %s: %s has no recording (%s is missing); the tag falls back to %s", cid, tag,
                                audio.name, DEFAULT_TAG)
                    sp.no_clip.append(tag.strip().lower())
                    continue
                sp.emotions[tag.strip().lower()] = Emotion(tag.strip().lower(), audio, str(entry.get("text") or ""))
        if not sp.emotions:
            raise FileNotFoundError(f"speaker {cid}: no usable reference clips in {emo_file}")
        return sp

    def resolve(self, tag: Optional[str]) -> tuple[Emotion, bool]:
        """The emotion for ``tag`` and whether it fell back (no recording for that tag)."""
        key = (tag or "").strip().lower()
        if key in self.emotions:
            return self.emotions[key], False
        fallback = self.emotions.get(DEFAULT_TAG) or next(iter(self.emotions.values()))
        return fallback, bool(key) and key != fallback.tag


def discover_speakers(root: Path) -> dict[str, Speaker]:
    out: dict[str, Speaker] = {}
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "character.json").is_file()):
        try:
            meta = json.loads((d / "character.json").read_text(encoding="utf-8-sig"))
            if meta.get("enabled", True) is False:
                continue
            sp = Speaker.load(d)
            out[sp.id] = sp
        except Exception as e:  # noqa: BLE001
            LOG.warning("skipping speaker folder %s: %s", d.name, e)
    return out


# ----------------------------------------------------------------------------- text
@dataclass
class Segment:
    tag: Optional[str]
    text: str


def clean_text(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def speakable(text: str) -> bool:
    """False for a run of only punctuation or symbols ("……", "~"). Upstream answers such a run with a 'Please enter
    valid text.' error or with a second of silence at 16 kHz (its model speaks at 32 kHz), so these runs are left out."""
    return SPEAKABLE_RE.search(text) is not None


def run_language(lang: str, text: str) -> str:
    """The upstream language mode for one run: 'en' becomes 'auto' when the run holds kana, Hangul or CJK characters
    (see NON_LATIN_RE); every other mode is used as asked."""
    return "auto" if lang == "en" and NON_LATIN_RE.search(text) else lang


def split_tagged(text: str) -> list[Segment]:
    """``【a】x 【b】y`` -> [Segment(a, x), Segment(b, y)]; text before the first tag has tag None."""
    segs: list[Segment] = []
    tag: Optional[str] = None
    pos = 0
    for m in TAG_RE.finditer(text):
        chunk = clean_text(text[pos:m.start()])
        if chunk:
            segs.append(Segment(tag, chunk))
        tag, pos = m.group(1), m.end()
    chunk = clean_text(text[pos:])
    if chunk:
        segs.append(Segment(tag, chunk))
    merged: list[Segment] = []
    for s in segs:  # same tag twice in a row -> one segment (one reference switch fewer)
        if merged and (merged[-1].tag or "") == (s.tag or ""):
            merged[-1].text = f"{merged[-1].text} {s.text}"
        else:
            merged.append(s)
    return merged


def split_sentences(text: str) -> list[str]:
    """Split at 。！？!?… and at '.' (not inside numbers like 3.5); keep the punctuation."""
    out: list[str] = []
    buf: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        buf.append(ch)
        end = ch in SENTENCE_END or (ch == "." and not (0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()))
        if end:
            while i + 1 < n and (text[i + 1] in SENTENCE_END or text[i + 1] == "." or text[i + 1] in CLOSERS):
                i += 1
                buf.append(text[i])
            out.append("".join(buf).strip())
            buf = []
        i += 1
    out.append("".join(buf).strip())
    merged: list[str] = []
    for part in out:
        if not part:
            continue
        if merged and not re.search(r"\w", part):   # a lone "…" or quote joins the sentence before it
            merged[-1] += part
        else:
            merged.append(part)
    return merged or [text]


def speech_units(text: str) -> float:
    """How long ``text`` takes to say, in syllables at the pace of DROPOUT_RATE: one per Chinese character (also kanji),
    vowel groups per Latin word, 0.5 per Japanese kana and 0.6 per Korean Hangul block (both are said faster per
    character; these two weights are estimates, not measured on her voice). Digits and other scripts are not
    counted, so the estimate only errs short, which keeps the dropout guard from firing."""
    n = float(len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)))     # CJK ideographs (ext. A + basic)
    # kana, without the small ones that only change the mora before them (small a i u e o, ya yu yo, wa)
    n += 0.5 * len(re.findall(r"[\u3041-\u3096\u30a1-\u30fa\u30fc]", SMALL_KANA_RE.sub("", text)))
    n += 0.6 * len(re.findall(r"[\uac00-\ud7a3]", text))
    for w in re.findall(r"[a-z']+", text.lower()):
        w = w.replace("'", "")
        k = len(re.findall(r"[aeiouy]+", w))
        if k > 1 and w.endswith("e") and not w.endswith(("le", "ee", "ye")):   # silent e: "nice", "were"
            k -= 1
        n += max(1, k) if w else 0
    return n


def fragments(text: str) -> list[str]:
    """``text`` cut into sentences (split_sentences) for the GPT stage; a sentence under FRAGMENT_MIN_UNITS syllables
    or without anything to speak joins the next one (at the end, the one before)."""
    out: list[str] = []
    pending = ""
    for s in split_sentences(text):
        cand = f"{pending} {s}" if pending else s
        if speech_units(cand) < FRAGMENT_MIN_UNITS or not speakable(cand):
            pending = cand
        else:
            out.append(cand)
            pending = ""
    if pending:
        if out:
            out[-1] = f"{out[-1]} {pending}"
        else:
            out.append(pending)
    return out


def lead_pause(text: str) -> str:
    """The text the GPT stage gets for a fragment: "." + text (see RISKY_HEAD_RE)."""
    s = text.lstrip()
    if s.startswith((".", "…", "。", ",", "，")) or RISKY_HEAD_RE.match(s) or TINY_RE.match(s):
        return text
    return "." + text


def guard_limit(units: float, speed: float) -> float:
    """Seconds of speech under which a text of ``units`` syllables counts as too short (see DROPOUT_RATE)."""
    want = DROPOUT_FRACTION * units / (DROPOUT_RATE * speed)
    if units < DROPOUT_MIN_UNITS:
        want = min(want, SILENT_TAKE_S / speed)
    return want


def language_code(name: Optional[str]) -> tuple[str, bool]:
    """('en', True) for 'English'; unknown names map to 'auto' (second item False)."""
    key = (name or "English").strip().lower().replace("-", "_")
    if key in LANGUAGES:
        return LANGUAGES[key], True
    short = key[:2]
    if short in LANGUAGES:   # "en_us", "zh_cn", ...
        return LANGUAGES[short], True
    return "auto", False


# ----------------------------------------------------------------------------- audio
def wav_bytes(pcm, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


def speech_bounds(pcm, sr: int, rel_db: float) -> Optional[tuple[int, int]]:
    """(first, last + 1) sample of the part between the first and the last 10 ms frame that is within ``rel_db`` of
    the loudest frame and not below SILENCE_FLOOR_DB; None if there is no such frame."""
    import numpy as np
    hop = max(1, int(sr * 0.01))
    n = len(pcm) // hop
    if n == 0:
        return None
    frames = pcm[: n * hop].astype(np.float32).reshape(n, hop) / 32768.0
    db = 10.0 * np.log10((frames * frames).mean(axis=1) + 1e-12)
    loud = np.nonzero(db >= max(SILENCE_FLOOR_DB, float(db.max()) - rel_db))[0]
    if loud.size == 0:
        return None
    return int(loud[0]) * hop, min(len(pcm), (int(loud[-1]) + 1) * hop)


def trim_silence(pcm, sr: int):
    """Cut the silence before and after the speech down to KEEP_SILENCE_S each. The model sometimes opens a line with
    up to 1.3 s of silence (and upstream ends every run with a pause), which the listener would wait through."""
    bounds = speech_bounds(pcm, sr, TRIM_REL_DB)
    if bounds is None:
        return pcm
    keep = int(KEEP_SILENCE_S * sr)
    return pcm[max(0, bounds[0] - keep): min(len(pcm), bounds[1] + keep)]


def resample(pcm, sr_from: int, sr_to: int):
    """int16 samples at sr_from -> sr_to (polyphase filter)."""
    import numpy as np
    from scipy.signal import resample_poly
    g = math.gcd(int(sr_from), int(sr_to))
    out = resample_poly(pcm.astype(np.float32), int(sr_to) // g, int(sr_from) // g)
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def find_ffmpeg(explicit: Optional[str]) -> Optional[str]:
    cands = [explicit] if explicit else []
    cands += [str(ENGINE_DIR / "ffmpeg" / ("ffmpeg.exe" if IS_WIN else "ffmpeg")), shutil.which("ffmpeg")]
    for c in cands:
        if c and Path(c).is_file():
            return str(c)
    return None


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def ffmpeg_has_libopus(ffmpeg: str) -> bool:
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, timeout=20,
                           creationflags=_no_window())
        return b"libopus" in p.stdout
    except Exception:  # noqa: BLE001
        return False


def opus_bytes(pcm, sr: int, ffmpeg: str, bitrate: str) -> bytes:
    """Ogg/Opus at 48 kHz mono through an ffmpeg subprocess."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(int(sr)), "-ac", "1",
           "-i", "pipe:0", "-c:a", "libopus", "-b:a", bitrate, "-ar", "48000", "-ac", "1", "-f", "ogg", "pipe:1"]
    p = subprocess.run(cmd, input=pcm.astype("<i2").tobytes(), capture_output=True, timeout=120,
                       creationflags=_no_window())
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", "replace").strip()[-300:])
    return p.stdout


# ----------------------------------------------------------------------------- upstream glue
def prepare_upstream(gsv_dir: Path) -> dict[str, str]:
    """Make the upstream checkout importable the way its own api_v2.py does it.

    Upstream resolves model paths relative to the checkout root, so the process works from there;
    every path this server uses is absolute. Returns notes about the compatibility shims applied.
    """
    notes: dict[str, str] = {}
    if not (gsv_dir / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py").is_file():
        raise FileNotFoundError(f"GPT-SoVITS checkout not found at {gsv_dir} (run scripts/setup_tts.py)")
    nltk_dir = ENGINE_DIR / "nltk_data"
    if nltk_dir.is_dir():
        os.environ["NLTK_DATA"] = os.pathsep.join(p for p in (str(nltk_dir), os.environ.get("NLTK_DATA", "")) if p)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")          # every model is local; never phone home mid-request
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    # Synthesis never runs the ffmpeg CLI at the pinned commit: speed_factor is applied inside the SoVITS decoder and
    # TTS.py's ffmpeg-based speed_change() is not called (VoiceEngine.load checks, see speed_uses_ffmpeg). The bundled
    # build goes on PATH for any upstream helper that runs "ffmpeg" by name.
    ff_dir = ENGINE_DIR / "ffmpeg"
    if ff_dir.is_dir():
        os.environ["PATH"] = str(ff_dir) + os.pathsep + os.environ.get("PATH", "")
    for p in (gsv_dir / "GPT_SoVITS", gsv_dir):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    os.chdir(gsv_dir)

    # jieba_fast has no wheels (it needs a C compiler); plain jieba has the same API and dictionary.
    try:
        import jieba_fast  # noqa: F401
        notes["jieba"] = "jieba_fast"
    except ImportError:
        import jieba
        import jieba.posseg
        sys.modules["jieba_fast"] = jieba
        sys.modules["jieba_fast.posseg"] = jieba.posseg
        notes["jieba"] = "jieba (as jieba_fast)"

    # torchaudio >= 2.9 decodes through torchcodec, which needs FFmpeg shared libraries. The reference
    # clips are plain WAV files, so read them with soundfile instead when torchcodec is not installed.
    import torchaudio
    try:
        import torchcodec  # noqa: F401
        notes["audio_loader"] = "torchcodec"
    except Exception:  # noqa: BLE001
        ver = tuple(int(x) for x in re.findall(r"\d+", torchaudio.__version__.split("+")[0])[:2])
        if ver >= (2, 9):
            import soundfile as sf
            import torch

            def _load(uri, frame_offset=0, num_frames=-1, normalize=True, channels_first=True,
                      format=None, buffer_size=4096, backend=None):  # noqa: A002, ARG001
                data, sr = sf.read(str(uri), dtype="float32", always_2d=True, start=int(frame_offset),
                                   frames=int(num_frames) if num_frames and num_frames > 0 else -1)
                data = data.T if channels_first else data
                return torch.from_numpy(data.copy()), sr

            torchaudio.load = _load
            notes["audio_loader"] = "soundfile"
        else:
            notes["audio_loader"] = "torchaudio"
    return notes


def speed_uses_ffmpeg(module: Any) -> bool:
    """True if upstream's TTS module calls its ffmpeg-based speed_change(). At the pinned commit the only call (in
    TTS.audio_postprocess) is commented out, so speed changes need no ffmpeg."""
    try:
        src = inspect.getsource(module)
    except (OSError, TypeError):
        return False
    return re.search(r"^(?![ \t]*(?:#|def\b))[^#\n]*\bspeed_change\(", src, re.M) is not None


def install_eos_floor(min_tokens: int) -> str:
    """Give upstream's CUDA-graph GPT loop the EOS floor of its plain loop (see MIN_GPT_TOKENS). Its
    ModelRunner.decode_step forbids EOS while a sequence has 0 tokens; the same method, compiled from its own source
    with "< min_tokens" in that one comparison, replaces it (the files on disk are not changed, and tracebacks keep
    upstream's line numbers). Upstream is left as it is when that line is not found (another upstream version).
    Returns a note for /api/health."""
    min_tokens = int(min_tokens)
    try:
        from GPT_SoVITS.Accel.PyTorch.AR import runner
        current = runner.ModelRunner.decode_step
        if getattr(current, "_eos_floor", None) is not None:
            return f"{current._eos_floor} tokens"
        lines, first = inspect.getsourcelines(current)
    except Exception as e:  # noqa: BLE001  (no CUDA-graph loop in this upstream, or no source)
        return f"not installed ({type(e).__name__})"
    src = textwrap.dedent("".join(lines))
    old = "first_step_mask = (self.step_buf[idx_tensor] == 0).bool()"
    if src.count(old) != 1:
        LOG.warning("upstream's CUDA-graph GPT loop is not the expected one; a short sentence may end after 1-2 tokens")
        return "not installed (decode_step changed)"
    src = "\n" * (first - 1) + src.replace(old, f"first_step_mask = (self.step_buf[idx_tensor] < {min_tokens}).bool()")
    ns = dict(vars(runner))
    exec(compile(src, runner.__file__, "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True), ns)
    patched = ns["decode_step"]
    patched.__qualname__, patched._eos_floor = current.__qualname__, min_tokens
    runner.ModelRunner.decode_step = patched
    return f"{min_tokens} tokens"


def upstream_commit(gsv_dir: Path) -> str:
    """The checkout's commit: a detached HEAD, a branch ref (loose or packed), or the zip install's marker."""
    git_dir = gsv_dir / ".git"
    try:
        head = (git_dir / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            if (git_dir / ref).is_file():
                head = (git_dir / ref).read_text().strip()
            elif (git_dir / "packed-refs").is_file():
                for line in (git_dir / "packed-refs").read_text().splitlines():
                    if line.endswith(" " + ref):
                        head = line.split(" ", 1)[0]
                        break
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return head
    except OSError:
        pass
    marker = gsv_dir / ".opensakura-commit"
    return marker.read_text().strip() if marker.is_file() else "unknown"


def prepare_sovits(path: Path, cache_dir: Path) -> tuple[Path, str]:
    """Return a SoVITS weights file upstream loads as the right model version.

    Upstream GPT-SoVITS saves v2Pro / v2ProPlus fine-tunes with the first two bytes of the torch zip
    ("PK") replaced by a version marker ("05" / "06") and reads the version back from that marker.
    A file whose marker was turned back into "PK" still unpickles, but upstream then guesses the
    version from the file size and builds a plain v2 model: the speaker-embedding layers (sv_emb,
    ge_to512, prelu) are dropped and synthesis fails with a channel mismatch. For such a file, write a
    copy with the marker restored into the cache folder and load that.
    """
    from process_ckpt import get_sovits_version_from_path_fast, head2version, load_sovits_new, model_version2byte

    with open(path, "rb") as f:
        head = f.read(2)
    if head != b"PK":
        if head not in head2version:
            raise ValueError(f"{path.name}: unknown SoVITS header {head!r}")
        return path, head2version[head][1]
    guessed = get_sovits_version_from_path_fast(str(path))[1]
    ckpt = load_sovits_new(str(path))
    weights = ckpt.get("weight", {})
    if "sv_emb.weight" not in weights or "Pro" in guessed:
        return path, guessed
    try:
        upc = int(ckpt["config"]["model"]["upsample_initial_channel"])
    except Exception:  # noqa: BLE001
        upc = 768
    real = "v2ProPlus" if upc >= 768 else "v2Pro"
    del ckpt, weights
    cache_dir.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    marked = cache_dir / f"{path.stem}.{real}.pth"
    stamp = cache_dir / f"{path.stem}.{real}.source.json"
    want = {"source": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    try:
        if marked.is_file() and json.loads(stamp.read_text()) == want:
            return marked, real
    except (OSError, ValueError):
        pass
    LOG.info("%s has a plain 'PK' header; writing %s with the %s marker", path.name, marked.name, real)
    tmp = marked.with_suffix(".tmp")
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        src.seek(2)
        dst.write(model_version2byte[real])
        shutil.copyfileobj(src, dst, 8 << 20)
    os.replace(tmp, marked)
    stamp.write_text(json.dumps(want))
    return marked, real


def prepare_reference(emo: Emotion, cache_dir: Path) -> tuple[str, str]:
    """(audio path, prompt text) usable by upstream: clips under 3 s are padded with silence;
    clips over 10 s are trimmed and spoken without their transcript (it no longer matches)."""
    import numpy as np
    import soundfile as sf

    info = sf.info(str(emo.audio))
    dur = info.frames / float(info.samplerate)
    if REF_MIN_S <= dur <= REF_MAX_S:
        return str(emo.audio), emo.text
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{emo.audio}|{emo.audio.stat().st_size}|{emo.audio.stat().st_mtime_ns}".encode()).hexdigest()[:12]
    out = cache_dir / f"ref_{key}.wav"
    if not out.is_file():
        data, sr = sf.read(str(emo.audio), dtype="int16", always_2d=True)
        if dur < REF_MIN_S:
            pad = int(sr * (REF_MIN_S + 0.3)) - data.shape[0]
            data = np.concatenate([data, np.zeros((pad, data.shape[1]), dtype=np.int16)])
        else:
            data = data[: int(sr * (REF_MAX_S - 0.2))]
        sf.write(str(out), data, sr, subtype="PCM_16")
    if dur < REF_MIN_S:
        LOG.debug("reference %s is %.2f s; padded to %.1f s", emo.audio.name, dur, REF_MIN_S + 0.3)
        return str(out), emo.text
    LOG.warning("reference %s is %.1f s (> %.0f s); trimmed and used without its transcript",
                emo.audio.name, dur, REF_MAX_S)
    return str(out), ""


class StdoutRouter:
    """Stands in for sys.stdout so upstream's print() output can be captured per thread: on a thread inside a
    ``_Quiet`` block it goes to that block's buffer, everywhere else to the real stdout. install_stdout_router()
    puts it in place once, on the main thread, before upstream is imported; after that sys.stdout is never swapped
    again (contextlib.redirect_stdout would swap the process-wide sys.stdout, for every thread at once)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.local = threading.local()

    def target(self) -> Any:
        return getattr(self.local, "buf", None) or self.real

    def write(self, s: str) -> int:
        return self.target().write(s)

    def flush(self) -> None:
        self.target().flush()

    def isatty(self) -> bool:
        return self.target().isatty()

    def fileno(self) -> int:
        return self.target().fileno()

    def __getattr__(self, name: str) -> Any:   # encoding, errors, reconfigure, ...
        if name in ("real", "local"):           # not set yet (copy / pickle): no recursion through target()
            raise AttributeError(name)
        return getattr(self.target(), name)


_ROUTER: Optional[StdoutRouter] = None


def install_stdout_router() -> None:
    """Called by main() on the main thread before the worker thread starts and before upstream is imported."""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = StdoutRouter(sys.stdout)
        sys.stdout = _ROUTER


class _Quiet:
    """Capture upstream's print() chatter made on this thread inside the block, and keep it for the timing line and
    error reports. Lock-free: only a thread-local of the router changes. Without the router (--verbose, or this
    module used without main()) nothing is captured."""

    def __init__(self, verbose: bool) -> None:
        self.router = None if verbose else _ROUTER
        self.buf = io.StringIO()
        self._prev: Any = None

    def __enter__(self) -> "_Quiet":
        if self.router is not None:
            self._prev = getattr(self.router.local, "buf", None)
            self.router.local.buf = self.buf
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.router is not None:
            self.router.local.buf = self._prev

    def tail(self, n: int = 1200) -> str:
        return self.buf.getvalue()[-n:]


class SingleWorker(Executor):
    """One daemon worker thread for every model call (upstream's TTS object is not thread-safe).

    Same job as ThreadPoolExecutor(max_workers=1), but its thread is a daemon: on Ctrl-C the process exits
    at once instead of waiting at interpreter shutdown for a synthesis that may still run for many seconds."""

    def __init__(self, name: str = "voice") -> None:
        self._jobs: "queue.SimpleQueue[Optional[tuple]]" = queue.SimpleQueue()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable, /, *args: Any, **kwargs: Any) -> Future:  # type: ignore[override]
        if self._closed:
            raise RuntimeError("the voice worker has shut down")
        fut: Future = Future()
        self._jobs.put((fut, fn, args, kwargs))
        return fut

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fut, fn, args, kwargs = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001  (handed to the awaiting coroutine)
                fut.set_exception(e)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._closed = True
        if cancel_futures:
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    job[0].cancel()
        self._jobs.put(None)
        if wait:
            self._thread.join()


# ----------------------------------------------------------------------------- engine
class UnknownCharacter(ValueError):
    pass


class VoiceEngine:
    """Owns the upstream TTS pipeline. Every method runs on the single worker thread."""

    PROMPT_CACHE_SIZE = 96          # one entry per reference clip (~1.6 MB of VRAM each)

    def __init__(self, args: argparse.Namespace, speakers: dict[str, Speaker], ffmpeg: Optional[str]) -> None:
        self.args = args
        self.speakers = speakers
        self.ffmpeg = ffmpeg
        self.speed_ffmpeg = False       # upstream changes speed through the ffmpeg CLI (not at the pinned commit)
        self.tts = None
        self.speaker: Optional[Speaker] = None
        self.device = "cpu"
        self.half = False
        self.model_version = "?"
        self.notes: dict[str, str] = {}
        self.state = "starting"
        self.error = ""
        self.load_s = 0.0
        self.warmup_s = 0.0
        self.rocm = False
        self.requests = 0
        self.retries = 0                # runs the dropout guard synthesized a second time
        self.gpu = ""
        self._prompt_template: dict = {}
        self._fragments: Optional[list] = None   # the last run's fragments, from _audio_postprocess
        self._prompts: "OrderedDict[tuple, dict]" = OrderedDict()
        self._refs: dict[str, tuple[str, str]] = {}

    # ---- load ----
    def _pick_device(self) -> tuple[str, bool]:
        import torch
        want = (self.args.device or "auto").lower()
        mps = getattr(torch.backends, "mps", None)
        xpu = getattr(torch, "xpu", None)
        try:
            xpu_ok = bool(xpu and xpu.is_available())
        except Exception:  # noqa: BLE001
            xpu_ok = False
        if want == "auto":
            dev = ("cuda" if torch.cuda.is_available() else "xpu" if xpu_ok
                   else "mps" if (mps and mps.is_available()) else "cpu")
        elif want.startswith("cuda") and not torch.cuda.is_available():
            LOG.warning("CUDA requested but torch.cuda.is_available() is False; using CPU")
            dev = "cpu"
        elif want.startswith("xpu") and not xpu_ok:
            LOG.warning("XPU requested but not available; using CPU")
            dev = "cpu"
        elif want == "mps" and not (mps and mps.is_available()):
            LOG.warning("MPS requested but not available; using CPU")
            dev = "cpu"
        else:
            dev = want
        half = self.args.half if self.args.half is not None else dev.startswith("cuda")
        if dev == "cpu" and half:
            LOG.warning("fp16 is not supported on CPU; using fp32")
            half = False
        return dev, bool(half)

    def load(self, character: Optional[str]) -> None:
        t0 = time.perf_counter()
        self.state = "loading"
        self.notes = prepare_upstream(self.args.gpt_sovits_dir.resolve())
        import torch
        self.device, self.half = self._pick_device()
        self.rocm = bool(getattr(torch.version, "hip", None))
        if self.device.startswith("cuda"):
            idx = torch.device(self.device).index or 0
            self.gpu = torch.cuda.get_device_name(idx)
        elif self.device.startswith("xpu"):
            try:
                self.gpu = torch.xpu.get_device_name(0)
            except Exception:  # noqa: BLE001
                self.gpu = "Intel XPU"
        LOG.info("torch %s, device %s%s, %s", torch.__version__, self.device, f" ({self.gpu})" if self.gpu else "",
                 "fp16" if self.half else "fp32")
        with _Quiet(self.args.verbose) as q:
            try:
                from TTS_infer_pack.TTS import TTS, TTS_Config  # noqa: F401  (upstream)
            except Exception:
                LOG.error("importing upstream GPT-SoVITS failed:\n%s", q.tail())
                raise
        self._TTS, self._TTS_Config = TTS, TTS_Config
        tts_module = sys.modules.get(TTS.__module__)
        # upstream's plain GPT loop (CPU, MPS) has the floor already; importing its CUDA-graph package on those would
        # only change torch's global thread / precision settings
        self.notes["eos_floor"] = install_eos_floor(MIN_GPT_TOKENS) if (self.device.startswith("cuda") and not self.rocm) else "built in"
        self.speed_ffmpeg = speed_uses_ffmpeg(tts_module)
        if self.speed_ffmpeg:   # a different upstream than the pinned one; it looks for ffmpeg in its own runtime/
            if self.ffmpeg:
                tts_module.FFMPEG_EXE = Path(self.ffmpeg)
            LOG.warning("this GPT-SoVITS changes speed through ffmpeg; %s", f"using {self.ffmpeg}" if self.ffmpeg else
                        "ffmpeg not found, so requests are spoken at speed 1.0")
        sp = self.speakers.get(character) if character else next(iter(self.speakers.values()))
        if sp is None:
            raise UnknownCharacter(f"unknown character {character!r}; have {sorted(self.speakers)}")
        self._load_speaker(sp)
        self.load_s = time.perf_counter() - t0
        LOG.info("model ready in %.1f s: speaker %s (%s), GPT-SoVITS %s, %d emotion clips, audio loader %s, %s",
                 self.load_s, sp.id, self.model_version, upstream_commit(self.args.gpt_sovits_dir)[:10],
                 len(sp.emotions), self.notes.get("audio_loader"), self.notes.get("jieba"))

    def _load_speaker(self, sp: Speaker) -> None:
        gc.unfreeze()                  # let the previous speaker's objects be collected
        gsv = self.args.gpt_sovits_dir.resolve()
        pm = gsv / "GPT_SoVITS" / "pretrained_models"
        cache = self.args.cache_dir.resolve() / "speakers" / sp.id
        for need in (pm / "chinese-hubert-base" / "pytorch_model.bin",
                     pm / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin"):
            if not need.is_file():
                raise FileNotFoundError(f"missing {need} (run scripts/setup_tts.py)")
        t0 = time.perf_counter()
        sovits, version = prepare_sovits(sp.sovits_path, cache)
        if "Pro" in version and not (pm / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt").is_file():
            raise FileNotFoundError(f"missing {pm / 'sv'} speaker-verification model (run scripts/setup_tts.py)")
        with _Quiet(self.args.verbose) as q:
            try:
                if self.tts is None:
                    cfg = self._TTS_Config({"custom": {
                        "device": self.device, "is_half": self.half, "version": version,
                        "t2s_weights_path": str(sp.gpt_path), "vits_weights_path": str(sovits),
                        "bert_base_path": str(pm / "chinese-roberta-wwm-ext-large"),
                        "cnhuhbert_base_path": str(pm / "chinese-hubert-base"),
                    }})
                    self.args.cache_dir.resolve().mkdir(parents=True, exist_ok=True)
                    # upstream rewrites its config yaml on every weight change; keep the checkout clean
                    cfg.configs_path = str(self.args.cache_dir.resolve() / "tts_infer.yaml")
                    self.tts = self._TTS(cfg)
                    self._prompt_template = copy.deepcopy(self.tts.prompt_cache)
                else:
                    self.tts.init_t2s_weights(str(sp.gpt_path))
                    self.tts.init_vits_weights(str(sovits))
            except Exception:
                LOG.error("loading %s failed:\n%s", sp.id, q.tail())
                raise
        self.model_version = str(self.tts.configs.version)
        self.tts.empty_cache = self._empty_cache
        self.tts.audio_postprocess = self._audio_postprocess
        if "Pro" in version and not getattr(self.tts, "is_v2pro", False):
            raise RuntimeError(f"{sp.sovits_path.name} was loaded as {self.model_version}, expected {version}")
        self.speaker = sp
        self._prompts.clear()
        self._refs.clear()
        LOG.info("loaded speaker %s in %.1f s (%s)", sp.id, time.perf_counter() - t0, self.model_version)

    def _audio_postprocess(self, audio, sr, batch_index_list=None, speed_factor=1.0, split_bucket=True,
                           fragment_interval=0.3, super_sampling=False):
        """Upstream joins a run's fragments here. Keep each fragment's samples first (int16, in text order, as
        upstream converts them) for the per-fragment dropout guard, then let upstream do its work."""
        import numpy as np
        try:
            frags = []
            for batch in audio:
                row = []
                for f in batch:
                    x = f.detach().float()
                    m = float(x.abs().max()) if x.numel() else 0.0
                    x = x / m if m > 1 else x
                    row.append(np.clip((x * 32768).cpu().numpy(), -32768, 32767).astype(np.int16))
                frags.append(row)
            if split_bucket and batch_index_list is not None:
                flat: list = [None] * sum(len(b) for b in batch_index_list)
                for row, idxs in zip(frags, batch_index_list):
                    for f, i in zip(row, idxs):
                        flat[i] = f
            else:
                flat = [f for row in frags for f in row]
            self._fragments = flat if all(f is not None for f in flat) else None
        except Exception as e:  # noqa: BLE001  (the guard then checks the run as a whole)
            LOG.debug("could not keep the fragments: %s", e)
            self._fragments = None
        return type(self.tts).audio_postprocess(self.tts, audio, sr, batch_index_list, speed_factor, split_bucket,
                                                fragment_interval, super_sampling)

    def _empty_cache(self) -> None:
        """Replaces upstream's per-request gc.collect() + torch.cuda.empty_cache(): with the model objects
        frozen out of the collector (see freeze_heap) a collection is cheap, and cached CUDA blocks are only
        handed back after a request that left a lot of them (keeps re-allocation off the hot path)."""
        gc.collect()
        if self.device.startswith("cuda"):
            import torch
            dev = torch.device(self.device)
            if torch.cuda.memory_reserved(dev) - torch.cuda.memory_allocated(dev) > (768 << 20):
                torch.cuda.empty_cache()
        elif self.device == "mps":
            import torch
            torch.mps.empty_cache()

    @staticmethod
    def freeze_heap() -> None:
        """Move the ~1M objects created while loading models into the permanent generation, so the
        collections upstream triggers after every request stop rescanning them (0.2 s each)."""
        gc.collect()
        gc.freeze()

    def ensure_speaker(self, cid: Optional[str]) -> Speaker:
        assert self.speaker is not None
        if not cid or cid == self.speaker.id:
            return self.speaker
        sp = self.speakers.get(cid)
        if sp is None:
            raise UnknownCharacter(f"unknown character_id {cid!r} (available: {', '.join(sorted(self.speakers))})")
        LOG.info("switching speaker %s -> %s", self.speaker.id, cid)
        self._load_speaker(sp)
        self.freeze_heap()
        return sp

    # ---- synthesis ----
    def _reference(self, emo: Emotion) -> tuple[str, str]:
        hit = self._refs.get(emo.tag)
        if hit is None:
            hit = self._refs[emo.tag] = prepare_reference(emo, self.args.cache_dir.resolve() / "refs" / self.speaker.id)
        return hit

    def _activate_prompt(self, key: tuple) -> None:
        """Upstream keeps one prompt cache (reference + transcript features); keep one per reference
        so switching emotions does not redo HuBERT / BERT / G2PW work every request."""
        cache = self._prompts.get(key)
        if cache is None:
            cache = copy.deepcopy(self._prompt_template)
            self._prompts[key] = cache
            while len(self._prompts) > self.PROMPT_CACHE_SIZE:
                self._prompts.popitem(last=False)
        else:
            self._prompts.move_to_end(key)
        self.tts.prompt_cache = cache

    @property
    def output_sr(self) -> int:
        """The sample rate the loaded model speaks at (32 kHz for v2ProPlus), as upstream's run() computes it."""
        c = self.tts.configs
        return int(self.tts.vocoder_configs["sr"]) if getattr(c, "use_vocoder", False) else int(c.sampling_rate)

    def _run(self, inputs: dict) -> tuple[int, Any, list[float]]:
        """One upstream TTS.run(): (sample rate, int16 samples, [prompt, text, gpt, sovits] seconds or []).
        The samples are always at output_sr: when upstream finds nothing to speak it yields a second of zeros at
        16 kHz instead, which comes back as no samples at all."""
        import numpy as np
        self._fragments = None
        with _Quiet(self.args.verbose) as q:
            try:
                # run() yields once; on failure it yields a second of silence first and raises after,
                # so drain it to surface the exception
                results = list(self.tts.run(dict(inputs)))
            except Exception as e:
                LOG.error("synthesis failed (%s: %s)\n%s", type(e).__name__, e, q.tail())
                raise
        if not results:
            raise RuntimeError("GPT-SoVITS returned no audio")
        found = STAGES_RE.findall(q.buf.getvalue())
        stages = [float(x) for x in found[-1]] if found else []
        sr, pcm = results[-1]
        if not self.tts.configs.use_vocoder:   # raw reference audio is only read by v3/v4 vocoders
            self.tts.prompt_cache.pop("raw_audio", None)
        sr, pcm, want = int(sr), np.asarray(pcm, dtype=np.int16).reshape(-1), self.output_sr
        if sr != want:
            if not pcm.any():
                LOG.warning("GPT-SoVITS found nothing to speak in %r", inputs["text"][:80])
                pcm = pcm[:0]
            else:
                LOG.warning("GPT-SoVITS returned %d Hz audio for %r; resampled to %d Hz", sr, inputs["text"][:80], want)
                pcm = resample(pcm, sr, want)
        return want, pcm, stages

    @staticmethod
    def _speech_s(pcm, sr: int) -> float:
        """Seconds from the first to the last frame within GUARD_REL_DB of the loudest; 0 for an all-silent take."""
        b = speech_bounds(pcm, sr, GUARD_REL_DB)
        return 0.0 if b is None else (b[1] - b[0]) / float(sr)

    def synth(self, text: str, tag: Optional[str], lang: str, opts: Optional[dict] = None) -> dict:
        """Speak one tagged run of text.

        Returns {sr, pcm (int16 ndarray), tag, fallback, lang, synth_s, stages, retried, warning}."""
        assert self.tts is not None and self.speaker is not None
        opts = opts or {}
        emo, fell_back = self.speaker.resolve(tag)
        ref_path, prompt_text = self._reference(emo)
        self._activate_prompt((ref_path, prompt_text, self.speaker.prompt_lang))
        a = self.args
        issues: list[str] = []          # sent to the client as the audio's "warning"
        speed = float(opts.get("speed", a.speed))
        if speed != 1.0 and self.speed_ffmpeg and not self.ffmpeg:
            issues.append(f"speed {speed:g} needs ffmpeg with this GPT-SoVITS version and none was found; "
                          "spoke at speed 1.0")
            speed = 1.0
        lang = run_language(lang, text)
        by_sentence = a.text_split == "sentence"
        parts = fragments(text) if by_sentence else [text]
        said = [lead_pause(p) for p in parts] if by_sentence else parts
        inputs = {
            "text": "\n".join(said), "text_lang": lang,
            "ref_audio_path": ref_path, "aux_ref_audio_paths": [],
            "prompt_text": prompt_text, "prompt_lang": self.speaker.prompt_lang,
            "top_k": int(opts.get("top_k", a.top_k)), "top_p": float(opts.get("top_p", a.top_p)),
            "temperature": float(opts.get("temperature", a.temperature)),
            # sentence mode: one fragment per line (cut0 keeps upstream from cutting them again)
            "text_split_method": "cut0" if by_sentence else a.text_split, "batch_size": a.batch_size,
            "batch_threshold": 0.75, "split_bucket": True, "speed_factor": speed,
            "fragment_interval": a.fragment_interval, "seed": int(opts.get("seed", a.seed)),
            "parallel_infer": True, "repetition_penalty": a.repetition_penalty,
            "streaming_mode": False, "return_fragment": False,
            "use_cuda_graph": not a.no_cuda_graph and self.device.startswith("cuda") and not self.rocm,
            "use_flash_attention": True,
        }
        if self.device.startswith("cuda"):
            import torch
            # upstream's AR acceleration turns cuDNN autotuning on; every request has a new audio length,
            # so autotuning re-benchmarks the SoVITS convolutions each time (~0.3 s) instead of helping
            torch.backends.cudnn.benchmark = False
        t0 = time.perf_counter()
        sr, pcm, stages = self._run(inputs)
        frags, self._fragments = self._fragments, None
        retried = 0
        if a.dropout_guard and by_sentence and frags is not None and len(frags) == len(parts):
            pcm, retried, stages, short = self._guard_fragments(inputs, parts, said, frags, sr, speed, stages)
            if short:
                issues.append(f"speech came out shorter than the text needs in {len(short)} sentence"
                              f"{'s' if len(short) > 1 else ''} after {a.dropout_retries + 1} takes; words may be "
                              "missing")
        elif a.dropout_guard:
            pcm, retried, stages = self._guard_run(inputs, text, emo.tag, sr, pcm, speed, stages, issues)
        if a.trim_silence:
            pcm = trim_silence(pcm, sr)
        return {"sr": sr, "pcm": pcm, "tag": emo.tag, "fallback": fell_back, "lang": lang,
                "synth_s": time.perf_counter() - t0, "stages": stages, "retried": retried, "warning": "; ".join(issues)}

    def _guard_fragments(self, inputs: dict, parts: list[str], said: list[str], frags: list, sr: int, speed: float,
                         stages: list[float]) -> tuple[Any, int, list[float], list[str]]:
        """The dropout guard for each sentence: a fragment whose speech is too short for its sentence (guard_limit)
        is synthesized again on its own, up to --dropout-retries more times, and the take with the most speech is
        kept. The fragments are then joined as upstream joins them (fragment_interval of silence after each).
        Returns (pcm, extra takes, stages, sentences still short)."""
        import numpy as np
        a = self.args
        seed = int(inputs["seed"])
        gap = np.zeros(int(a.fragment_interval * sr), dtype=np.int16)
        frags = list(frags)
        retried, short = 0, []
        for k, part in enumerate(parts):
            units = speech_units(part)
            if units < 1:
                continue
            want = guard_limit(units, speed)
            got = first = self._speech_s(frags[k], sr)
            n = 0
            while got < want and n < a.dropout_retries:
                # the same batch size as the first take: another one makes upstream rebuild its GPT decoder
                n += 1
                one = dict(inputs, text=said[k], seed=seed if seed == -1 else (seed + 101 * (k + 1) + n) % SEED_SPAN)
                _, pcm2, stages2 = self._run(one)
                again, self._fragments = self._fragments, None
                if again:   # one sentence is one fragment, unless upstream had to cut an overlong one
                    f2 = again[0] if len(again) == 1 else np.concatenate([x for f in again for x in (f, gap)][:-1])
                else:       # nothing kept (upstream found nothing to speak): its audio without the closing pause
                    f2 = pcm2[: max(0, len(pcm2) - len(gap))]
                got2 = self._speech_s(f2, sr)
                stages = [x + y for x, y in zip(stages, stages2)] if stages and stages2 else []
                if got2 > got:
                    frags[k], got = f2, got2
            if n:
                retried += n
                self.retries += n
                LOG.warning("sentence %d/%d: %.2f s of speech for %.0f syllables (%.2f s expected at least): %s; "
                            "synthesized %d more time%s, kept a take with %.2f s%s: %r", k + 1, len(parts), first,
                            units, want, "likely skipped words" if units >= DROPOUT_MIN_UNITS else "a silent take", n,
                            "s" if n > 1 else "", got, ", still short" if got < want else "", part[:60])
                if got < want:
                    short.append(part)
        pcm = np.concatenate([x for f in frags for x in (f, gap)]) if frags else np.zeros(0, dtype=np.int16)
        return pcm, retried, stages, short

    def _guard_run(self, inputs: dict, text: str, tag: str, sr: int, pcm, speed: float, stages: list[float],
                   issues: list[str]) -> tuple[Any, int, list[float]]:
        """The dropout guard for a whole run (upstream text split methods): too little speech for the text, most
        likely a skipped sentence, gets up to --dropout-retries more takes (the last one cut at every punctuation
        mark) and the take with the most speech is kept."""
        a = self.args
        retried = 0
        units = speech_units(text)
        if units < 1:
            return pcm, retried, stages
        want = guard_limit(units, speed)
        got = first = self._speech_s(pcm, sr)
        seed = int(inputs["seed"])
        inputs = dict(inputs)
        while got < want and retried < a.dropout_retries:
            retried += 1
            inputs["seed"] = seed if seed == -1 else (seed + retried) % SEED_SPAN
            if retried > 1 and retried == a.dropout_retries:
                inputs["text_split_method"] = "cut5"
            sr2, pcm2, stages2 = self._run(inputs)
            got2 = self._speech_s(pcm2, sr2)
            stages = [x + y for x, y in zip(stages, stages2)] if stages and stages2 else []
            if got2 > got:
                pcm, got = pcm2, got2
        if retried:
            self.retries += retried
            LOG.warning("%s: %.2f s of speech for %.0f syllables (%.2f s expected at least): %s; synthesized %d "
                        "more time%s, kept a take with %.2f s%s", tag, first, units, want,
                        "likely a skipped sentence" if units >= DROPOUT_MIN_UNITS else "a silent take", retried,
                        "s" if retried > 1 else "", got, ", still short" if got < want else "")
            if got < want:
                issues.append(f"speech came out shorter than the text needs in {retried + 1} takes; "
                              "words may be missing")
        return pcm, retried, stages

    def warmup(self) -> None:
        t0 = time.perf_counter()
        # The third line is English with kana, as the emotion mapper writes it: it loads upstream's language splitter
        # and Japanese front-end now rather than in the first such request (on the first start after an install
        # the Japanese front-end also builds its user dictionary, about 1 s, unless setup_tts.py already did).
        lines = [("Hello! I'm awake now. It's so nice to see you.", "en"), ("你好呀，我准备好了。", "zh"),
                 ("Welcome home, ごしゅじんさま!", "en")]
        for text, lang in lines:
            r = self.synth(text, DEFAULT_TAG, lang)
            LOG.info("warm-up %s: %.2f s audio in %.2f s", r["lang"], len(r["pcm"]) / r["sr"], r["synth_s"])
        self.freeze_heap()
        self.warmup_s = time.perf_counter() - t0

    def vram(self) -> dict:
        # /api/health runs this on the event loop thread, so it must never import torch (seconds while the worker is
        # still loading). The device turns to cuda only after the worker has imported torch and picked the GPU.
        torch = sys.modules.get("torch")
        if torch is None or not self.device.startswith("cuda"):
            return {}
        try:
            dev = torch.device(self.device)
            return {"vram_allocated_mb": round(torch.cuda.memory_allocated(dev) / 2**20),
                    "vram_reserved_mb": round(torch.cuda.memory_reserved(dev) / 2**20),
                    "vram_peak_mb": round(torch.cuda.max_memory_allocated(dev) / 2**20)}
        except Exception:  # noqa: BLE001
            return {}


# ----------------------------------------------------------------------------- requests
@dataclass
class Job:
    task_id: str
    turn_id: Optional[str]
    character_id: Optional[str]
    language: str
    lang: str
    audio_format: str
    segments: list[Segment]          # the tagged runs that have something to speak
    opts: dict
    skipped: int = 0                 # runs left out because they hold only punctuation or symbols
    notes: list[str] = field(default_factory=list)   # request problems reported in the audio's "warning"
    received: float = field(default_factory=time.perf_counter)

    @property
    def turn_mode(self) -> bool:
        return bool(self.turn_id)

    def pieces(self) -> list[list[Segment]]:
        """Turn mode: one piece (every run). Sentence mode: one piece per sentence. A text with nothing to speak is
        one empty piece, answered with SILENT_REPLY_S of silence."""
        if self.turn_mode:
            out = [self.segments] if self.segments else []
        else:
            out = [[Segment(s.tag, sent)] for s in self.segments for sent in split_sentences(s.text) if speakable(sent)]
        return out or [[]]


def parse_job(data: Any) -> Job:
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    text = "" if data.get("text") is None else str(data["text"])
    if not text.strip():
        raise ValueError("empty text")
    segs = split_tagged(text)
    spoken = [s for s in segs if speakable(s.text)]
    lang, known = language_code(data.get("language"))
    if not known:
        LOG.warning("unknown language %r; using auto-detect", data.get("language"))
    notes: list[str] = []
    asked = str(data.get("audio_format") or "wav").strip().lower()
    fmt = {"ogg": "opus", "ogg_opus": "opus", "wave": "wav"}.get(asked, asked)
    if fmt not in ("wav", "opus"):
        notes.append(f"audio_format {asked!r} is not supported (wav or opus); sent WAV")
        fmt = "wav"
    opts = {k: check_option(k, data[k]) for k in OPTIONS if data.get(k) is not None}
    return Job(task_id=str(data.get("task_id") or uuid.uuid4()), turn_id=(str(data["turn_id"]) if data.get("turn_id") else None),
               character_id=(str(data["character_id"]) if data.get("character_id") else None),
               language=str(data.get("language") or "English"), lang=lang, audio_format=fmt, segments=spoken,
               opts=opts, skipped=len(segs) - len(spoken), notes=notes)


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.speakers = discover_speakers(args.speakers_dir.resolve())
        self.ffmpeg = find_ffmpeg(args.ffmpeg)
        self.opus_ok = bool(self.ffmpeg) and ffmpeg_has_libopus(self.ffmpeg)
        self.engine = VoiceEngine(args, self.speakers, self.ffmpeg)
        self.pool = SingleWorker("voice")
        self.job_lock = asyncio.Lock()   # one request at a time, so a request's sentences stay together
        self.pending = 0
        self.started = time.time()
        self.sio = None
        self._tasks: set[asyncio.Task] = set()
        self.clients: set[str] = set()   # connected Socket.IO sids; work for a client that left is dropped

    # ---- worker-thread side ----
    def _render(self, job: Job, piece: list[Segment]) -> dict:
        import numpy as np
        t0 = time.perf_counter()
        sp = self.engine.ensure_speaker(job.character_id)
        outs = [self.engine.synth(s.text, s.tag, job.lang, job.opts) for s in piece]
        sr = self.engine.output_sr      # every run comes back at this rate (see VoiceEngine._run)
        pcm = np.concatenate([o["pcm"] for o in outs]) if outs else np.zeros(0, dtype=np.int16)
        warns = list(job.notes) + list(dict.fromkeys(o["warning"] for o in outs if o["warning"]))
        if not len(pcm):   # only tags, punctuation or emoji (or nothing upstream could read): a short pause
            pcm = np.zeros(int(SILENT_REPLY_S * sr), dtype=np.int16)
            warns.append(f"nothing to speak in this text; sent {SILENT_REPLY_S:g} s of silence")
        langs = list(dict.fromkeys(o["lang"] for o in outs))
        synth_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        fmt = job.audio_format
        if fmt == "opus":
            if not self.opus_ok:
                fmt = "wav"
                warns.append("ffmpeg with libopus not found; sent WAV instead of Opus")
            else:
                try:
                    audio = opus_bytes(pcm, sr, self.ffmpeg, self.args.opus_bitrate)
                except Exception as e:  # noqa: BLE001
                    fmt = "wav"
                    warns.append(f"Opus encoding failed ({e}); sent WAV instead")
        if fmt == "wav":
            audio = wav_bytes(pcm, sr)
        self.engine.requests += 1
        stages = [sum(o["stages"][i] for o in outs) for i in range(4)] if outs and all(o["stages"] for o in outs) else []
        return {"audio": audio, "audio_format": fmt, "sr": sr, "duration": len(pcm) / float(sr), "stages": stages,
                "text": " ".join(s.text for s in piece), "emotions": [o["tag"] for o in outs],
                "fallback": [s.tag for s, o in zip(piece, outs) if o["fallback"]],
                "lang": job.lang if langs in ([], [job.lang]) else f"{job.lang}>{'+'.join(langs)}",
                "retried": sum(o["retried"] for o in outs), "character_id": sp.id,
                "synth_s": synth_s, "encode_s": time.perf_counter() - t1, "warning": "; ".join(warns)}

    # ---- event-loop side ----
    async def handle(self, sid: str, data: Any) -> None:
        loop = asyncio.get_running_loop()
        try:
            job = parse_job(data)
        except Exception as e:  # noqa: BLE001
            tid = str((data or {}).get("task_id", "")) if isinstance(data, dict) else ""
            await self.sio.emit("error", {"task_id": tid, "error": f"bad request: {e}"}, to=sid)
            return
        pieces = job.pieces()
        self.pending += 1
        await self.sio.emit("task_queued", {"task_id": job.task_id, "turn_id": job.turn_id,
                                            "sentence_count": len(pieces), "queue_position": self.pending - 1},
                            to=sid)
        try:
            if self.engine.state != "ready":
                await self._wait_ready()
            # Without the lock, two sentence-mode requests would alternate sentence by sentence on the one
            # worker (and reload the model every sentence if they asked for different speakers).
            async with self.job_lock:
                for i, piece in enumerate(pieces):
                    if sid not in self.clients:   # it disconnected (a client gives up after its timeout)
                        LOG.info("task %s: the client disconnected; %d of %d piece%s not synthesized",
                                 job.task_id[:8], len(pieces) - i, len(pieces), "s" if len(pieces) > 1 else "")
                        return
                    r = await loop.run_in_executor(self.pool, self._render, job, piece)
                    await self._send_audio(sid, job, r, i == len(pieces) - 1)
        except Exception as e:  # noqa: BLE001
            LOG.warning("task %s failed: %s: %s", job.task_id[:8], type(e).__name__, e)
            if self.args.verbose:
                traceback.print_exc()
            await self.sio.emit("error", {"task_id": job.task_id, "turn_id": job.turn_id,
                                          "error": f"{type(e).__name__}: {e}"}, to=sid)
        finally:
            self.pending -= 1

    async def _wait_ready(self) -> None:
        while self.engine.state in ("starting", "loading", "warming"):
            await asyncio.sleep(0.2)
        if self.engine.state != "ready":
            raise RuntimeError(f"voice engine unavailable: {self.engine.error or self.engine.state}")

    async def _send_audio(self, sid: str, job: Job, r: dict, last_piece: bool) -> None:
        audio: bytes = r["audio"]
        chunks = [audio[i:i + CHUNK_BYTES] for i in range(0, len(audio), CHUNK_BYTES)] or [b""]
        total_ms = int((time.perf_counter() - job.received) * 1000)
        base = {"task_id": job.task_id, "turn_id": job.turn_id, "audio_format": r["audio_format"],
                "audio_duration": round(r["duration"], 3), "sample_rate": 48000 if r["audio_format"] == "opus" else r["sr"],
                "text": r["text"], "character_id": r["character_id"],
                "language": job.language, "emotions": r["emotions"], "elapsed_ms": total_ms}
        if r["fallback"]:
            base["fallback_from"] = r["fallback"]
        if r["warning"]:
            base["warning"] = r["warning"]
        for i, chunk in enumerate(chunks):
            msg = dict(base, audio=chunk, is_final=last_piece and i == len(chunks) - 1,
                       chunk_index=i, chunk_count=len(chunks))
            await self.sio.emit("audio_ready", msg, to=sid)
        rtf = r["synth_s"] / r["duration"] if r["duration"] else 0.0
        st = r["stages"]
        detail = f" (ref {st[0]:.2f} + text {st[1]:.2f} + gpt {st[2]:.2f} + sovits {st[3]:.2f})" if st else ""
        LOG.info("task %s %s %s lang=%s chars=%d -> %.2f s audio, synth %.2f s%s RTF %.2f, %s %d KB, total %.2f s%s%s%s",
                 job.task_id[:8], "turn" if job.turn_mode else "sentence", "+".join(r["emotions"]) or "-", r["lang"],
                 len(r["text"]), r["duration"], r["synth_s"], detail, rtf, r["audio_format"], len(audio) // 1024,
                 total_ms / 1000.0, f" (re-synthesized {r['retried']}x)" if r["retried"] else "",
                 f" ({job.skipped} run{'s' if job.skipped > 1 else ''} without words left out)"
                 if job.skipped and last_piece else "", f" [{r['warning']}]" if r["warning"] else "")

    # ---- HTTP ----
    def characters(self) -> dict:
        cur = self.engine.speaker.id if self.engine.speaker else None
        return {"characters": {sp.id: {"name": sp.name, "language": sp.language,
                                       "emotions": sorted(sp.emotions), "default_emotion": DEFAULT_TAG,
                                       "no_clip": sorted(sp.no_clip), "loaded": sp.id == cur}
                               for sp in self.speakers.values()},
                "current": cur}

    def health(self) -> dict:
        e = self.engine
        return {"ok": e.state == "ready", "status": e.state, "error": e.error, "server": SERVER_NAME,
                "version": SERVER_VERSION, "gpt_sovits_commit": upstream_commit(self.args.gpt_sovits_dir.resolve()),
                "device": e.device, "gpu": e.gpu, "half": e.half, "model_version": e.model_version,
                "character": e.speaker.id if e.speaker else None, "load_s": round(e.load_s, 2),
                "warmup_s": round(e.warmup_s, 2), "uptime_s": round(time.time() - self.started),
                "requests": e.requests, "pending": self.pending, "ffmpeg": self.ffmpeg, "opus": self.opus_ok,
                "speed_needs_ffmpeg": e.speed_ffmpeg, "text_split": self.args.text_split,
                "batch_size": self.args.batch_size, "trim_silence": self.args.trim_silence,
                "dropout_guard": self.args.dropout_guard,
                "dropout_retries": self.args.dropout_retries, "retries": e.retries, **e.notes, **e.vram()}

    def _startup(self) -> None:
        e = self.engine
        try:
            e.load(self.args.character)
            if not self.args.no_warmup:
                e.state = "warming"
                e.warmup()
                LOG.info("warm-up done in %.1f s", e.warmup_s)
            else:
                e.freeze_heap()
            e.state = "ready"
            LOG.info("ready: %s", json.dumps({k: v for k, v in self.health().items()
                                               if k in ("device", "gpu", "half", "model_version", "character",
                                                        "vram_allocated_mb", "vram_reserved_mb", "opus")}))
        except Exception as ex:  # noqa: BLE001
            e.state, e.error = "error", f"{type(ex).__name__}: {ex}"
            LOG.error("start-up failed: %s\n%s", e.error, traceback.format_exc())

    async def run(self) -> int:
        import socketio
        from aiohttp import web

        if not self.speakers:
            LOG.error("no speakers in %s (each needs character.json, the .ckpt/.pth weights and "
                      "references/emotions.json; run scripts/setup_tts.py)", self.args.speakers_dir)
            return 2
        if self.args.character and self.args.character not in self.speakers:
            LOG.error("--character %s not found; have %s", self.args.character, ", ".join(self.speakers))
            return 2
        self.sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*" if self.args.cors else None,
                                        max_http_buffer_size=16 * 1024 * 1024, logger=False, engineio_logger=False)

        @self.sio.on("connect")
        async def _on_connect(sid: str, _environ: Any, _auth: Any = None) -> None:
            self.clients.add(sid)

        @self.sio.on("disconnect")
        async def _on_disconnect(sid: str, *_reason: Any) -> None:
            self.clients.discard(sid)

        @self.sio.on("tts_request_gateway")
        async def _on_request(sid: str, data: Any) -> None:
            task = asyncio.create_task(self.handle(sid, data))
            self._tasks.add(task)               # keep a reference until it finishes
            task.add_done_callback(self._tasks.discard)

        async def ping(_req: web.Request) -> web.Response:
            st = self.engine.state
            if st == "ready":
                return web.Response(text="ok")
            return web.Response(status=503 if st != "error" else 500, text=st if st != "error" else f"error: {self.engine.error}")

        async def characters(_req: web.Request) -> web.Response:
            return web.json_response(self.characters(), dumps=lambda o: json.dumps(o, ensure_ascii=False))

        async def health(_req: web.Request) -> web.Response:
            return web.json_response(self.health())

        async def index(_req: web.Request) -> web.Response:
            return web.Response(text=f"{SERVER_NAME} {SERVER_VERSION}: GET /ping, /api/characters, /api/health; "
                                     "Socket.IO event tts_request_gateway\n")

        token = (self.args.token or "").strip()

        @web.middleware
        async def require_token(request: web.Request, handler: Any) -> web.StreamResponse:
            if not token or request.method == "OPTIONS" or request.path in ("/", "/ping"):
                return await handler(request)
            sent = request.headers.get("X-Api-Key") or request.headers.get("Authorization", "")
            if sent.lower().startswith("bearer "):
                sent = sent[7:]
            if not hmac.compare_digest(sent.strip().encode(), token.encode()):
                raise web.HTTPUnauthorized(text="missing or invalid service token\n")
            return await handler(request)

        if not token and self.args.host not in ("127.0.0.1", "localhost", "::1"):
            LOG.warning("listening on %s with NO --token / SAKURA_SERVICE_TOKEN: anyone who can reach this port can use the voice server",
                        self.args.host)
        app = web.Application(middlewares=[require_token])
        self.sio.attach(app)
        app.router.add_get("/ping", ping)
        app.router.add_get("/api/characters", characters)
        app.router.add_get("/api/health", health)
        app.router.add_get("/", index)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.5)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.args.host, self.args.port).start()
        except OSError as e:
            LOG.error("cannot listen on %s:%d: %s", self.args.host, self.args.port, e)
            await runner.cleanup()
            return 1
        LOG.info("%s %s listening on http://%s:%d (speakers: %s)", SERVER_NAME, SERVER_VERSION, self.args.host,
                 self.args.port, ", ".join(self.speakers))
        try:
            await asyncio.get_running_loop().run_in_executor(self.pool, self._startup)
            if self.engine.state != "ready":
                return 1
            while True:
                await asyncio.sleep(3600)
        finally:   # also on Ctrl-C, which cancels this coroutine
            await self._close(runner)

    async def _close(self, runner: Any) -> None:
        """Disconnect the Socket.IO clients first: an open websocket (the backend keeps one) would otherwise hold
        aiohttp's shutdown for up to a minute. A synthesis in flight is not waited for (daemon worker thread)."""
        try:
            if self.sio.eio.sockets:
                await asyncio.wait_for(self.sio.eio.disconnect(), 2.0)   # every client, at the Engine.IO level
            await asyncio.wait_for(self.sio.shutdown(), 1.0)
        except Exception:  # noqa: BLE001
            pass
        await runner.cleanup()


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    for p in ("gpt_sovits_dir", "speakers_dir", "cache_dir"):
        setattr(args, p, Path(getattr(args, p)).expanduser().resolve())
    if not args.verbose:
        install_stdout_router()     # before upstream is imported, so anything that keeps a handle to stdout gets it
    server = Server(args)
    try:
        return asyncio.run(server.run())
    except KeyboardInterrupt:
        LOG.info("stopped")
        return 0
    finally:
        # queued requests are dropped; a synthesis already running is not waited for (daemon worker thread)
        server.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    raise SystemExit(main())
