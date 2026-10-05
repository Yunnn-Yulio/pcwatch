@echo off
setlocal
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%~dp0..\PCWatch.pyw" --stop %*
    exit /b
)
python.exe "%~dp0..\PCWatch.pyw" --stop %*
