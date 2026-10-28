@echo off
cd /d "%~dp0"
title Virtual DualSense (audio endpoint)
python tools\viiper_audio_device.py --start-server
echo.
pause
