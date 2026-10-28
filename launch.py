#!/usr/bin/env python3
"""OpenSakura launcher — one command for the whole stack.

    python launch.py              start STT + the voice server + Sakura, open the browser
    python launch.py --check      validate the install without starting anything
                                  (with --text-only / --no-stt / --no-tts the voice rows are informational)
    python launch.py --text-only  no GPU needed: text chat + 3D avatar, brain on OpenRouter/Ollama
    python launch.py --no-stt --no-tts --no-browser --host 127.0.0.1 --port 8010

Child services log to logs/stt.log and logs/tts.log; the backend logs
to the console. Ctrl+C stops everything (children are killed as a tree).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit(f"OpenSakura needs Python 3.11+ (recommend 3.11) because there is type hintings (this is {sys.version.split()[0]}). "
             "Install one beside your Python (`uv python install 3.11`), then run scripts/setup.py (or Install_OpenSakura.bat).")

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sakura"))

IS_WIN = os.name == "nt"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")

# Re-exec inside the project venv if there is one and we're not already in it.
if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve() and not os.environ.get("OPENSAKURA_NO_REEXEC"):
    os.environ["OPENSAKURA_NO_REEXEC"] = "1"
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

from settings import settings, LOG_DIR, DATA_DIR  # noqa: E402

CHILDREN: list[subprocess.Popen] = []


def log(msg: str) -> None:
    print(f"[launch] {msg}", flush=True)


def port_of(url: str, default: int) -> int:
    try:
        return urlparse(url).port or default
    except Exception:  # noqa: BLE001
        return default


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def opensakura_running(port: int) -> bool:
    """True if an OpenSakura backend answers GET /api/state on this port (a JSON object with its emotion state)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3.0) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001  (refused, not 2xx, not JSON)
        return False
    return isinstance(body, dict) and "dominant" in body


