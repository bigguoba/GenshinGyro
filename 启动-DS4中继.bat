@echo off
rem Console bridge forced to the physical DualShock 4 (full relay).
setlocal
cd /d "%~dp0"
start "" /wait "GenshinGyro.exe" --cli --source ds4 %*
if errorlevel 1 (
    echo.
    echo [The program exited with code %errorlevel%.]
    echo If the pad is paired but idle, press the PS button to wake it.
    echo.
    pause
)
endlocal
