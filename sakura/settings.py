"""Runtime settings for OpenSakura.

Two files, two jobs:

- ``config/settings.json`` — everything that is NOT a secret (mode, models,
  service URLs, reply toggles, ports). Editable from the Setup page; created
  from ``config/settings.example.json`` on first run.
- ``.env`` — secrets only (OPENROUTER_API_KEY, DISCORD_BOT_TOKEN). Never
  returned to the browser; the Setup page only learns whether a key is set.

Paths are resolved relative to the project root so the folder is relocatable.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
WEB_DIR = ROOT / "web"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
EXAMPLE_PATH = CONFIG_DIR / "settings.example.json"
ENV_PATH = ROOT / ".env"

SECRET_KEYS = ("OPENROUTER_API_KEY", "OPENAI_COMPAT_API_KEY", "SAKURA_SERVICE_TOKEN", "DISCORD_BOT_TOKEN")

DEFAULTS: dict[str, Any] = {
    "mode": "web",                       # "web" | "discord"
    "persona_file": "config/sakura.json",
    "owner": {"name": "Master", "user_ids": []},   # web user (id 0) is always the owner
    "llm": {
        # per-role provider: "openrouter" | "ollama" | "openai" (any OpenAI-compatible endpoint)
        "roles": {"actor": "openrouter", "director": "openrouter", "action": "openrouter", "mod": "openrouter"},
        "openrouter": {
            "actor_model": "google/gemini-2.5-flash",
            "director_model": "google/gemini-2.5-flash",
            "action_model": "google/gemini-2.5-flash",
            "mod_model": "google/gemini-2.5-flash",
            "site_url": "https://github.com/zeekk0/OpenSakura",
            "app_name": "OpenSakura",
        },
        "ollama": {
            "url": "http://127.0.0.1:11434",
            "actor_model": "qwen3:8b",
            "director_model": "qwen3:8b",
            "action_model": "qwen3:8b",
            "mod_model": "qwen3:8b",
            "num_ctx": 8192,
            "think": False,
        },
        "openai": {
            "url": "http://127.0.0.1:8080/v1",\
            "actor_model": "",         # empty = send "default" (single-model servers ignore it)
            "director_model": "",
            "action_model": "",
            "mod_model": "",
            "json_mode": "auto",       # auto (try, fall back) | on | off
            "extra_body": {},          # e.g. {"chat_template_kwargs": {"enable_thinking": false}}
        },
    },
    "stt": {
        "enabled": True,
        "autostart": True,               # launch.py starts services/stt
        "url": "http://127.0.0.1:50000",
        "lang": "auto",                  # auto | zh | en | yue | ja | ko
        "device": "auto",              # auto | cpu | cuda[:N] | xpu | mps  (auto: best available, falls back to CPU)
        "threads": 0,                  # CPU threads for PyTorch, 0 = default (all cores)
    },
    "tts": {
        "enabled": True,
        "autostart": True,               # launch.py starts vendor/tts-engine (1 worker)
        "url": "http://127.0.0.1:8002",
        "engine_dir": "vendor/tts-engine",
        "character_id": "MekaHime_Sakura",
        "language": "English",
        "device": "auto",                # auto | cuda | cuda:N | xpu | mps | cpu
        "extra_args": [],                # extra voice-server flags, e.g. ["--batch-size", "1"] (CPU-only: one sentence at a time)
        "audio_format": "wav",           # wav decodes everywhere in the browser; opus is smaller
        "timeout_s": 45,
    },
    "actions": {"enabled": True, "followup": True},
    "reply": {
        # shared
        "output_in_chat": True,          # post text
        "output_in_voice": True,         # synthesize + play / stream
        "safety_filter": True,           # sensitive-word tiering
        "show_ai_disclaimer": False,
        "intimacy_mode": True,           # owner flirting block at favorability >= 0.85 (18+); False drops it
        # web
        "respond_to_text": True,
        "respond_to_voice": True,
        # discord
        "discord_trigger": "smart",      # "all" | "mention" | "smart" (SLM gate)
        "respond_to_bots": False,
    },
    "memory": {"consolidation_interval": 4},
    "server": {"host": "127.0.0.1", "port": 8010, "open_browser": True},
    "discord": {"guild_id": "", "channel_ids": [], "stage_channel_id": "", "error_channel_id": ""},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Settings:
    """Thread-safe settings holder. ``get("tts.character_id")`` style access."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ---- settings.json ----
    def load(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            for p in (EXAMPLE_PATH, SETTINGS_PATH):
                if p.exists():
                    try:
                        with p.open(encoding="utf-8") as f:
                            data = _deep_merge(data, json.load(f))
                    except (OSError, json.JSONDecodeError) as e:
                        print(f"[SETTINGS] failed to read {p.name}: {e}")
            self._data = data
            if not SETTINGS_PATH.exists():
                self.save()

    def save(self) -> None:
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_PATH.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_PATH)

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self._data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return copy.deepcopy(cur)

    def update(self, partial: dict) -> dict:
        """Deep-merge ``partial`` into the settings, persist, return the result."""
        with self._lock:
            self._data = _deep_merge(self._data, partial or {})
            self.save()
            return self.all()

    def override(self, partial: dict) -> None:
        """Deep-merge ``partial`` for this process only; nothing is written to settings.json.

        Used by ``launch.py --no-stt / --no-tts / --text-only`` so the backend stops trying to
        reach services that were deliberately not started.
        """
        with self._lock:
            self._data = _deep_merge(self._data, partial or {})

    # ---- paths ----
    def path(self, dotted: str) -> Path:
        p = Path(str(self.get(dotted, "")))
        return p if p.is_absolute() else (ROOT / p)

    # ---- .env secrets ----
    @staticmethod
    def _read_env_lines() -> list[str]:
        if not ENV_PATH.exists():
            return []
        return ENV_PATH.read_text(encoding="utf-8").splitlines()

    def secret(self, name: str) -> str:
        val = os.environ.get(name, "")
        if val:
            return val.strip()
        for line in self._read_env_lines():
            m = re.match(rf"^\s*{re.escape(name)}\s*=\s*(.*)$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
        return ""

    def secret_status(self) -> dict[str, bool]:
        return {k: bool(self.secret(k)) for k in SECRET_KEYS}

    def set_secret(self, name: str, value: str) -> None:
        """Write/replace ``NAME=value`` in .env, keeping other lines intact."""
        if name not in SECRET_KEYS:
            raise ValueError(f"not a managed secret: {name}")
        value = (value or "").strip()
        lines = self._read_env_lines()
        pat = re.compile(rf"^\s*{re.escape(name)}\s*=")
        replaced = False
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = f"{name}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{name}={value}")
        ENV_PATH.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        os.environ[name] = value


settings = Settings()
