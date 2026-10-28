# Derived from FunAudioLLM/SenseVoice (https://github.com/FunAudioLLM/SenseVoice),
# MIT License, Copyright (c) FunASR.
# Modified by MekaHime Inc.
import os
import re
import time
import logging
import warnings
from io import BytesIO
from enum import Enum
from typing import List, Optional

warnings.filterwarnings("ignore", message=".*ffmpeg.*")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
import soundfile as sf
import torchaudio
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing_extensions import Annotated

from devices import describe, resolve_device
from model import SenseVoiceSmall
from funasr.utils.postprocess_utils import rich_transcription_postprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TARGET_FS = 16000


class Language(str, Enum):
    auto = "auto"
    zh = "zh"
    en = "en"
    yue = "yue"
    ja = "ja"
    ko = "ko"
    nospeech = "nospeech"


if os.getenv("STT_THREADS", "").strip().isdigit() and int(os.environ["STT_THREADS"]) > 0:
    torch.set_num_threads(int(os.environ["STT_THREADS"]))
# STT_DEVICE: auto | cpu | cuda[:N] | xpu | mps
DEVICE, _device_warning = resolve_device(os.getenv("STT_DEVICE", "auto"), torch)
if _device_warning:
    logger.warning(_device_warning)
MODEL_DIR = os.getenv(
    "STT_MODEL",
    os.path.join(os.path.dirname(__file__), "models"),
)



def _load(device: str):
    model, kw = SenseVoiceSmall.from_pretrained(model=MODEL_DIR, device=device)
    model.eval()
    return model, kw


logger.info(f"Loading STT model from {MODEL_DIR} on {DEVICE} ...")
load_start = time.time()
try:
    m, kwargs = _load(DEVICE)
except Exception as _e:
    if DEVICE == "cpu":
        raise
    logger.warning(f"Loading on {DEVICE} failed ({_e}). Falling back to CPU")
    DEVICE = "cpu"
    m, kwargs = _load(DEVICE)
load_elapsed = time.time() - load_start
logger.info(f"Model loaded in {load_elapsed:.2f}s")

regex = r"<\|.*?\|>"

def _warm_up() -> None:
    with torch.no_grad():
        m.inference(data_in=[torch.zeros(16000, dtype=torch.float32)], language="auto", use_itn=True, ban_emo_unk=False, key=["warmup"], fs=16000, **kwargs)


try:
    _warm_start = time.time()
    _warm_up()
    logger.info(f"Warm-up inference done in {time.time() - _warm_start:.2f}s")
except Exception as _e:
    if DEVICE != "cpu":
        logger.warning(f"Warm-up on {DEVICE} failed ({_e}); falling back to CPU")
        DEVICE = "cpu"
        m, kwargs = _load(DEVICE)
        try:
            _warm_up()
        except Exception as _e2:
            logger.warning(f"Warm-up inference failed (continuing): {_e2}")
    else:
        logger.warning(f"Warm-up inference failed (continuing): {_e}")

app = FastAPI(
    title="MekaHime STT API (modified SenseVoiceSmall)",
    description="MekaHime-modified SenseVoiceSmall: multilingual ASR with language, emotion and audio-event tags.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SERVICE_TOKEN = os.getenv("SAKURA_SERVICE_TOKEN", "").strip()
if _SERVICE_TOKEN:
    import hmac
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in ("/", "/health"):
            return await call_next(request)
        sent = request.headers.get("x-api-key") or request.headers.get("authorization", "")
        if sent.lower().startswith("bearer "):
            sent = sent[7:]
        if not hmac.compare_digest(sent.strip().encode(), _SERVICE_TOKEN.encode()):
            return JSONResponse({"detail": "missing or invalid service token"}, status_code=401)
        return await call_next(request)


@app.get("/")
async def root():
    return {"service": "MekaHime STT (modified SenseVoiceSmall)", "docs": "/docs", "asr": "POST /api/v1/asr"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_DIR,
        **describe(torch, DEVICE),     # device, backend (cuda|rocm|xpu|mps|cpu), gpu_name, torch
        "requested_device": os.getenv("STT_DEVICE", "auto"),
        "cuda_available": torch.cuda.is_available(),   # kept for older clients
    }


@app.post("/api/v1/asr")
async def asr(
    files: Annotated[List[UploadFile], File(description="audio files (wav/mp3/flac/ogg)")],
    keys: Annotated[Optional[str], Form(description="comma-separated keys, one per file")] = None,
    lang: Annotated[Language, Form(description="auto | zh | en | yue | ja | ko")] = "auto",
    use_itn: Annotated[bool, Form(description="inverse text normalization (punctuation, numbers)")] = True,
):
    if not files:
        raise HTTPException(status_code=400, detail="no audio file provided")

    request_start = time.time()
    audios = []
    audio_lengths = []

    for file in files:
        try:
            file_bytes = await file.read()
            file_io = BytesIO(file_bytes)
            data_np, audio_fs = sf.read(file_io, dtype="float32")
            audio_tensor = torch.from_numpy(data_np)
            if audio_tensor.dim() == 1:
                audio_tensor = audio_tensor.unsqueeze(0)
            else:
                audio_tensor = audio_tensor.T

            if audio_fs != TARGET_FS:
                resampler = torchaudio.transforms.Resample(orig_freq=audio_fs, new_freq=TARGET_FS)
                audio_tensor = resampler(audio_tensor)

            audio_tensor = audio_tensor.mean(0)
            audios.append(audio_tensor)
            audio_lengths.append(len(audio_tensor) / TARGET_FS)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"could not decode {file.filename}: {str(e)}")

    if lang == "":
        lang = "auto"

    if not keys:
        key = [f.filename for f in files]
    else:
        key = keys.split(",")

    inference_start = time.time()
    try:
        with torch.no_grad(): # no need autograd graph we just do inference
            res = m.inference(
                data_in=audios,
                language=lang,
                use_itn=use_itn,
                ban_emo_unk=False,
                key=key,
                fs=TARGET_FS,
                **kwargs,
            )
    except Exception as e:
        logger.error(f"Inference failed: {e}")
        raise HTTPException(status_code=500, detail=f"inference failed: {str(e)}")

    inference_elapsed = time.time() - inference_start
    total_elapsed = time.time() - request_start

    if len(res) == 0:
        return {"result": [], "meta": {"inference_time": inference_elapsed, "total_time": total_elapsed}}

    for it in res[0]:
        it["raw_text"] = it["text"]
        it["clean_text"] = re.sub(regex, "", it["text"], 0, re.MULTILINE)
        it["text"] = rich_transcription_postprocess(it["text"])

    logger.info(
        f"ASR completed: {len(files)} file(s), "
        f"total_audio={sum(audio_lengths):.1f}s, "
        f"inference={inference_elapsed:.3f}s, "
        f"total={total_elapsed:.3f}s"
    )

    return {
        "result": res[0],
        "meta": {
            "inference_time": round(inference_elapsed, 3),
            "total_time": round(total_elapsed, 3),
            "audio_duration": round(sum(audio_lengths), 2),
        },
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("STT_HOST", "127.0.0.1")
    port = int(os.getenv("STT_PORT", "50000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not _SERVICE_TOKEN:
        logger.warning(f"listening on {host} with NO SAKURA_SERVICE_TOKEN: anyone who can reach this port can use the service")
    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
