#!/usr/bin/env bash
:<<'::BATCH'
@echo off
setlocal
chcp 65001 >nul
title OpenSakura
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launch.py %*
) else (
  echo [!] .venv not found. run Install_OpenSakura.bat first ^(or run: python scripts\setup.py^).
  python launch.py %*
)
if errorlevel 1 pause
exit /b
::BATCH

cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python launch.py "$@"
fi
echo "[!] .venv not found. Run: bash scripts/setup.bat   (or: python3 scripts/setup.py)" >&2
exec python3 launch.py "$@"
