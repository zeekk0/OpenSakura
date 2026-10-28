"""One LLM door for every role (actor / director / action / mod).

Each role is routed to a provider by ``settings.llm.roles``:

- ``openrouter`` — OpenAI-compatible chat completions with ``response_format``
  json_object (the default).
- ``ollama`` — local ``/api/chat`` with ``format: "json"``; ``think`` is
  disabled for reasoning models (qwen3, deepseek-r1, ...) unless configured.
- ``openai`` — any OpenAI-compatible ``/chat/completions`` endpoint (llama.cpp's
  ``llama-server``, LM Studio, vLLM, LocalAI, Jan, Groq, Together, OpenAI itself, ...).
  Base URL, optional key and per-role model names are configurable; see
  docs/OPENAI_COMPATIBLE.md.

Callers ask for JSON; both providers are told to return a JSON object and the
result is parsed leniently (first ``{...}`` block). A 4xx from OpenRouter is
raised as ``LLMRejected`` (per-message problem, keep running); anything else
is ``LLMError``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Optional

import http_clients
from settings import DATA_DIR, settings

OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"

# ---- token / cost ledger ----------------------------------------------------------------
# OpenRouter reports exact cost per call when asked (`usage: {include: true}`); Ollama reports
# token counts (prompt_eval_count / eval_count) and costs nothing. "session" resets with the
# process (or the State-tab button); "lifetime" persists in data/usage.json.
USAGE_PATH = DATA_DIR / "usage.json"


def _empty_bucket() -> dict:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "estimated_calls": 0, "by_role": {}}


def _empty_usage() -> dict:
    return {"openrouter": _empty_bucket(), "ollama": _empty_bucket(), "openai": _empty_bucket()}


def _load_lifetime() -> dict:
    try:
        with USAGE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        out = _empty_usage()
        for prov in out:
            if isinstance(data.get(prov), dict):
                out[prov].update({k: v for k, v in data[prov].items() if k in out[prov]})
        return out
    except (OSError, json.JSONDecodeError, AttributeError):
        return _empty_usage()


_usage: dict = {"session": _empty_usage(), "lifetime": _load_lifetime(), "session_started": time.time(), "last": None}


def _save_lifetime() -> None:
    try:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=USAGE_PATH.parent, prefix=".usage_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_usage["lifetime"], f, indent=1)
        os.replace(tmp, USAGE_PATH)
    except OSError as e:
        print(f"[USAGE] could not persist ledger: {e}")


def record_usage(provider: str, role: str, model: str, prompt_tokens: int, completion_tokens: int,
                 cost_usd: Optional[float], estimated: bool = False) -> None:
    for scope in ("session", "lifetime"):
        b = _usage[scope].setdefault(provider, _empty_bucket())
        b["calls"] += 1
        b["prompt_tokens"] += int(prompt_tokens or 0)
        b["completion_tokens"] += int(completion_tokens or 0)
        b["cost_usd"] = round(b["cost_usd"] + float(cost_usd or 0.0), 8)
        if estimated:
            b["estimated_calls"] += 1
        r = b["by_role"].setdefault(role, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        r["calls"] += 1
        r["prompt_tokens"] += int(prompt_tokens or 0)
        r["completion_tokens"] += int(completion_tokens or 0)
        r["cost_usd"] = round(r["cost_usd"] + float(cost_usd or 0.0), 8)
    _usage["last"] = {"provider": provider, "role": role, "model": model, "prompt_tokens": prompt_tokens,
                      "completion_tokens": completion_tokens, "cost_usd": cost_usd, "estimated": estimated, "ts": time.time()}
    _save_lifetime()


def usage_snapshot() -> dict:
    return {"session": _usage["session"], "lifetime": _usage["lifetime"],
            "session_started": _usage["session_started"], "last": _usage["last"]}


def reset_session_usage() -> None:
    _usage["session"] = _empty_usage()
    _usage["session_started"] = time.time()


_pricing: dict = {"ts": 0.0, "models": {}}


async def _openrouter_price(model: str) -> Optional[tuple[float, float]]:
    """(usd per prompt token, usd per completion token) from the public model list; cached 1 h."""
    if time.time() - _pricing["ts"] > 3600 or not _pricing["models"]:
        try:
            r = await http_clients.client(OPENROUTER_MODELS).get(OPENROUTER_MODELS, timeout=10)
            r.raise_for_status()
            _pricing["models"] = {m["id"]: m.get("pricing", {}) for m in r.json().get("data", []) if isinstance(m, dict) and "id" in m}
            _pricing["ts"] = time.time()
        except Exception as e:  # noqa: BLE001 — pricing is a nicety
            print(f"[USAGE] pricing fetch failed: {e}")
            return None
    p = _pricing["models"].get(model)
    if not p:
        return None
    try:
        return float(p.get("prompt", 0)), float(p.get("completion", 0))
    except (TypeError, ValueError):
        return None


class LLMError(Exception):
    pass


class LLMRejected(LLMError):
    """4xx from the provider — content moderation, bad payload, unknown model."""

    def __init__(self, status: int, body: str, model: str = ""):
        self.status = status
        self.body = body
        self.model = model
        snippet = (body or "")[:600].replace("\n", " ")
        super().__init__(f"LLM {status} ({model or '?'}): {snippet}")


# ---- OpenAI-compatible provider helpers ----
_openai_json_unsupported: set[str] = set()   # base URLs that rejected response_format
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def openai_base() -> str:
    """Normalised base URL (``http://host:port/v1``). Works for full completion endpoint too"""
    base = str(settings.get("llm.openai.url", "http://127.0.0.1:8080/v1")).strip().rstrip("/")
    for tail in ("/chat/completions", "/completions"):
        if base.endswith(tail):
            base = base[: -len(tail)]
    return base.rstrip("/")


def _openai_headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = settings.secret("OPENAI_COMPAT_API_KEY")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def provider_for(role: str) -> str:
    return str(settings.get(f"llm.roles.{role}", "openrouter"))


def model_for(role: str, provider: Optional[str] = None) -> str:
    provider = provider or provider_for(role)
    return str(settings.get(f"llm.{provider}.{role}_model", ""))


def _strip_images_for_ollama(messages: list[dict]) -> list[dict]:
    """Ollama's /api/chat wants plain string content (+ optional base64 images).
    OpenAI-style content parts with image URLs are flattened to their text."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            out.append({"role": m.get("role", "user"), "content": text})
        else:
            out.append(m)
    return out


async def chat(role: str, messages: list[dict], *, temperature: float = 0.9,
               json_mode: bool = True, timeout: float = 90.0,
               max_tokens: Optional[int] = None) -> str:
    """Return the assistant message content as text."""
    provider = provider_for(role)
    model = model_for(role, provider)
    if not model and provider != "openai": # some local servers with only 1 model allow no models
        raise LLMError(f"no model configured for role={role} provider={provider}")

    if provider == "openrouter":
        api_key = settings.secret("OPENROUTER_API_KEY")
        if not api_key:
            raise LLMError("OPENROUTER_API_KEY is not set (Setup page → OpenRouter)")
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                   "usage": {"include": True}}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": str(settings.get("llm.openrouter.site_url", "http://localhost")),
            "X-Title": str(settings.get("llm.openrouter.app_name", "OpenSakura")),
            "Content-Type": "application/json",
        }
        r = await http_clients.client(OPENROUTER_CHAT).post(OPENROUTER_CHAT, json=payload, headers=headers,
                                                            timeout=timeout)
        if 400 <= r.status_code < 500:
            raise LLMRejected(r.status_code, r.text, model)
        if r.status_code >= 500:
            raise LLMError(f"OpenRouter {r.status_code}: {r.text[:300]}")
        body = r.json()
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"OpenRouter malformed response: {e}: {str(body)[:300]}")
        u = body.get("usage") or {}
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        cost, estimated = u.get("cost"), False
        if cost is None:
            price = await _openrouter_price(model)
            if price:
                cost, estimated = pt * price[0] + ct * price[1], True
        record_usage("openrouter", role, model, pt, ct, cost, estimated)
        return content

    if provider == "ollama":
        base = str(settings.get("llm.ollama.url", "http://127.0.0.1:11434")).rstrip("/")
        payload = {
            "model": model,
            "messages": _strip_images_for_ollama(messages),
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": int(settings.get("llm.ollama.num_ctx", 8192))},
        }
        if json_mode:
            payload["format"] = "json"
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        if settings.get("llm.ollama.think", False) is False:
            payload["think"] = False
        client = http_clients.client(base)
        r = await client.post(f"{base}/api/chat", json=payload, timeout=timeout)
        if r.status_code == 400 and "think" in r.text.lower() and "think" in payload:
            # model has no thinking switch — retry without it
            payload.pop("think", None)
            r = await client.post(f"{base}/api/chat", json=payload, timeout=timeout)
        if 400 <= r.status_code < 500:
            raise LLMRejected(r.status_code, r.text, model)
        if r.status_code >= 500:
            raise LLMError(f"Ollama {r.status_code}: {r.text[:300]}")
        body = r.json()
        try:
            content = body["message"]["content"] or ""
        except (KeyError, TypeError) as e:
            raise LLMError(f"Ollama malformed response: {e}: {str(body)[:300]}")
        record_usage("ollama", role, model, int(body.get("prompt_eval_count") or 0), int(body.get("eval_count") or 0), 0.0)
        return content

    if provider == "openai":
        base = openai_base()
        if not base:
            raise LLMError("OpenAI-compatible URL is not set (Setup → LLM)")
        url = f"{base}/chat/completions"
        payload = {"model": model or "default", "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        extra = settings.get("llm.openai.extra_body") or {}
        if isinstance(extra, dict):
            payload.update(extra) # e.g. {"chat_template_kwargs": {"enable_thinking": false}}
        mode = str(settings.get("llm.openai.json_mode", "auto")).lower()   # auto | on | off
        want_json = json_mode and mode != "off" and not (mode == "auto" and base in _openai_json_unsupported)
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        client = http_clients.client(url)
        r = await client.post(url, json=payload, headers=_openai_headers(), timeout=timeout)
        if (want_json and mode == "auto" and r.status_code in (400, 422)
                and any(w in r.text.lower() for w in ("response_format", "json_object", "grammar", "json"))):
            _openai_json_unsupported.add(base)
            payload.pop("response_format", None)
            r = await client.post(url, json=payload, headers=_openai_headers(), timeout=timeout)
        if 400 <= r.status_code < 500:
            raise LLMRejected(r.status_code, r.text, model)
        if r.status_code >= 500:
            raise LLMError(f"OpenAI-compatible server {r.status_code}: {r.text[:300]}")
        body = r.json()
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"OpenAI-compatible malformed response: {e}: {str(body)[:300]}")
        content = _THINK_BLOCK.sub("", content).strip()   # reasoning models may inline <think>...</think>
        u = body.get("usage") or {}
        cost = u.get("cost") if isinstance(u.get("cost"), (int, float)) else 0.0
        record_usage("openai", role, model or "default", int(u.get("prompt_tokens") or 0),
                     int(u.get("completion_tokens") or 0), cost)
        return content

    raise LLMError(f"unknown provider {provider!r} for role {role}")


def parse_json(text: str) -> dict | None:
    """Lenient: first {...} block, tolerating ```json fences and prose."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def chat_json(role: str, messages: list[dict], **kw) -> dict | None:
    return parse_json(await chat(role, messages, json_mode=True, **kw))


# ---- health / discovery (Setup page) ----
async def ollama_health(timeout: float = 4.0) -> dict:
    base = str(settings.get("llm.ollama.url", "http://127.0.0.1:11434")).rstrip("/")
    try:
        r = await http_clients.client(base).get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        models = [m.get("name") for m in r.json().get("models", []) if isinstance(m, dict)]
        return {"ok": True, "url": base, "models": models}
    except Exception as e:  # noqa: BLE001 — health probe
        return {"ok": False, "url": base, "error": str(e)[:200], "models": []}


async def openai_health(timeout: float = 4.0) -> dict:
    """Probe ``{base}/models`` (llama.cpp, LM Studio, vLLM, OpenAI all serve it). 
    A server without that route still counts as up if it answers at all, so the list may be empty."""
    base = openai_base()
    configured = bool(settings.get("llm.openai.url"))
    try:
        url = f"{base}/models"
        r = await http_clients.client(url).get(url, headers=_openai_headers(), timeout=timeout)
        if r.status_code in (401, 403):
            return {"ok": False, "url": base, "configured": configured, "error": f"{r.status_code}: key rejected", "models": []}
        models: list[str] = []
        if r.status_code == 200:
            try:
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else None
                models = [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]
            except ValueError:
                pass
        return {"ok": r.status_code < 500, "url": base, "configured": configured, "models": models}
    except Exception as e:
        return {"ok": False, "url": base, "configured": configured, "error": str(e)[:200], "models": []}


async def openrouter_health(timeout: float = 6.0) -> dict:
    key = settings.secret("OPENROUTER_API_KEY")
    if not key:
        return {"ok": False, "configured": False, "error": "no API key"}
    try:
        url = "https://openrouter.ai/api/v1/auth/key"
        r = await http_clients.client(url).get(url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        if r.status_code == 200:
            d = r.json().get("data", {})
            return {"ok": True, "configured": True, "label": d.get("label"), "usage": d.get("usage"), "limit": d.get("limit")}
        return {"ok": False, "configured": True, "error": f"{r.status_code}: {r.text[:120]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "error": str(e)[:200]}


async def ollama_pull(model: str) -> dict:
    """Kick a (blocking) pull; the Setup page calls this in the background."""
    base = str(settings.get("llm.ollama.url", "http://127.0.0.1:11434")).rstrip("/")
    r = await http_clients.client(base).post(f"{base}/api/pull", json={"model": model, "stream": False}, timeout=None)
    return {"ok": r.status_code == 200, "status": r.text[:200]}
