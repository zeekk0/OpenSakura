#!/usr/bin/env bash
:<<"::BATCH"
@echo off
setlocal
cd /d "%~dp0\.."

python3.11 --version >nul 2>&1
if %errorlevel% equ 0 (
    python3.11 scripts\setup.py %*
    exit /b %errorlevel%
)
python --version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    echo %PY_VER% | findstr /r "^3\.11\. ^3\.12\." >nul
    if %errorlevel% equ 0 (
        python scripts\setup.py %*
        exit /b %errorlevel%
    )
)
echo [setup] Python 3.11 not found. >&2
echo         Install it from python.org or via winget: >&2
echo         winget install Python.Python.3.11 >&2
exit /b 1
::BATCH

set -euo pipefail
cd "$(dirname "$0")/.."
for c in python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),((3,12)) else 1)' 2>/dev/null; then
    exec "$c" scripts/setup.py "$@"
  fi
done
echo "[setup] Python 3.11 not found." >&2
case "$(uname -s)" in
  Darwin) echo "        Install it with:  brew install python@3.11" >&2 ;;
  *)      echo "        Debian/Ubuntu:  sudo apt install python3.11 python3.11-venv    (others: your package manager, or pyenv)" >&2 ;;
esac
exit 1
