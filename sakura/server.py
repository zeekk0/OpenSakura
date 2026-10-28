"""OpenSakura HTTP + WebSocket server (FastAPI).

Serves the web page (three.js stage + Setup/Play panels) and exposes:

    GET  /api/health            live probes: LLM providers, STT, TTS, Discord
    GET  /api/settings          settings.json (+ which secrets are set)
    POST /api/settings          deep-merge partial settings
    POST /api/secrets           write OPENROUTER_API_KEY / OPENAI_COMPAT_API_KEY / DISCORD_BOT_TOKEN to .env
    GET  /api/state             emotions, dominant emotion, owner profile, uptime
    POST /api/chat              {text} -> one turn (reply via HTTP, audio/actions via /ws)
    POST /api/voice             multipart audio -> STT -> one turn
    POST /api/stt               multipart audio -> transcript only
    POST /api/tts               {text, emotion?, intensity?} -> audio bytes (test)
    POST /api/control/*         reset / emotions / unhalt / say
    GET  /api/persona  POST     persona editor
    WS   /ws                    hello | reply | audio | action_chain | followup_action_chain | state | error
    POST /reply                 Discord bot contract: one turn in, one reply out (discord/ mode)
    POST /discord/state         the Discord bot reports whether it is running

Every /ws message is a JSON object with a `type`; reply messages carry `content` and
`emotion_state`, action messages `action_chain`, audio messages base64 `audio_data`.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import brain as brain_mod  # noqa: E402
import http_clients  # noqa: E402
import llm  # noqa: E402
import stt_client  # noqa: E402
import tts_emotion_mapper  # noqa: E402
from action_controller import ActionController  # noqa: E402
from brain import WEB_USER_ID, brain  # noqa: E402
from sensitive_words import WordBank  # noqa: E402
from settings import DATA_DIR, LOG_DIR, SECRET_KEYS, WEB_DIR, settings  # noqa: E402
from tts_client import TTSError, tts  # noqa: E402

log = logging.getLogger("sakura.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-16s %(levelname)-5s %(message)s")

app = FastAPI(title="OpenSakura", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

actions = ActionController()
HISTORY: collections.deque = collections.deque(maxlen=10)        # (user_text, reply)
ACTION_HISTORY: collections.deque = collections.deque(maxlen=12)
_turn_lock = asyncio.Lock()
_word_bank: Optional[WordBank] = None
_last_turn: dict[str, Any] = {}
_discord_state: dict[str, Any] = {"running": False, "detail": ""}


# ==== websocket hub ====
class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_json({"type": "hello", "ok": True, "name": brain.name, "state": brain.snapshot(),
                            "settings": _public_settings()})
        while True:
            # clients may send pings / small commands; we only need to keep the socket open
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("ws closed: %s", e)
    finally:
        hub.clients.discard(ws)


# ==== lifecycle ====
@app.on_event("startup")
async def _startup() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    brain.start()
    _reload_word_bank()
    actions.set_followup_callback(_on_followup_chain)
    if settings.get("tts.enabled", True):
        asyncio.create_task(tts.connect())
    log.info("OpenSakura backend up — mode=%s persona=%s", settings.get("mode"), brain.name)


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        await tts.close()
    finally:
        await http_clients.aclose_all()     # the shared STT / OpenRouter / Ollama clients (stt_client.py, llm.py)


def _reload_word_bank() -> None:
    global _word_bank
    sw = brain.persona_cfg.get("sensitive_words") or {}
    try:
        _word_bank = WordBank(sw) if sw else None
    except Exception as e:  # noqa: BLE001
        log.warning("sensitive word bank disabled: %s", e)
        _word_bank = None


def _public_settings() -> dict:
    s = settings.all()
    s["_secrets"] = settings.secret_status()
    return s


# ==== the turn pipeline ====
async def _on_followup_chain(chain: dict) -> None:
    for a in (chain or {}).get("actions", []):
        ACTION_HISTORY.append(a.get("name", "idle"))
    await hub.broadcast({"type": "followup_action_chain", "turn_id": _last_turn.get("turn_id"), "action_chain": chain})


async def _tts_task(turn_id: str, text: str, emotion: str, intensity: float) -> dict:
    t0 = time.perf_counter()
    tagged = tts_emotion_mapper.format_tts_message(text, emotion, intensity)
    try:
        r = await tts.synthesize(tagged)
    except TTSError as e:
        await hub.broadcast({"type": "error", "where": "tts", "turn_id": turn_id, "error": str(e)})
        return {"ok": False, "error": str(e)}
    payload = {
        "type": "audio", "turn_id": turn_id,
        "audio_data": base64.b64encode(r.audio).decode("ascii"),
        "audio_format": r.audio_format, "duration": r.duration,
        "text": text, "tagged_text": tagged, "emotion": emotion, "intensity": intensity,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000), "bytes": len(r.audio),
    }
    await hub.broadcast(payload)
    return {"ok": True, "duration": r.duration, "elapsed_ms": payload["elapsed_ms"], "bytes": len(r.audio),
            "tagged_text": tagged}


async def _action_task(turn_id: str, user_text: str, reply: str, emotion: str, intensity: float,
                       relationship: str) -> dict:
    actions.enabled = bool(settings.get("actions.enabled", True))
    ctx = {
        "user_text": user_text, "ai_reply": reply, "emotion": emotion, "intensity": intensity,
        "relationship": relationship, "conversation_history": list(HISTORY), "action_history": list(ACTION_HISTORY),
    }
    try:
        chain = await actions.get_action_recommendation(ctx)
    except Exception as e:  # noqa: BLE001
        await hub.broadcast({"type": "error", "where": "actions", "turn_id": turn_id, "error": str(e)[:300]})
        return {"ok": False, "error": str(e)[:300]}
    for a in (chain or {}).get("actions", []):
        ACTION_HISTORY.append(a.get("name", "idle"))
    await hub.broadcast({"type": "action_chain", "turn_id": turn_id, "action_chain": chain})
    return {"ok": True, "actions": [a.get("name") for a in (chain or {}).get("actions", [])]}


async def run_turn(text: str, *, user_id: int = WEB_USER_ID, display_name: str = "", channel: str = "web",
                   source: str = "text", images: Optional[list[str]] = None,
                   system_flavor: Optional[str] = None, addressed: Optional[bool] = None,
                   wait_for_media: bool = False) -> dict:
    """One conversational turn: brain -> ws 'reply' -> (tts, actions) in parallel."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    rp = settings.get("reply", {}) or {}
    if source == "voice" and not rp.get("respond_to_voice", True):
        return {"ok": False, "skipped": "respond_to_voice is off"}
    if source == "text" and not rp.get("respond_to_text", True):
        return {"ok": False, "skipped": "respond_to_text is off"}

    flavor = system_flavor
    if rp.get("safety_filter", True) and _word_bank is not None and flavor is None:
        try:
            hit = _word_bank.match(text)
            if hit:
                tier = hit[0] if isinstance(hit, (tuple, list)) else str(hit)
                flavor = f"sensitive:{tier}"
        except Exception:  # noqa: BLE001
            pass

    turn_id = uuid.uuid4().hex[:12]
    async with _turn_lock:
        await hub.broadcast({"type": "thinking", "turn_id": turn_id, "text": text, "source": source})
        res = await brain_mod.reply(text, user_id, display_name, images=images, system_flavor=flavor,
                                    addressed=addressed, channel=channel)
    if res.halted or res.rejected or not res.reply:
        await hub.broadcast({"type": "error", "where": "actor", "turn_id": turn_id,
                             "error": res.error or "empty reply", "halted": res.halted})
        return {"ok": False, "turn_id": turn_id, "error": res.error, "halted": res.halted, "rejected": res.rejected}

    HISTORY.append((text, res.reply))
    _last_turn.update({"turn_id": turn_id, "text": text, "reply": res.reply, "emotion": res.emotion})

    reply_msg = {
        "type": "reply", "turn_id": turn_id, "source": source,
        "content": {"text": res.reply, "emotion": res.emotion, "safety": "ok", "notes": ""},
        "llm_input": {"emotion": res.emotion, "intensity": res.intensity},
        "emotion_state": brain.emotion_state_payload(),
        "conversation_context": {"owner_present": True, "conversation_target": display_name or "owner",
                                 "relationship": res.relationship, "favorability": res.favorability,
                                 "user_id": str(user_id)},
        "user_text": text, "image_url": res.image_url, "applied_deltas": res.applied_deltas,
        "provider": res.provider, "model": res.model, "latency_ms": res.latency_ms,
    }
    await hub.broadcast(reply_msg)

    tasks = []
    if rp.get("output_in_voice", True) and settings.get("tts.enabled", True):
        tasks.append(("tts", asyncio.create_task(_tts_task(turn_id, res.reply, res.emotion, res.intensity))))
    if settings.get("actions.enabled", True):
        tasks.append(("actions", asyncio.create_task(_action_task(turn_id, text, res.reply, res.emotion, res.intensity, res.relationship))))
    media: dict = {}
    if wait_for_media and tasks:
        done = await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        # keyed by name, so a disabled TTS (text-only mode) does not shift the actions result into "tts"
        media = {name: ({"ok": False, "error": str(r)} if isinstance(r, BaseException) else r)
                 for (name, _), r in zip(tasks, done)}

    return {"ok": True, "turn_id": turn_id, "reply": res.reply, "emotion": res.emotion, "intensity": res.intensity,
            "favorability": res.favorability, "relationship": res.relationship, "applied_deltas": res.applied_deltas,
            "provider": res.provider, "model": res.model, "latency_ms": res.latency_ms, "image_url": res.image_url,
            "media": media}


