@echo off
cd /d "%~dp0"
title Haptics Diagnostic (no rumble output)
python tools\haptics_probe.py --verbose --no-rumble
echo.
pause
