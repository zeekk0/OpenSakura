"""Client for the STT service (MekaHime-modified SenseVoiceSmall).

The service is the standalone FastAPI app under ``services/stt``:

    GET  /health                       -> {"status": "ok", ...}
    POST /api/v1/asr  (multipart)      files=<audio>, lang=auto|zh|en|yue|ja|ko, use_itn=true
         -> {"result": [{"key", "text", "clean_text", "raw_text", ...}], ...}

``raw_text`` carries SenseVoice's rich tags (<|zh|><|HAPPY|><|Speech|>...)
which we surface as ``lang`` / ``emotion`` / ``events`` hints.
"""
from __future__ import annotations

import re
from typing import Optional

import http_clients
from settings import settings

_TAG = re.compile(r"<\|([^|>]+)\|>")
_LANGS = {"zh", "en", "yue", "ja", "ko", "nospeech"}
_EMOS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED", "EMO_UNKNOWN"}


def _auth() -> dict:
    tok = settings.secret("SAKURA_SERVICE_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _base() -> str:
    return str(settings.get("stt.url", "http://127.0.0.1:50000")).rstrip("/")


def parse_rich_tags(raw: str) -> dict:
    tags = _TAG.findall(raw or "")
    lang = next((t for t in tags if t in _LANGS), None)
    emo = next((t for t in tags if t in _EMOS), None)
    events = [t for t in tags if t not in _LANGS and t not in _EMOS and t not in ("withitn", "woitn")]
    return {"lang": lang, "emotion": (emo or "").lower() or None, "events": events}


async def transcribe(audio_bytes: bytes, filename: str = "utterance.wav",
                     lang: Optional[str] = None, use_itn: bool = True, timeout: float = 30.0) -> dict:
    lang = lang or str(settings.get("stt.lang", "auto"))
    files = {"files": (filename, audio_bytes, "audio/wav")}
    data = {"lang": lang, "use_itn": "true" if use_itn else "false", "keys": "utterance"}
    url = f"{_base()}/api/v1/asr"
    r = await http_clients.client(url).post(url, files=files, data=data, headers=_auth(), timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("result") or []
    if not results:
        return {"text": "", "raw_text": "", "lang": None, "emotion": None, "events": [], "meta": payload.get("meta", {})}
    first = results[0]
    raw = first.get("raw_text") or ""
    text = (first.get("clean_text") or first.get("text") or "").strip()
    out = {"text": text, "raw_text": raw, "meta": payload.get("meta", {})}
    out.update(parse_rich_tags(raw))
    return out


async def health(timeout: float = 4.0) -> dict:
    base = _base()
    try:
        r = await http_clients.client(base).get(f"{base}/health", timeout=timeout)
        ok = r.status_code == 200
        body = r.json() if ok else {}
        return {"ok": ok and body.get("status", "ok") == "ok", "url": base, "info": body}
    except Exception as e:
        return {"ok": False, "url": base, "error": str(e)[:200]}