def wait_for(url: str, what: str, timeout_s: float, proc: subprocess.Popen | None = None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if http_ok(url):
            log(f"{what} ready ({time.time() - t0:.0f}s)")
            return True
        if proc is not None and proc.poll() is not None:
            log(f"{what} exited early with code {proc.returncode} — see logs/")
            return False
        time.sleep(1.0)
    log(f"{what} did not answer at {url} within {timeout_s:.0f}s (continuing; check logs/)")
    return False


def spawn(name: str, cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        logf = open(LOG_DIR / f"{name}.log", "ab", buffering=0)
    except PermissionError:
        # Windows: another process (e.g. a manually started copy) holds the log open
        logf = open(LOG_DIR / f"{name}.{os.getpid()}.log", "ab", buffering=0)
    logf.write(f"\n===== {name} start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
    e = dict(os.environ)
    e.update(env or {})
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("PYTHONUNBUFFERED", "1")
    if IS_WIN:
        extra: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        extra = {"start_new_session": True}
    p = subprocess.Popen(cmd, cwd=str(cwd), env=e, stdout=logf, stderr=subprocess.STDOUT, **extra)
    CHILDREN.append(p)
    log(f"started {name} (pid {p.pid}) → logs/{name}.log")
    return p


def kill_children() -> None:
    for p in CHILDREN:
        if p.poll() is not None:
            continue
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    p.terminate()
        except Exception:  # noqa: BLE001
            pass
    if not IS_WIN:   # give them some time to die
        deadline = time.time() + 5
        for p in CHILDREN:
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass


atexit.register(kill_children)


# ---- services ----
def stt_python() -> Path:
    v = ROOT / "services" / "stt" / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return v if v.exists() else Path(sys.executable)


def start_stt() -> subprocess.Popen | None:
    url = str(settings.get("stt.url"))
    port = port_of(url, 50000)
    if http_ok(f"{url}/health"):
        log(f"STT already running at {url}")
        return None
    if not port_free(port):
        log(f"port {port} busy but /health not answering — not starting STT")
        return None
    cwd = ROOT / "services" / "stt"
    if not (cwd / "models" / "model.pt").exists():
        log("STT: services/stt/models/model.pt missing — run scripts/download_sensevoice.py (STT disabled)")
        return None
    env = {"STT_HOST": "127.0.0.1", "STT_PORT": str(port), "STT_DEVICE": str(settings.get("stt.device", "auto")),
           "STT_THREADS": str(settings.get("stt.threads", 0) or ""),
           "STT_MODEL": str(cwd / "models")}
    return spawn("stt", [str(stt_python()), "api.py"], cwd, env)


def tts_layout() -> dict:
    """Where the voice server lives. ``kind`` is "venv" (vendor/tts-engine built by scripts/setup_tts.py),
    "legacy" (an older bundle with its own Runtime/python.exe + WebUI.py) or "missing"."""
    vdir = settings.path("tts.engine_dir")
    venv_py = vdir / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    legacy_py = vdir / "Runtime" / "python.exe"
    if venv_py.exists() and (vdir / "main.py").exists():
        return {"kind": "venv", "dir": vdir, "python": venv_py}
    if legacy_py.exists() and (vdir / "WebUI.py").exists():
        return {"kind": "legacy", "dir": vdir, "python": legacy_py}
    return {"kind": "missing", "dir": vdir, "python": venv_py}


def start_tts() -> subprocess.Popen | None:
    url = str(settings.get("tts.url"))
    port = port_of(url, 8002)
    if http_ok(f"{url}/ping"):
        log(f"TTS already running at {url}")
        return None
    if not port_free(port):
        log(f"port {port} busy but /ping not answering — not starting TTS")
        return None
    t = tts_layout()
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if t["kind"] == "venv":
        cmd = [str(t["python"]), "main.py", "--port", str(port), "--host", "127.0.0.1"]
        cid = str(settings.get("tts.character_id") or "")
        if cid and (t["dir"] / "Speakers" / cid / "character.json").exists():
            cmd += ["--character", cid]
        dev = str(settings.get("tts.device") or "auto")
        if dev != "auto":
            cmd += ["--device", dev]
        extra = settings.get("tts.extra_args") or []   # e.g. ["--batch-size", "1"] on a CPU-only machine
        if isinstance(extra, list):
            cmd += [str(a) for a in extra]
        return spawn("tts", cmd, t["dir"], env)
    if t["kind"] == "legacy":   # an older engine bundle: one worker, WebUI.py serves the same contract
        return spawn("tts", [str(t["python"]), "-B", "WebUI.py", "--port", str(port), "--host", "127.0.0.1"],
                     t["dir"], env)
    log(f"voice server not installed at {t['dir']} — run: python scripts/setup_tts.py (TTS disabled)")
    return None


def gsv_commit(gsv: Path) -> str:
    """Commit of the GPT-SoVITS checkout: detached HEAD, a branch ref (loose or packed), or the zip marker."""
    git_dir = gsv / ".git"
    try:
        head = (git_dir / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            if (git_dir / ref).is_file():
                head = (git_dir / ref).read_text().strip()
            elif (git_dir / "packed-refs").is_file():
                head = next((ln.split(" ", 1)[0] for ln in (git_dir / "packed-refs").read_text().splitlines()
                             if ln.endswith(" " + ref)), "")
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            return head
    except OSError:
        pass
    marker = gsv / ".opensakura-commit"
    try:
        return marker.read_text().strip() if marker.is_file() else ""
    except OSError:
        return ""


def tts_rows(note: str, required: bool) -> list[tuple[str, bool, str, bool]]:
    """--check rows for the voice server (see tts_layout)."""
    t = tts_layout()
    vdir: Path = t["dir"]
    cid = str(settings.get("tts.character_id") or "MekaHime_Sakura")
    spk = vdir / "Speakers" / cid
    rows: list[tuple[str, bool, str, bool]] = []
    if t["kind"] == "legacy":
        rows.append(("tts runtime (legacy bundle)", True, f"{t['python']}{note}", required))
    else:
        how = "" if t["kind"] == "venv" else " — run: python scripts/setup_tts.py"
        rows.append(("tts venv python", t["python"].exists(), f"{t['python']}{how}{note}", required))
        gsv = vdir / "GPT-SoVITS"
        commit = gsv_commit(gsv)[:10]
        rows.append(("tts GPT-SoVITS checkout", (gsv / "GPT_SoVITS" / "TTS_infer_pack" / "TTS.py").exists(),
                     f"{gsv}{' @ ' + commit if commit else ''}{note}", required))
        pm = gsv / "GPT_SoVITS" / "pretrained_models"
        need = [pm / "chinese-hubert-base" / "pytorch_model.bin", pm / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin",
                pm / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt", pm / "fast_langdetect" / "lid.176.bin",
                gsv / "GPT_SoVITS" / "text" / "G2PWModel" / "g2pW.onnx", vdir / "nltk_data" / "corpora" / "cmudict.zip"]
        missing = [f"{p.parent.name}/{p.name}" for p in need if not p.exists()]
        detail = ("HuBERT, RoBERTa, speaker-verification, G2PW, language ID, nltk data" if not missing else
                  f"missing {', '.join(missing[:2])}{f' +{len(missing) - 2} more' if len(missing) > 2 else ''}"
                  " — run: python scripts/setup_tts.py")
        rows.append(("tts pretrained models", not missing, detail + note, required))
        ff = vdir / "ffmpeg" / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
        ff_ok = ff.exists() or bool(shutil.which("ffmpeg"))
        rows.append(("tts ffmpeg (Opus only)", ff_ok, str(ff if ff.exists() else (shutil.which("ffmpeg") or
                     "not found: WAV still works")), False))
    files = [spk / "character.json", spk / "references" / "emotions.json"]
    try:
        meta = json.loads((spk / "character.json").read_text(encoding="utf-8-sig"))
        files += [spk / str(meta.get("llm_model") or f"{cid}.ckpt"), spk / str(meta.get("vocoder_model") or f"{cid}.pth")]
    except (OSError, ValueError):
        pass
    gone = [p.name for p in files if not p.exists()]
    rows.append((f"tts speaker {cid}", not gone and len(files) == 4,
                 (str(spk) if not gone and len(files) == 4 else f"{spk}: missing {', '.join(gone) or 'weights'}"
                  " — python scripts/setup_tts.py downloads it") + note, required))
    return rows


ROLES = ("actor", "director", "action", "mod")


def brain_rows() -> list[tuple[str, bool, str, bool]]:
    """--check rows for her brain. An OpenRouter key can still be pasted in Setup after the
    start, so a missing one is informational; with roles on Ollama, Ollama must answer and
    every model the roles use must be pulled."""
    roles = settings.get("llm.roles") or {}
    rows: list[tuple[str, bool, str, bool]] = []
    if any(roles.get(r, "openrouter") == "openrouter" for r in ROLES):
        key = bool(settings.secret("OPENROUTER_API_KEY"))
        rows.append(("OPENROUTER_API_KEY", key, "set" if key else
                     "not set yet: paste it in Setup after starting, or put it in .env (or switch the roles to Ollama)", False))
    else:
        rows.append(("OPENROUTER_API_KEY", True, "not needed (no role uses OpenRouter)", False))
    if any(roles.get(r) == "openai" for r in ROLES):
        base = str(settings.get("llm.openai.url") or "").strip().rstrip("/")
        for tail in ("/chat/completions", "/completions"):
            base = base[: -len(tail)] if base.endswith(tail) else base
        up = False
        if base:
            hdr = {"Authorization": f"Bearer {settings.secret('OPENAI_COMPAT_API_KEY')}"} if settings.secret("OPENAI_COMPAT_API_KEY") else {}
            try:   # any HTTP answer means something is listening
                urllib.request.urlopen(urllib.request.Request(base + "/models", headers=hdr), timeout=3).close()
                up = True
            except urllib.error.HTTPError as e:
                up = e.code not in (401, 403) and e.code < 500
            except Exception:  # noqa: BLE001
                up = False
        rows.append(("openai-compatible server", up, f"{base or '(url not set)'}" + ("" if up else
                     " not answering: start llama-server / LM Studio / your provider, or fix the URL in Setup → LLM"), True))
    wanted = sorted({str(settings.get(f"llm.ollama.{r}_model") or "") for r in ROLES if roles.get(r) == "ollama"} - {""})
    if wanted:
        url = str(settings.get("llm.ollama.url")).rstrip("/")
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=3) as r:
                have = {m.get("name", "") for m in json.load(r).get("models", [])}
        except Exception:  # noqa: BLE001
            rows.append(("ollama", False, f"not answering at {url}: install it from ollama.com and start it", True))
            return rows
        for m in wanted:
            pulled = m in have or f"{m}:latest" in have
            rows.append((f"ollama model {m}", pulled, "pulled" if pulled else f"missing → ollama pull {m}", True))
    return rows


def check() -> int:
    """Print one row per prerequisite. Rows for a disabled service (and live:* probes) are
    informational: a miss prints "--" and does not fail the check. So is the server port when
    OpenSakura itself is the one using it (the stack is running)."""
    ok = True
    rows: list[tuple[str, bool, str, bool]] = []   # (name, good, detail, required)
    stt_on = bool(settings.get("stt.enabled", True))
    tts_on = bool(settings.get("tts.enabled", True))
    _pv = sys.version_info[:2]
    rows.append(("python", _pv >= (3, 11), sys.version.split()[0] + ("" if _pv in ((3, 11), (3, 12)) else
                 "  (need 3.11 or 3.12)" if _pv < (3, 11) else "  (3.11 or 3.12 recommended)"), True))
    for mod in ("fastapi", "uvicorn", "httpx", "socketio", "pydantic", "multipart"):
        try:
            __import__(mod)
            rows.append((f"pip:{mod}", True, "ok", True))
        except ImportError:
            rows.append((f"pip:{mod}", False, "missing → pip install -r requirements.txt", True))
    rows.append(("config/sakura.json", settings.path("persona_file").exists(), str(settings.path("persona_file")), True))
    rows.extend(brain_rows())
    rows.append(("DISCORD_BOT_TOKEN", True, "set" if settings.secret("DISCORD_BOT_TOKEN") else "not set (web mode only)", True))
    stt_note = "" if stt_on else "  (STT disabled)"
    tts_note = "" if tts_on else "  (TTS disabled)"
    yp = stt_python()
    rows.append(("stt venv", (ROOT / "services/stt/.venv").exists(), str(yp) + stt_note, stt_on))
    rows.append(("stt model.pt", (ROOT / "services/stt/models/model.pt").exists(), "services/stt/models/model.pt" + stt_note, stt_on))
    rows.extend(tts_rows(tts_note, tts_on))
    for name, url in (("ollama", settings.get("llm.ollama.url") + "/api/tags"), ("stt", settings.get("stt.url") + "/health"), ("tts", settings.get("tts.url") + "/ping")):
        rows.append((f"live:{name}", http_ok(url), url, False))
    try:   # what is the speech service really running on?
        with urllib.request.urlopen(str(settings.get("stt.url")) + "/health", timeout=3) as r:
            h = json.loads(r.read().decode("utf-8", "replace"))
        rows.append(("stt device", True, f"{h.get('backend', '?')} · {h.get('device', '?')}" + (f" · {h['gpu_name']}" if h.get("gpu_name") else ""), False))
    except Exception:  # noqa: BLE001
        pass
    sp = int(settings.get("server.port", 8010))
    if port_free(sp):
        rows.append((f"server port {sp}", True, "free", True))
    elif opensakura_running(sp):   # the stack is already up: informational, not a failure
        rows.append((f"server port {sp}", False, "running (in use by OpenSakura: GET /api/state answers)", False))
    else:
        rows.append((f"server port {sp}", False, "in use", True))
    width = max(len(r[0]) for r in rows)
    for name, good, detail, required in rows:
        mark = "OK " if good else ("!! " if required else "-- ")
        print(f"  {mark}{name.ljust(width)}  {detail}")
        if not good and required:
            ok = False
    print("\nresult:", "ready" if ok else "fix the !! lines above (-- lines are informational)")
    return 0 if ok else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # messages can be non-ASCII; never die on a cp1252 console or a redirected log file
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="OpenSakura launcher")
    ap.add_argument("--check", action="store_true", help="validate the install and exit (honours --no-stt/--no-tts/--text-only)")
    ap.add_argument("--no-stt", action="store_true", help="do not start or use speech recognition")
    ap.add_argument("--no-tts", action="store_true", help="do not start or use the voice engine")
    ap.add_argument("--text-only", action="store_true", help="same as --no-stt --no-tts (no GPU needed)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default=None, help="bind address (default: server.host in settings, 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    if args.text_only:
        args.no_stt = args.no_tts = True
    # Disable the clients for this process only (settings.json is untouched), so the backend
    # neither connects to nor waits on services that were deliberately not started.
    overrides: dict = {}
    if args.no_stt:
        overrides["stt"] = {"enabled": False}
    if args.no_tts:
        overrides["tts"] = {"enabled": False}
    if overrides:
        settings.override(overrides)
        log("text-only: " + ", ".join(k.upper() + " off" for k in overrides))
    if args.check:
        return check()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    host = args.host or str(settings.get("server.host", "127.0.0.1"))
    port = args.port or int(settings.get("server.port", 8010))

    procs: list[tuple[str, str, subprocess.Popen | None, float]] = []
    if settings.get("stt.enabled", True) and settings.get("stt.autostart", True) and not args.no_stt:
        procs.append(("STT", settings.get("stt.url") + "/health", start_stt(), 240))
    if settings.get("tts.enabled", True) and settings.get("tts.autostart", True) and not args.no_tts:
        procs.append(("TTS", settings.get("tts.url") + "/ping", start_tts(), 420))

    def _open() -> None:
        if wait_for(f"http://{host}:{port}/api/state", "web server", 60):
            for what, url, p, to in procs:
                if p is not None:
                    wait_for(url, what, to, p)
            if not args.no_browser and settings.get("server.open_browser", True):
                webbrowser.open(f"http://{host}:{port}/")
            log(f"ready → http://{host}:{port}/")

    threading.Thread(target=_open, daemon=True).start()

    import uvicorn
    from server import app
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        kill_children()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