# ==== API: health / settings / state ====
@app.get("/api/health")
async def api_health() -> dict:
    roles = settings.get("llm.roles", {})
    or_h, ol_h, st_h, tt_h, oa_h = await asyncio.gather(
        llm.openrouter_health(), llm.ollama_health(), stt_client.health(), tts.health(),
        llm.openai_health() if "openai" in roles.values() else asyncio.sleep(0, {"ok": False, "configured": bool(settings.get("llm.openai.url")),
                                                              "url": llm.openai_base(), "models": [], "error": "not used by any role"}))
    return {
        "ok": True, "mode": settings.get("mode"), "persona": brain.name,
        "llm": {"roles": roles, "openrouter": or_h, "ollama": ol_h, "openai": oa_h,
                "models": {r: {"provider": roles.get(r), "model": llm.model_for(r)} for r in ("actor", "director", "action", "mod")}},
        "stt": {**st_h, "enabled": settings.get("stt.enabled")},
        "tts": {**tt_h, "enabled": settings.get("tts.enabled"), "character_id": settings.get("tts.character_id")},
        "discord": {"token_set": bool(settings.secret("DISCORD_BOT_TOKEN")), **_discord_state},
        "ws_clients": len(hub.clients), "halted": brain.halted,
    }


@app.get("/api/settings")
async def api_get_settings() -> dict:
    return _public_settings()


