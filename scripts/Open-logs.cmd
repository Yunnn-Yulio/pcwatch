@echo off
if not exist "%LOCALAPPDATA%\PCWatch\Logs" (
    echo No default logs yet. Run PCWatch first.
    pause
    exit /b 1
)
explorer.exe "%LOCALAPPDATA%\PCWatch\Logs"
