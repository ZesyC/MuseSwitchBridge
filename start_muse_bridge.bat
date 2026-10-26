@echo off
setlocal
title MuseBridge
cd /d "%~dp0"
echo Starting MuseBridge...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" muse_bridge.py serve
) else (
    python muse_bridge.py serve
)
pause
