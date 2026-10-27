@echo off
rem Open the window (same as double-clicking the exe).  Started through this
rem script so Explorer never hands the unsigned exe straight to SmartScreen.
setlocal
cd /d "%~dp0"
start "" "GenshinGyro.exe"
endlocal