@app.post("/api/settings")
async def api_set_settings(partial: dict) -> dict:
    partial.pop("_secrets", None)
    before_tts = settings.get("tts.url")
    settings.update(partial)
    if settings.get("tts.url") != before_tts:
        asyncio.create_task(tts.connect())
    await hub.broadcast({"type": "settings", "settings": _public_settings()})
    return _public_settings()


class SecretsIn(BaseModel):
    OPENROUTER_API_KEY: Optional[str] = None
    OPENAI_COMPAT_API_KEY: Optional[str] = None
    SAKURA_SERVICE_TOKEN: Optional[str] = None
    DISCORD_BOT_TOKEN: Optional[str] = None


@app.post("/api/secrets")
async def api_set_secrets(body: SecretsIn) -> dict:
    for k in SECRET_KEYS:
        v = getattr(body, k)
        if v is not None:
            settings.set_secret(k, v)
    return {"ok": True, "secrets": settings.secret_status()}


@app.get("/api/state")
async def api_state() -> dict:
    s = brain.snapshot()
    s.update({"mode": settings.get("mode"), "tts_connected": tts.connected, "last_turn": _last_turn,
              "action_history": list(ACTION_HISTORY), "usage": llm.usage_snapshot()})
    return s


@app.post("/api/control/usage_reset")
async def api_usage_reset() -> dict:
    llm.reset_session_usage()
    return {"ok": True, "usage": llm.usage_snapshot()}


# ==== API: chat / voice / stt / tts ====
class ChatIn(BaseModel):
    text: str
    display_name: str = ""
    wait_for_media: bool = False


@app.post("/api/chat")
async def api_chat(body: ChatIn) -> dict:
    if settings.get("mode") == "discord":
        raise HTTPException(409, "Sakura is in Discord mode — switch to web mode in Setup")
    return await run_turn(body.text, display_name=body.display_name, channel="web", source="text",
                          wait_for_media=body.wait_for_media)


