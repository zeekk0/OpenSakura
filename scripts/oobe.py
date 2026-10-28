#!/usr/bin/env python3
"""OpenSakura first-run wizard (OOBE): look at this computer, recommend a setup, and do it for you.

    python scripts/oobe.py              # interactive (any Python 3.9+ runs this; it finds Python 3.11 / 3.12 for the install)
    python scripts/oobe.py --yes        # accept the recommendation without questions (never accepts her voice licence for you)
    python scripts/oobe.py --dry-run    # show what it would do, change nothing
    python scripts/oobe.py --detect     # only print what it found about this machine

Steps: 1 detect hardware -> 2 recommend a path -> 3 choose her brain -> 4 install (scripts/setup.py, scripts/setup_tts.py) -> 5 write the settings -> 6 run launch.py --check -> 7 offer to start her.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_setup():
    import importlib.util
    spec = importlib.util.spec_from_file_location("opensakura_setup", Path(__file__).with_name("setup.py"))
    mod = importlib.util.module_from_spec(spec)           # type: ignore[arg-type]
    spec.loader.exec_module(mod)                           # type: ignore[union-attr]
    return mod


SETUP = _load_setup()
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
ROLES = ("actor", "director", "action", "mod")
DRY = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str = "", yes: bool = False) -> str:
    if yes or not sys.stdin.isatty():
        say(f"{prompt} [{default}]  -> {default or '(skipped)'}")
        return default
    try:
        v = input(f"{prompt} [{default}] " if default else f"{prompt} ").strip()
    except EOFError:
        return default
    return v or default


def choose(title: str, options: list[tuple[str, str]], default: int, yes: bool) -> str:
    say(f"\n{title}")
    for i, (_, text) in enumerate(options, 1):
        say(f"  {i}. {text}" + ("   <- recommended" if i == default else ""))
    while True:
        v = ask("Your choice (number)", str(default), yes)
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        say("  please type one of the numbers above")


def run(cmd: list, what: str) -> int:
    cmd = [str(c) for c in cmd]
    say(f"\n>> {what}\n   $ " + " ".join(cmd))
    if DRY:
        return 0
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def venv_py() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")

# ------ step 1

def ram_gb() -> float:
    try:
        if IS_WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                            ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong)]
            s = MS(); s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))   # type: ignore[attr-defined]
            return s.total / 2**30
        if IS_MAC:
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
        m = re.search(r"MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text())
        return int(m.group(1)) / 2**20 if m else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def detect() -> dict:
    d: dict = {"os": platform.system(), "arch": platform.machine(), "ram_gb": round(ram_gb(), 1),
               "disk_free_gb": round(shutil.disk_usage(ROOT).free / 2**30, 1), "gpu": "none", "vram_gb": 0.0,
               "accel": "cpu", "python": None, "ollama": bool(shutil.which("ollama")),
               "llama_server": bool(shutil.which("llama-server")), "installed": venv_py().exists()}
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
            if out:
                name, mem = [x.strip() for x in out[0].split(",")[:2]]
                d.update(gpu=name, vram_gb=round(float(mem) / 1024, 1), accel="cuda")
        except Exception:  # noqa: BLE001
            pass
    if d["accel"] == "cpu" and IS_MAC and platform.machine() == "arm64":
        d.update(gpu="Apple Silicon GPU (shares memory with the CPU)", accel="mps", vram_gb=d["ram_gb"])
    if d["accel"] == "cpu" and not IS_WIN and not IS_MAC and (shutil.which("rocminfo") or shutil.which("rocm-smi")):
        d.update(gpu="AMD GPU (ROCm tools found)", accel="rocm")
    d["python"] = SETUP.find_python(None, required=False)    # Python 3.11 (preferred) or 3.12
    return d


# ------ step 2

def recommend(d: dict) -> tuple[str, str]:
    """-> (path key: text|local|voice, one-line reason)"""
    accel, vram, ram = d["accel"], d["vram_gb"], d["ram_gb"]
    if d["disk_free_gb"] < 15:
        return "text", f"only {d['disk_free_gb']} GB free disk: the voice needs ~12 GB, so start text-only"
    if accel == "cuda" and vram >= 6:
        return "voice", f"NVIDIA GPU with {vram} GB: her ears and voice run well on it"
    if accel == "mps" and ram >= 16:
        return "voice", f"Apple Silicon with {ram} GB: voice runs on the Mac GPU"
    if accel == "rocm":
        return "voice", "AMD GPU: ears work on ROCm, her voice is experimental there (falls back to CPU)"
    if ram >= 16:
        return "text", "no suitable GPU: the voice would be slow on CPU (you can add it later)"
    return "text", "modest hardware: text chat with a cloud brain is the smoothest start"


def ollama_model(d: dict, path: str) -> str:
    if d["accel"] == "cpu":   # CPU-only: replies get slow fast, so stay small
        return "qwen3:4b" if d["ram_gb"] >= 12 else "qwen3:1.7b"
    mem = d["vram_gb"]
    if path == "voice" and d["accel"] == "cuda":
        mem -= 6           # her ears + voice live on the same card
    return "qwen3:8b" if mem >= 10 else "qwen3:4b" if mem >= 5 else "qwen3:1.7b"


# ------ step 5

def load_settings() -> dict:
    p, ex = ROOT / "config" / "settings.json", ROOT / "config" / "settings.example.json"
    if not p.exists() and not DRY:
        shutil.copyfile(ex, p)
    return json.loads((p if p.exists() else ex).read_text(encoding="utf-8"))


def save_settings(s: dict) -> None:
    if not DRY:
        (ROOT / "config" / "settings.json").write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def set_env(name: str, value: str) -> None:
    if DRY:
        return
    p = ROOT / ".env"
    if not p.exists() and (ROOT / ".env.example").exists():
        shutil.copyfile(ROOT / ".env.example", p)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    pat = re.compile(rf"^\s*{re.escape(name)}\s*=")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------ main

def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(description="OpenSakura first-run wizard")
    ap.add_argument("--yes", action="store_true", help="accept the recommendation, ask nothing")
    ap.add_argument("--dry-run", action="store_true", help="show the steps, change nothing")
    ap.add_argument("--detect", action="store_true", help="only print what was detected")
    ap.add_argument("--path", choices=("text", "local", "voice"), help="skip the recommendation: text / local brain / full voice")
    ap.add_argument("--brain", choices=("openrouter", "ollama", "openai", "later"), help="skip the brain question")
    a = ap.parse_args()
    DRY = a.dry_run
    yes = a.yes

    say("=== OpenSakura first-run setup ===")
    d = detect()
    say("\nThis computer:")
    say(f"  system   {d['os']} {d['arch']}   RAM {d['ram_gb']} GB   free disk {d['disk_free_gb']} GB")
    say(f"  graphics {d['gpu']}" + (f"  ({d['vram_gb']} GB)" if d["accel"] == "cuda" else ""))
    say(f"  python 3.11/3.12: {d['python'] or 'NOT FOUND'}")
    say(f"  ollama: {'yes' if d['ollama'] else 'no'}   llama-server: {'yes' if d['llama_server'] else 'no'}")
    if a.detect:
        return 0
    if not d["python"]:
        hint = ("brew install python@3.11" if IS_MAC else "winget install Python.Python.3.11  (or python.org: tick 'Add python.exe to PATH')" if IS_WIN
                else "sudo apt install python3.11 python3.11-venv   (or your package manager / pyenv)")
        say("\nOpenSakura needs Python 3.11 (recommended) or 3.12. It can live beside the Python you already have; nothing is replaced.")
        if shutil.which("uv") and not yes and ask("Install Python 3.11 with uv now? (y/n)", "y").lower() in ("y", "yes"):
            run(["uv", "python", "install", "3.11"], "installing Python 3.11 with uv")
            d["python"] = SETUP.find_python(None, required=False)
        if not d["python"] and not DRY:
            say(f"Install it first: {hint}\nTip: `uv python install 3.11` works on every system. Then run this wizard again.")
            return 1
        d["python"] = d["python"] or "python3.11"

    # step 2
    rec, why = recommend(d)
    say(f"\nRecommendation: {rec.upper()}: {why}.")
    path = a.path or choose("What do you want?", [
        ("text", "TEXT ONLY: chat + 3D avatar, small install (about 1 GB), no GPU needed"),
        ("local", "TEXT + FREE LOCAL BRAIN: same, but the AI runs on your computer (Ollama / llama.cpp)"),
        ("voice", "FULL VOICE: she hears you and talks (about 12 GB download; fastest on an NVIDIA GPU)"),
    ], {"text": 1, "local": 2, "voice": 3}[rec], yes)

    # step 3
    default_brain = "ollama" if path == "local" else "openrouter"
    brain = a.brain or choose("Which brain should write her replies?", [
        ("openrouter", "OpenRouter (cloud, a few cents per hour of chat; needs an API key from openrouter.ai)"),
        ("ollama", "Ollama (free, on your computer; install from ollama.com)"),
        ("openai", "Another OpenAI-compatible server (llama.cpp, LM Studio, vLLM, ...)"),
        ("later", "Decide later in the page (Setup -> LLM)"),
    ], {"openrouter": 1, "ollama": 2, "openai": 3, "later": 4}[default_brain], yes)
    key = url = model = ""
    if brain == "openrouter":
        key = ask("Paste your OpenRouter API key (or press Enter to add it to .env later)", "", yes)
    elif brain == "ollama":
        model = ask("Ollama model to use", ollama_model(d, path), yes)
    elif brain == "openai":
        url = ask("Server URL (must end in /v1)", "http://127.0.0.1:8080/v1", yes)

    # summary
    say("\nPlan:")
    steps = [f"install the brain + page (scripts/setup.py{' --text-only' if path != 'voice' else ''})"]
    if path == "voice":
        steps.append(f"install ears (PyTorch for: {d['accel']}) and her voice (scripts/setup_tts.py, you will be asked to accept her licence)")
    if brain == "ollama":
        steps.append(f"pull {model} with Ollama" if d["ollama"] else "Ollama is not installed: install it from ollama.com, then run: ollama pull " + model)
    steps.append("write config/settings.json and .env, then run the health check")
    for i, st in enumerate(steps, 1):
        say(f"  {i}. {st}")
    if not yes and sys.stdin.isatty() and ask("Go ahead? (y/n)", "y").lower() not in ("y", "yes"):
        say("Nothing was changed.")
        return 0

    # step 4
    py = d["python"]
    cmd = [py, "scripts/setup.py"] + (["--text-only"] if path != "voice" else [])
    if run(cmd, "installing the brain and page" + (" and ears" if path == "voice" else "")) != 0:
        say("\nThe install step failed. Fix the message above, then run this wizard again: finished steps are skipped.")
        return 1
    if path == "voice":
        tts_dev = {"cuda": "cuda", "mps": "auto", "rocm": "rocm", "cpu": "cpu"}[d["accel"]]
        rc = run([venv_py() if not DRY else ".venv/bin/python", "scripts/setup_tts.py", "--device", tts_dev],
                 "installing her voice (type 'yes' when it shows the licence)")
        if rc != 0:
            say("\nHer voice did not finish installing. Text chat still works. Run later:  python scripts/setup_tts.py")
            path = "text"

    # step 5
    s = load_settings()
    if brain == "openrouter":
        if key:
            set_env("OPENROUTER_API_KEY", key)
        s["llm"]["roles"] = {r: "openrouter" for r in ROLES}
    elif brain == "ollama":
        s["llm"]["roles"] = {r: "ollama" for r in ROLES}
        for r in ROLES:
            s["llm"].setdefault("ollama", {})[f"{r}_model"] = model
        if d["ollama"]:
            run(["ollama", "pull", model], f"downloading {model} (a few GB)")
    elif brain == "openai":
        s["llm"]["roles"] = {r: "openai" for r in ROLES}
        s["llm"].setdefault("openai", {})["url"] = url
    save_settings(s)
    say("\nSettings written." if not DRY else "\n(dry run: settings not written)")

    # step 6
    say("\nHealth check:")
    run([venv_py() if not DRY else ".venv/bin/python", "launch.py", "--check"] + ([] if path == "voice" else ["--text-only"]), "launch.py --check")

    # step 7
    start = "Start_OpenSakura.bat" + ("" if path == "voice" else " --text-only")
    say(f"\nDone. Start her with:  {start}   then open http://localhost:8010")
    say("Stuck? docs/TROUBLESHOOTING.md. Speech on another machine? distributed/README.md.")
    if not yes and sys.stdin.isatty() and not DRY and ask("Start her now? (y/n)", "y").lower() in ("y", "yes"):
        os.execv(str(venv_py()), [str(venv_py()), "launch.py"] + ([] if path == "voice" else ["--text-only"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
