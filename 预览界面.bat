@echo off
rem Preview the window from the source tree (needs Python 3.12 on PATH).
rem
rem   preview.bat            -> real controller
rem   preview.bat --demo     -> synthetic data, no controller needed
rem   preview.bat --topmost  -> keep the window above everything
rem
rem Kept ASCII-only on purpose: cmd's code page is not reliable across systems,
rem so the Chinese lives in the file name and not in the echoed text.
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [Python not found]  Install Python 3.12 and add it to PATH.
    pause
    exit /b 1
)
python main.py %*
if errorlevel 1 pause
endlocal
