@echo off
rem Console bridge, automatic input source.  "start /wait" matters: the exe is
rem built without its own console, so it attaches to this window instead.
setlocal
cd /d "%~dp0"
start "" /wait "GenshinGyro.exe" --cli %*
if errorlevel 1 (
    echo.
    echo [The program exited with code %errorlevel%.]
    echo Check the quick-start .txt: ViGEmBus driver installed, controller awake,
    echo HidHide whitelist if you use HidHide.
    echo.
    pause
)
endlocal
