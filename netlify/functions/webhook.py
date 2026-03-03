"""
Netlify serverless function — Telegram Webhook Handler
======================================================
Receives POST requests from Telegram and dispatches them
through the existing bot handler logic.

Deploy steps:
  1. Push this repo to Netlify (it reads netlify.toml automatically)
  2. Set BOT_TOKEN in Netlify dashboard → Site settings → Environment variables
  3. After first deploy, register the webhook with Telegram once:
       https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-site>.netlify.app/webhook
"""

import json
import os
import logging
import asyncio
import sys

# ── Make the repo root importable ──────────────────────────────────────────────
# Netlify runs functions from the repo root, so 'bot' package is already on the
# path.  This guard just ensures it works in all environments.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from telegram import Update, BotCommand
from telegram.ext import Application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Lazy-initialise the Application once per cold start ───────────────────────
_app: Application | None = None


async def _get_app() -> Application:
    """Build and initialise the PTB Application (once per Lambda cold start)."""
    global _app
    if _app is not None:
        return _app

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    # Import handlers from the existing bot package
    from bot.handlers import setup_handlers

    async def post_init(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("start",  "👋 Welcome & instructions"),
            BotCommand("vongsa", "💳 Pay Vongsa Hourt (KHQR)"),
            BotCommand("ty",     "💳 Pay Ty Hen (KHQR)"),
        ])

    _app = (
        Application.builder()
        .token(token)
        .job_queue(None)          # no scheduler needed in webhook mode
        .post_init(post_init)
        .build()
    )

    setup_handlers(_app)

    await _app.initialize()
    logger.info("Bot application initialised")
    return _app


# ── Netlify Function entry point ───────────────────────────────────────────────
def handler(event, context):
    """
    Netlify function handler.
    Telegram sends a POST with a JSON body containing the Update object.
    """
    # Only accept POST
    if event.get("httpMethod") != "POST":
        return {"statusCode": 405, "body": "Method Not Allowed"}

    # Parse the incoming Telegram update
    try:
        body = event.get("body") or "{}"
        update_data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Failed to parse request body: {e}")
        return {"statusCode": 400, "body": "Bad Request"}

    # Process update synchronously (Netlify functions are sync by default)
    try:
        asyncio.run(_process_update(update_data))
    except Exception as e:
        logger.error(f"Error processing update: {e}", exc_info=True)
        # Always return 200 to Telegram so it doesn't retry endlessly
        return {"statusCode": 200, "body": "error handled"}

    return {"statusCode": 200, "body": "ok"}


async def _process_update(update_data: dict) -> None:
    """Parse and dispatch a single Telegram update."""
    app = await _get_app()
    update = Update.de_json(update_data, app.bot)
    await app.process_update(update)
