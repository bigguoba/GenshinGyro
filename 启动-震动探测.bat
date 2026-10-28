@echo off
cd /d "%~dp0"
title Rumble Probe
python tools\rumble_probe.py --menu
echo.
pause
