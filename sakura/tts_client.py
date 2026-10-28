"""TTS engine client (Socket.IO).

Speaks the gateway contract every GPT-SoVITS-family server implements:

    emit  tts_request_gateway {task_id, turn_id, text:"【tag】...", character_id, language, audio_format}
    on    task_queued  {task_id, sentence_count}
    on    audio_ready  {task_id, audio:<bytes>, audio_format, audio_duration, is_final}
    on    error        {task_id, error}

A non-null ``turn_id`` asks for ONE unsplit response per request (turn mode),
which is what a chat reply wants. Adapted from the Discord-era client, which
used the sentence-stream path.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
import socketio

from settings import settings

log = logging.getLogger("sakura.tts")


class TTSError(Exception):
    pass


@dataclass
class TTSResult:
    audio: bytes
    audio_format: str
    duration: float
    text: str
    elapsed: float
    chunks: int = 1
    meta: dict = field(default_factory=dict)


class TTSClient:
    def __init__(self) -> None:
        self._sio: Optional[socketio.AsyncClient] = None
        self._url: str = ""
        self._connected = False
        self._pending: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ---- lifecycle ----
    @property
    def url(self) -> str:
        return str(settings.get("tts.url", "http://127.0.0.1:8002")).rstrip("/")

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        async with self._lock:
            url = self.url
            if self._sio is not None and self._url == url and self._connected:
                return True
            await self._disconnect_locked()
            sio = socketio.AsyncClient(reconnection=True, reconnection_attempts=0,
                                       reconnection_delay=2, reconnection_delay_max=10, logger=False)
            self._register(sio)
            try:
                await sio.connect(url, headers=_auth(), transports=["websocket"], wait_timeout=8)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS engine connect failed (%s): %s", url, e)
                self._sio, self._url, self._connected = sio, url, False
                return False
            self._sio, self._url, self._connected = sio, url, True
            log.info("TTS engine connected: %s", url)
            return True

    async def _disconnect_locked(self) -> None:
        if self._sio is not None:
            try:
                await self._sio.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._sio = None
        self._connected = False

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    def _register(self, sio: socketio.AsyncClient) -> None:
        @sio.on("connect")
        async def _on_connect() -> None:
            self._connected = True

        @sio.on("disconnect")
        async def _on_disconnect() -> None:
            self._connected = False
            log.warning("TTS engine disconnected (auto-reconnect)")

        @sio.on("task_queued")
        async def _on_queued(data: dict) -> None:
            t = self._pending.get(str(data.get("task_id", "")))
            if t is not None:
                t["queued"] = data

        @sio.on("audio_ready")
        async def _on_audio(data: dict) -> None:
            tid = str(data.get("task_id", ""))
            t = self._pending.get(tid)
            if t is None:
                return
            audio = data.get("audio") or b""
            if isinstance(audio, str):            # some builds base64 over the wire
                import base64
                try:
                    audio = base64.b64decode(audio)
                except Exception:  # noqa: BLE001
                    audio = b""
            t["chunks"].append(bytes(audio))
            t["meta"] = data
            if data.get("warning"):
                log.warning("TTS warning (%s): %s", tid, data.get("warning"))
            if data.get("is_final", True):
                t["done"].set()

        @sio.on("error")
        async def _on_error(data: dict) -> None:
            tid = str((data or {}).get("task_id", ""))
            t = self._pending.get(tid)
            if t is not None:
                t["error"] = str((data or {}).get("error", "unknown TTS error"))
                t["done"].set()

    # ---- synthesis ----
    async def synthesize(self, tagged_text: str, *, character_id: Optional[str] = None,
                         language: Optional[str] = None, audio_format: Optional[str] = None,
                         turn_mode: bool = True, timeout: Optional[float] = None) -> TTSResult:
        if not tagged_text.strip():
            raise TTSError("empty text")
        if not await self.connect():
            raise TTSError(f"TTS engine not reachable at {self.url}")
        assert self._sio is not None
        character_id = character_id or str(settings.get("tts.character_id", "MekaHime_Sakura"))
        language = language or str(settings.get("tts.language", "English"))
        audio_format = audio_format or str(settings.get("tts.audio_format", "wav"))
        timeout = timeout or float(settings.get("tts.timeout_s", 45))

        task_id = str(uuid.uuid4())
        payload = {
            "task_id": task_id,
            "text": tagged_text,
            "character_id": character_id,
            "language": language,
            "audio_format": audio_format,
        }
        if turn_mode:
            payload["turn_id"] = f"turn-{task_id[:8]}"
        entry = {"chunks": [], "done": asyncio.Event(), "error": None, "meta": {}, "queued": None}
        self._pending[task_id] = entry
        t0 = time.perf_counter()
        try:
            await self._sio.emit("tts_request_gateway", payload)
            try:
                await asyncio.wait_for(entry["done"].wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TTSError(f"TTS timeout after {timeout:.0f}s (character={character_id})")
            if entry["error"]:
                raise TTSError(entry["error"])
            audio = b"".join(entry["chunks"])
            if not audio:
                raise TTSError("TTS returned no audio")
            meta = entry["meta"] or {}
            return TTSResult(
                audio=audio,
                audio_format=str(meta.get("audio_format") or audio_format),
                duration=float(meta.get("audio_duration") or 0.0),
                text=str(meta.get("text") or tagged_text),
                elapsed=time.perf_counter() - t0,
                chunks=len(entry["chunks"]),
                meta={k: v for k, v in meta.items() if k != "audio"},
            )
        finally:
            self._pending.pop(task_id, None)

    # ---- health (HTTP side of the same server) ----
    async def health(self, timeout: float = 4.0) -> dict:
        url = self.url
        out: dict = {"url": url, "ok": False, "socket_connected": self._connected, "characters": []}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(f"{url}/ping")   # open on purpose: it is the "is it up" probe
                out["ping"] = r.text.strip()[:40]
                out["ok"] = r.status_code == 200
                try:
                    rc = await client.get(f"{url}/api/characters", headers=_auth())
                    chars = rc.json().get("characters", {})
                    out["characters"] = sorted(chars.keys()) if isinstance(chars, dict) else []
                except Exception:  # noqa: BLE001 — characters endpoint is optional on gateways
                    pass
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:200]
        return out


def _auth() -> dict:
    tok = settings.secret("SAKURA_SERVICE_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


tts = TTSClient()
