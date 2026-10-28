@echo off
cd /d "%~dp0"
title Audio Rumble
python tools\audio_rumble.py --menu
echo.
pause
