#!/bin/bash

# Run from the script's own directory so relative paths (assets/, data/, .env) resolve correctly.
cd "$(dirname "$0")"

# Prefer the project virtualenv if it exists; otherwise fall back to system python.
PYTHON="venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    echo "[run-bot] venv not found, falling back to system python."
    PYTHON="python3"
fi

if [ ! -f ".env" ]; then
    echo "[run-bot] WARNING: .env not found. BOT_TOKEN must be set or the bot will exit."
fi

echo "[run-bot] Starting Food Bot..."
"$PYTHON" main.py
EXITCODE=$?

echo
echo "[run-bot] Bot exited with code $EXITCODE."
exit $EXITCODE
