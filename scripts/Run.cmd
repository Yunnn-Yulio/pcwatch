@echo off
setlocal
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0..\PCWatch.pyw" %*
    exit /b 0
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0..\PCWatch.pyw" %*
    exit /b 0
)
echo Python was not found. Install Python 3.11 or newer and add it to PATH.
pause
exit /b 1
