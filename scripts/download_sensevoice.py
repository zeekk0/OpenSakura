#!/usr/bin/env python3
"""Download the SenseVoiceSmall weights the STT service needs (model.pt, ~936 MB).

The small companion files (config.yaml, am.mvn, tokens.json, bpe model) are
committed; only model.pt is fetched. Source: Hugging Face mirror of the
official FunAudioLLM release. ModelScope works too (see README).
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

# HF_ENDPOINT (e.g. https://hf-mirror.com) switches the download host for networks where huggingface.co is slow or blocked
URL = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/") + "/FunAudioLLM/SenseVoiceSmall/resolve/main/model.pt"
DEST = Path(__file__).resolve().parent.parent / "services" / "stt" / "models" / "model.pt"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # messages can be non-ASCII; never die on a cp1252 console or a redirected log file
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    if DEST.exists() and DEST.stat().st_size > 800_000_000:
        print(f"already present: {DEST} ({DEST.stat().st_size/1e6:.0f} MB)")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    part = DEST.with_suffix(".pt.part")
    print(f"downloading {URL}\n      → {DEST}")

    def hook(blocks: int, bs: int, total: int) -> None:
        done = blocks * bs
        if total > 0:
            pct = min(100, done * 100 // total)
            sys.stdout.write(f"\r  {done/1e6:7.0f} / {total/1e6:.0f} MB  {pct:3d}%")
            sys.stdout.flush()

    urllib.request.urlretrieve(URL, part, hook)
    part.replace(DEST)
    print(f"\ndone: {DEST.stat().st_size/1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