@app.post("/api/stt")
async def api_stt(file: UploadFile = File(...), lang: Optional[str] = Form(None)) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty audio")
    t0 = time.perf_counter()
    try:
        r = await stt_client.transcribe(data, filename=file.filename or "utterance.wav", lang=lang or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"STT failed: {str(e)[:200]}")
    r["ms"] = int((time.perf_counter() - t0) * 1000)
    return r


@app.post("/api/voice")
async def api_voice(file: UploadFile = File(...), lang: Optional[str] = Form(None),
                    display_name: str = Form("")) -> dict:
    if settings.get("mode") == "discord":
        raise HTTPException(409, "Sakura is in Discord mode — switch to web mode in Setup")
    stt_res = await api_stt(file, lang)
    text = stt_res.get("text", "").strip()
    if not text:
        await hub.broadcast({"type": "stt", "text": "", "empty": True})
        return {"ok": False, "stt": stt_res, "skipped": "no speech"}
    await hub.broadcast({"type": "stt", "text": text, "lang": stt_res.get("lang"), "emotion": stt_res.get("emotion"),
                         "ms": stt_res.get("ms")})
    turn = await run_turn(text, display_name=display_name, channel="voice", source="voice")
    return {"ok": turn.get("ok"), "stt": stt_res, "turn": turn}


class TTSIn(BaseModel):
    text: str
    emotion: str = "neutral"
    intensity: float = 0.5
    character_id: Optional[str] = None
    language: Optional[str] = None
    audio_format: Optional[str] = None
    raw: bool = False      # True: text already carries 【tag】, skip the emotion mapper


@app.post("/api/tts")
async def api_tts(body: TTSIn) -> Response:
    tagged = body.text if body.raw else tts_emotion_mapper.format_tts_message(body.text, body.emotion, body.intensity)
    try:
        r = await tts.synthesize(tagged, character_id=body.character_id, language=body.language,
                                 audio_format=body.audio_format)
    except TTSError as e:
        raise HTTPException(502, str(e))
    media = "audio/wav" if r.audio_format.lower() == "wav" else "audio/ogg"
    return Response(content=r.audio, media_type=media,
                    headers={"X-Audio-Format": r.audio_format, "X-Audio-Duration": str(r.duration),
                             "X-Tagged-Text": tagged.encode("ascii", "backslashreplace").decode("ascii"),
                             "X-Elapsed-Ms": str(int(r.elapsed * 1000))})


@app.get("/api/tts/characters")
async def api_tts_characters() -> dict:
    h = await tts.health()
    return {"ok": h.get("ok", False), "characters": h.get("characters", []), "current": settings.get("tts.character_id")}


# ==== API: control ====
@app.post("/api/control/reset")
async def api_reset() -> dict:
    brain.reset_all()
    HISTORY.clear()
    ACTION_HISTORY.clear()
    await hub.broadcast({"type": "state", "state": brain.snapshot(), "reset": True})
    return {"ok": True}


@app.post("/api/control/unhalt")
async def api_unhalt() -> dict:
    brain.unhalt()
    return {"ok": True}


@app.post("/api/control/emotions")
async def api_set_emotions(values: dict) -> dict:
    brain.set_emotions(values)
    await hub.broadcast({"type": "state", "state": brain.snapshot()})
    return brain.snapshot()


class SayIn(BaseModel):
    text: str
    emotion: Optional[str] = None
    intensity: Optional[float] = None


@app.post("/api/control/say")
async def api_say(body: SayIn) -> dict:
    """Speak a line without the LLM (panel test button)."""
    emo, inten = brain.dominant_emotion()
    turn_id = "say-" + uuid.uuid4().hex[:8]
    await hub.broadcast({"type": "reply", "turn_id": turn_id, "source": "say",
                         "content": {"text": body.text, "emotion": body.emotion or emo, "safety": "ok", "notes": ""},
                         "llm_input": {"emotion": body.emotion or emo, "intensity": body.intensity if body.intensity is not None else inten},
                         "emotion_state": brain.emotion_state_payload()})
    return await _tts_task(turn_id, body.text, body.emotion or emo, body.intensity if body.intensity is not None else inten)


