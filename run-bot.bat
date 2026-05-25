@echo off
REM ─────────────────────────────────────────────────────────────
REM  Launch the Telegram Food Poll Bot locally (polling mode).
REM  Double-click this file, or run it from a terminal.
REM ─────────────────────────────────────────────────────────────

REM Always run from the folder this script lives in.
cd /d "%~dp0"

REM Prefer the project virtual environment if it exists.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

REM Warn if there's no .env (BOT_TOKEN is required).
if not exist ".env" (
    echo [WARNING] No .env file found. The bot needs BOT_TOKEN set in .env.
    echo           Copy env.example to .env and fill it in.
    echo.
)

echo Starting Food Bot with %PYTHON% ...
echo Press Ctrl+C to stop.
echo.

"%PYTHON%" main.py

echo.
echo Bot has stopped.
pause
