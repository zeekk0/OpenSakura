#!/usr/bin/env bash
:<<'::BATCH'
@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 scripts\oobe.py %* & goto :end)
where python >nul 2>nul && (python scripts\oobe.py %* & goto :end)
echo.
echo Python was not found. Install Python 3.11 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run me again
:end
echo.
pause
exit /b
::BATCH

cd "$(dirname "$0")" || exit 1
for c in python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null; then
    "$c" scripts/oobe.py "$@"
    echo; read -r -p "Press Enter to close..." _
    exit 0
  fi
done
echo "Python not found. Install it:  brew install python@3.12   (macOS)   or   sudo apt install python3 python3-venv   (Debian/Ubuntu)"
read -r -p "Press Enter to close..." _
