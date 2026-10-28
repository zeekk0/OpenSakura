#!/usr/bin/env python3
"""OpenSakura one-time setup for Windows, Linux and macOS (Python 3.11 recommended, 3.12 also works).

    python scripts/setup.py                  # detect your hardware and set everything up
    python scripts/setup.py --text-only      # just the brain + 3D page: no speech services, no big downloads
    python scripts/setup.py --device cpu     # force a PyTorch flavour for the speech-recognition service
    python scripts/setup.py --dry-run        # print what would run, change nothing

What it does
Step
  1. creates .venv and installs requirements.txt (the brain / web server)
  2. creates services/stt/.venv and installs the right PyTorch for your hardware + the STT requirements
  3. downloads the speech-recognition weights (~936 MB, services/stt/models/model.pt)
  4. copies .env.example -> .env and config/settings.example.json -> config/settings.json
Her voice (text-to-speech) is separate go run python scripts/setup_tts.py
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
TORCH_INDEX = {
    "cuda": "https://download.pytorch.org/whl/cu128",   # CUDA 12.8 builds (also required for RTX 50-series)
    "rocm": "https://download.pytorch.org/whl/rocm6.4", # AMD, Linux only. Check pytorch.org for the newest ROCm tag.
    "xpu": "https://download.pytorch.org/whl/xpu",      # Intel Arc / Data Center GPUs
    "cpu": "https://download.pytorch.org/whl/cpu",      # small CPU-only wheels (Windows/Linux)
    "mps": None,                                        # macOS: the default PyPI wheels already include Apple GPU support
}
DRY = False


def say(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WIN else "bin/python")


def run(cmd: list, what: str) -> None:
    cmd = [str(c) for c in cmd]
    say(what)
    if DRY:
        print("    $ " + " ".join(cmd))
        return
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        sys.exit(f"[setup] {what} failed (exit code {r.returncode}). Fix the error above, then run this script again.")


SUPPORTED = ((3, 11), (3, 12))
PREFERRED = ("3.11", "3.12")


def py_version(cmd: list) -> tuple[int, int] | None:
    try:
        out = subprocess.run([*map(str, cmd), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        major, minor = out.split(".")
        return int(major), int(minor)
    except Exception:  # noqa: BLE001
        return None


def _exe_of(cmd: list) -> str | None:
    try:
        out = subprocess.run([*map(str, cmd), "-c", "import sys;print(sys.executable)"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return out or None
    except Exception:  # noqa: BLE001
        return None


def python_candidates() -> list[list]:
    c: list[list] = [[sys.executable]]
    for v in PREFERRED:
        if shutil.which(f"python{v}"):
            c.append([f"python{v}"])
        if IS_WIN and shutil.which("py"):
            c.append(["py", f"-{v}"])
    uv = shutil.which("uv")
    if uv:
        for v in PREFERRED:
            try:
                p = subprocess.run([uv, "python", "find", v], capture_output=True, text=True, timeout=30).stdout.strip()
                if p:
                    c.append([p])
            except Exception:  # noqa: BLE001
                pass
    for n in ("python3", "python"):
        if shutil.which(n):
            c.append([n])
    return c


def find_python(explicit: str | None, required: bool = True) -> str | None:
    if explicit:
        v = py_version([explicit])
        if v in SUPPORTED:
            return explicit
        if required:
            sys.exit(f"[setup] {explicit} is {'Python %d.%d' % v if v else 'not a working Python'}; OpenSakura needs Python 3.11 (recommended) or 3.12.")
        return None
    found = [(py_version(cmd), cmd) for cmd in python_candidates()]
    for want in SUPPORTED:
        for v, cmd in found:
            if v == want:
                return _exe_of(cmd) or cmd[-1]
    if required:
        have = next((v for v, _ in found if v), None)
        hint = ("brew install python@3.11" if IS_MAC else
                "sudo apt install python3.11 python3.11-venv   (Debian/Ubuntu; other distros: your package manager or pyenv)" if not IS_WIN else
                "install it from python.org and tick 'Add python.exe to PATH' (or: winget install Python.Python.3.11)")
        sys.exit(f"[setup] OpenSakura needs Python 3.11 (recommended) or 3.12" + (f"; the Python found here is {have[0]}.{have[1]}" if have else "") + f".\n"
                 f"        Install it (it can live beside your current Python, nothing is replaced): {hint}\n"
                 "        Easiest on any system: `uv python install 3.11` (https://docs.astral.sh/uv/). Then re-run, or pass --python /path/to/python.")
    return None


def detect_device() -> str:
    if IS_MAC:
        return "mps" if platform.machine() == "arm64" else "cpu"
    if shutil.which("nvidia-smi"):
        return "cuda"
    if not IS_WIN and (shutil.which("rocminfo") or shutil.which("rocm-smi") or Path("/opt/rocm").exists()):
        return "rocm"
    return "cpu"   # Intel Arc users: pass --device xpu


def check_device(device: str) -> None:
    if device == "rocm" and (IS_WIN or IS_MAC):
        sys.exit("[setup] ROCm PyTorch wheels exist for Linux only. Use --device cpu here.")
    if device == "mps" and not IS_MAC:
        sys.exit("[setup] --device mps is for macOS only.")
    if device == "cuda" and IS_MAC:
        sys.exit("[setup] CUDA does not exist on macOS. Use --device mps (Apple silicon) or cpu.")
    if device == "xpu" and IS_MAC:
        sys.exit("[setup] Intel XPU is for Windows/Linux. Use --device mps or cpu on macOS.")


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(description="OpenSakura setup (Windows / Linux / macOS)")
    ap.add_argument("--device", choices=("auto", "cuda", "rocm", "xpu", "mps", "cpu"), default="auto",
                    help="PyTorch flavour for speech recognition (default: auto-detect)")
    ap.add_argument("--torch-index", default=None, help="override the PyTorch wheel index URL (advanced)")
    ap.add_argument("--text-only", action="store_true", help="set up only the brain/web server (no STT venv, no model download)")
    ap.add_argument("--skip-model", action="store_true", help="do not download the SenseVoice weights")
    ap.add_argument("--python", default=None, help="path to a Python 3.11 / 3.12 interpreter (default: the best one found)")
    ap.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    args = ap.parse_args()
    DRY = args.dry_run

    os.chdir(ROOT)
    py = find_python(args.python)
    say(f"using Python at {py}")
    device = detect_device() if args.device == "auto" else args.device
    check_device(device)
    if not args.text_only:
        say(f"speech-recognition PyTorch flavour: {device}" + ("  (auto-detected; change with --device)" if args.device == "auto" else ""))

    # step 1
    venv = ROOT / ".venv"
    if not venv_python(venv).exists():
        run([py, "-m", "venv", venv], "creating .venv")
    vp = venv_python(venv)
    run([vp, "-m", "pip", "install", "-U", "pip", "wheel"], "upgrading pip (.venv)")
    run([vp, "-m", "pip", "install", "-r", "requirements.txt"], "installing requirements.txt")

    # step 2+3
    if not args.text_only:
        yv = ROOT / "services" / "stt" / ".venv"
        if not venv_python(yv).exists():
            run([py, "-m", "venv", yv], "creating services/stt/.venv")
        yp = venv_python(yv)
        run([yp, "-m", "pip", "install", "-U", "pip", "wheel"], "upgrading pip (STT venv)")
        index = args.torch_index or TORCH_INDEX[device]
        cmd = [yp, "-m", "pip", "install", "torch", "torchaudio"] + (["--index-url", index] if index else [])
        run(cmd, f"installing PyTorch ({device}) for speech recognition (large download)")
        run([yp, "-m", "pip", "install", "-r", "services/stt/requirements.txt"], "installing the STT requirements")
        model = ROOT / "services" / "stt" / "models" / "model.pt"
        if not args.skip_model and not model.exists():
            run([vp, "scripts/download_sensevoice.py"], "downloading the speech-recognition model (~936 MB)")
            if not DRY and not model.exists():
                sys.exit("[setup] services/stt/models/model.pt is missing after the download. Run this script again.")

    # step 4
    for src, dst in ((".env.example", ".env"), ("config/settings.example.json", "config/settings.json")):
        if not (ROOT / dst).exists():
            say(f"creating {dst}")
            if not DRY:
                shutil.copyfile(ROOT / src, ROOT / dst)

    py_cmd = r".venv\Scripts\python" if IS_WIN else ".venv/bin/python"
    tts_dev = {"cuda": "cuda", "mps": "auto", "rocm": "rocm", "xpu": "xpu", "cpu": "cpu"}[device]
    print("\n[setup] done. Next:")
    if args.text_only:
        print("  1. give her a brain: an OpenRouter key in .env, or a local model (see docs/GETTING_STARTED.md)")
        print(f"  2. {py_cmd} launch.py --text-only   ->  http://localhost:8010")
    else:
        print(f"  1. {py_cmd} scripts/setup_tts.py --device {tts_dev}   (her voice; type yes to accept her personal-use licence)")
        print("  2. give her a brain: an OpenRouter key in .env, or a local model (see docs/GETTING_STARTED.md)")
        print(f"  3. {py_cmd} launch.py --check, then start: Start_OpenSakura.bat -> http://localhost:8010")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
