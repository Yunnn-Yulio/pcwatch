@echo off
setlocal
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%~dp0..\PCWatch.pyw" --remove-startup %*
) else (
    python.exe "%~dp0..\PCWatch.pyw" --remove-startup %*
)
if errorlevel 1 (
    echo Startup removal failed. See README.md for troubleshooting.
    pause
    exit /b 1
)
echo PCWatch startup has been disabled and the monitor was asked to stop.
pause
