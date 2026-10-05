@echo off
setlocal
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%~dp0..\PCWatch.pyw" --install-startup %*
) else (
    python.exe "%~dp0..\PCWatch.pyw" --install-startup %*
)
if errorlevel 1 (
    echo Startup installation failed. See README.md for troubleshooting.
    pause
    exit /b 1
)
echo PCWatch will start at your next Windows sign-in. Keep this folder in place.
pause
