@echo off
rem Console bridge, motion only + Share as the touchpad click: the combination
rem that works in Genshin Impact.
setlocal
cd /d "%~dp0"
start "" /wait "GenshinGyro.exe" --cli --source ds4 --motion-only %*
if errorlevel 1 (
    echo.
    echo [The program exited with code %errorlevel%.]
    echo If the pad is paired but idle, press the PS button to wake it.
    echo.
    pause
)
endlocal
