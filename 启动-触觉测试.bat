@echo off
cd /d "%~dp0"
title Haptics Probe
python tools\haptics_probe.py --verbose
echo.
pause
