@echo off
rem Build a standalone GenshinGyro.exe with PyInstaller.
rem
rem   build_exe.bat            -> dist\GenshinGyro\GenshinGyro.exe (onedir)
rem   build_exe.bat onefile    -> dist\GenshinGyro.exe (single file)
rem   build_exe.bat console    -> build WITH a console window (debugging only)
rem
rem Without "console" the build is --windowed: double-clicking the exe opens only
rem the graphical window.  The console modes (--cli) attach themselves to the
rem console that launches them, so the .bat launchers still show their output.
rem
rem The icon is used twice on purpose: --icon embeds it in the exe (Explorer,
rem shortcuts), --add-data ships it so the window can load it at run time for the
rem title bar and the task bar.
setlocal
cd /d "%~dp0"

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found.  Install it with:  python -m pip install pyinstaller
    pause
    exit /b 1
)

set MODE=--onedir
if /i "%~1"=="onefile" set MODE=--onefile
set WINDOW=--windowed
if /i "%~1"=="console" set WINDOW=--console
if /i "%~2"=="console" set WINDOW=--console

python -m PyInstaller --noconfirm --clean %MODE% ^
    --name GenshinGyro ^
    --icon "assets\genshingyro.ico" ^
    --add-data "assets\genshingyro.ico;assets" ^
    --add-binary "vendor\ViGEmClient.dll;vendor" ^
    %WINDOW% ^
    main.py

echo.
if /i "%~1"=="onefile" (
    echo Built dist\GenshinGyro.exe
) else (
    echo Built dist\GenshinGyro\GenshinGyro.exe
)
pause
endlocal
