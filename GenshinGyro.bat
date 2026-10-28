@echo off
rem Launcher for the development tree.  Use the launcher scripts in the release
rem folder when running a packaged copy.
rem
rem   (no argument)  open the graphical window
rem   cli            run the plain console bridge
setlocal
cd /d "%~dp0"

if /i "%~1"=="cli" goto :cli

if exist "dist\GenshinGyro\GenshinGyro.exe" (
    "dist\GenshinGyro\GenshinGyro.exe" --gui
    goto :end
)
python main.py --gui
goto :end

:cli
if exist "dist\GenshinGyro\GenshinGyro.exe" (
    "dist\GenshinGyro\GenshinGyro.exe"
    goto :end
)
python main.py

:end
endlocal
