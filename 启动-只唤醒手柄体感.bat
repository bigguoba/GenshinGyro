@echo off
rem Diagnostic: no virtual pad, just hold the physical pad in its full-report
rem mode and print live motion values.
setlocal
cd /d "%~dp0"
start "" /wait "GenshinGyro.exe" --cli --source ds4 --no-virtual-pad --verbose %*
echo.
pause
endlocal
