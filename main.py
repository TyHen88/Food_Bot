#!/usr/bin/env python3
"""
FastAPI entry point for the Telegram Food Poll Bot.

Production (WEBHOOK_URL set): runs FastAPI under uvicorn; Telegram pushes
updates to POST /webhook. APScheduler runs in the same event loop.

Local dev (WEBHOOK_URL empty): start with `python main.py` to fall back to
long polling (the legacy mode), so you can iterate without ngrok.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from telegram import Update

from bot import build_application
from bot.api import api_router
from bot.config import (
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    setup_logging,
)
from bot.sheets import is_configured as sheets_configured
from bot.sheets.bootstrap import bootstrap as sheets_bootstrap
from bot.sheets.cache import cache as sheets_cache

setup_logging()
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"


class NoCacheStaticFiles(StaticFiles):
    """Serve the Mini App with ``Cache-Control: no-cache`` so WebViews (notably
    Telegram's) revalidate every load instead of pinning a stale app.js/HTML.
    ETag/Last-Modified still yield cheap 304s when nothing changed."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def _webhook_watchdog(application, expected_url: str, secret_token: str | None,
                            interval: int = 60) -> None:
    """Re-register the webhook if Telegram ever reports it missing/wrong.

    A dying old container during a rolling deploy — or an accidental local
    polling run on the same token — can delete the webhook out from under a
    healthy instance. Without this, the bot silently stops receiving updates
    until someone redeploys or revokes the token. This loop checks every
    `interval`s and re-sets the webhook if it has drifted from expected_url.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            info = await application.bot.get_webhook_info()
            if info.url != expected_url:
                kwargs = {"url": expected_url, "drop_pending_updates": False}
                if secret_token:
                    kwargs["secret_token"] = secret_token
                await application.bot.set_webhook(**kwargs)
                logger.warning(
                    f"Webhook was '{info.url or '(empty)'}', expected "
                    f"'{expected_url}'. Re-registered by watchdog."
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Webhook watchdog check failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the PTB application alongside FastAPI; tear it down on shutdown."""

    # Sheets bootstrap (no-op if credentials missing — safe for local dev).
    if sheets_configured():
        try:
            await sheets_bootstrap()
            sheets_cache.start_refresh_loop()
        except Exception as e:
            logger.error(f"Sheets bootstrap failed: {e}. Continuing without Sheets.")
    else:
        logger.warning(
            "Sheets not configured (GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_JSON empty) "
            "— skipping bootstrap. Bot will fall back to in-memory state."
        )

    application = build_application()
    app.state.application = application

    await application.initialize()
    # PTB only invokes post_init from run_polling()/run_webhook(), which we
    # don't use — FastAPI owns the lifecycle and we drive initialize()/start()
    # manually. Without this call, _post_init never runs, so the command menu
    # (set_my_commands) and the APScheduler reminder jobs are never set up.
    if getattr(application, "post_init", None):
        await application.post_init(application)
    await application.start()

    polling_active = False
    watchdog_task = None
    if WEBHOOK_URL:
        full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        kwargs = {"url": full_url, "drop_pending_updates": True}
        if WEBHOOK_SECRET:
            kwargs["secret_token"] = WEBHOOK_SECRET
        await application.bot.set_webhook(**kwargs)
        logger.info(f"Webhook registered at {full_url}")
        # Self-heal if the webhook gets deleted (e.g. an old container's
        # shutdown during a rolling deploy). See _webhook_watchdog.
        watchdog_task = asyncio.create_task(
            _webhook_watchdog(application, full_url, WEBHOOK_SECRET or None)
        )
    else:
        # Local dev: no public HTTPS URL → start long polling inside the
        # FastAPI process so the bot still receives updates while serving
        # the Mini App on localhost.
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await application.updater.start_polling(drop_pending_updates=True)
        polling_active = True
        logger.info(
            "WEBHOOK_URL empty — long polling started inside FastAPI process. "
            "Bot will receive Telegram updates."
        )

    try:
        yield
    finally:
        if watchdog_task:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if polling_active:
            try:
                await application.updater.stop()
            except Exception as e:
                logger.warning(f"Failed to stop polling on shutdown: {e}")
        # NOTE: deliberately do NOT delete the webhook here. On a rolling
        # deploy the new container sets the webhook before the old one shuts
        # down; deleting it on shutdown would race and remove the webhook the
        # new instance just registered, leaving the bot unable to receive any
        # updates (the "bot dies on every deploy" bug). The webhook URL is
        # stable, so the next startup re-sets it idempotently. Polling mode
        # (WEBHOOK_URL empty) already clears the webhook on startup.
        await application.stop()
        await application.shutdown()
        await sheets_cache.stop_refresh_loop()


app = FastAPI(title="Food Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Receive updates from Telegram and hand them to PTB."""
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    application = request.app.state.application
    payload = await request.json()
    update = Update.de_json(payload, application.bot)
    try:
        await application.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update in telegram_webhook: {e}", exc_info=True)
    return Response(status_code=200)


# Mount API routes BEFORE the static catch-all so /api/* isn't shadowed.
app.include_router(api_router)

if FRONTEND_DIR.exists():
    app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning(f"Frontend directory not found at {FRONTEND_DIR} — Mini App will 404")


if __name__ == "__main__":
    import uvicorn

    # Always run uvicorn so the Mini App is reachable on http://127.0.0.1:8000.
    # The lifespan handler picks webhook (if WEBHOOK_URL is set) or long polling
    # (if empty) — the HTTP server runs either way.
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