class ActionsIn(BaseModel):
    actions: list[dict]          # [{"name": "Hop", "duration": 2.0}, ...]


@app.post("/api/control/actions")
async def api_actions(body: ActionsIn) -> dict:
    """Play a body-action chain on connected pages (no LLM) — used by scripted captures."""
    chain = {"actions": body.actions, "total_duration": sum(float(a.get("duration", 2.0)) for a in body.actions), "reasoning": "manual"}
    for a in body.actions:
        ACTION_HISTORY.append(a.get("name", "idle"))
    await hub.broadcast({"type": "action_chain", "turn_id": "manual", "action_chain": chain})
    return {"ok": True}


class FaceIn(BaseModel):
    expression: Optional[str] = None   # happy | angry | sad | relaxed | surprised | neutral  (direct preset, full weight)
    weight: float = 1.0
    emotion: Optional[str] = None      # Sakura emotion name -> pulse through the normal mapping
    intensity: float = 0.8


@app.post("/api/control/face")
async def api_face(body: FaceIn) -> dict:
    """Drive the avatar's face directly on connected pages (no backend state change)."""
    await hub.broadcast({"type": "face", "expression": body.expression, "weight": body.weight,
                         "emotion": body.emotion, "intensity": body.intensity})
    return {"ok": True}


@app.get("/api/persona")
async def api_get_persona() -> dict:
    return {"persona": brain.persona, "sensitive_words": brain.persona_cfg.get("sensitive_words", {})}


@app.post("/api/persona")
async def api_set_persona(body: dict) -> dict:
    if "persona" in body and isinstance(body["persona"], dict):
        brain.save_persona(body["persona"])
    if "sensitive_words" in body and isinstance(body["sensitive_words"], dict):
        brain.persona_cfg["sensitive_words"] = body["sensitive_words"]
        brain.save_persona(brain.persona)
        _reload_word_bank()
    return {"ok": True, "persona": brain.persona}


@app.get("/api/llm/ollama/models")
async def api_ollama_models() -> dict:
    return await llm.ollama_health()


@app.get("/api/llm/openai/models")
async def api_openai_models() -> dict:
    return await llm.openai_health()


class PullIn(BaseModel):
    model: str


@app.post("/api/llm/ollama/pull")
async def api_ollama_pull(body: PullIn) -> dict:
    asyncio.create_task(llm.ollama_pull(body.model))
    return {"ok": True, "started": body.model}


# ==== Discord bot contract (used by discord/ mode) ====
class MentionedUser(BaseModel):
    user_id: int
    display_name: str


class ReplyRequest(BaseModel):
    text: str
    user_id: int
    display_name: str
    images: list[str] = []
    is_proactive: bool = False
    system_flavor: Optional[str] = None
    addressed_to_sakura: Optional[bool] = None
    mentioned_others: list[MentionedUser] = []


@app.post("/reply")
async def discord_reply(req: ReplyRequest) -> dict:
    res = await brain_mod.reply(req.text, req.user_id, req.display_name, images=req.images,
                                system_flavor=req.system_flavor, addressed=req.addressed_to_sakura,
                                mentioned_others=req.mentioned_others, channel="discord")
    if res.reply:
        HISTORY.append((req.text, res.reply))
        await hub.broadcast({"type": "reply", "turn_id": uuid.uuid4().hex[:12], "source": "discord",
                             "content": {"text": res.reply, "emotion": res.emotion, "safety": "ok", "notes": ""},
                             "llm_input": {"emotion": res.emotion, "intensity": res.intensity},
                             "emotion_state": brain.emotion_state_payload(), "user_text": req.text,
                             "conversation_context": {"relationship": res.relationship, "favorability": res.favorability,
                                                      "user_id": str(req.user_id), "conversation_target": req.display_name}})
    return {"reply": res.reply, "emotion_values": res.emotion_values, "applied_deltas": res.applied_deltas,
            "halted": res.halted, "favorability": res.favorability, "relationship": res.relationship,
            "message_count": res.message_count, "promoted_to": res.promoted_to, "image_url": res.image_url,
            "emotion_target": res.emotion_target}


@app.post("/discord/state")
async def discord_state(body: dict) -> dict:
    """The discord bot process reports itself here (running / detail)."""
    _discord_state.update({k: body.get(k) for k in ("running", "detail") if k in body})
    return {"ok": True}


# ==== static web (must be last) ====
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
