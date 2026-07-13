@echo off
setlocal

REM Run from the script's own directory so relative paths (assets/, data/, .env) resolve correctly.
cd /d "%~dp0"

REM Prefer the project virtualenv if it exists; otherwise fall back to system python.
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [run-bot] .venv not found, falling back to system python.
    set "PYTHON=python"
)

if not exist ".env" (
    echo [run-bot] WARNING: .env not found. BOT_TOKEN must be set or the bot will exit.
)

echo [run-bot] Starting Food Bot...
"%PYTHON%" main.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo [run-bot] Bot exited with code %EXITCODE%.
pause
exit /b %EXITCODE%
