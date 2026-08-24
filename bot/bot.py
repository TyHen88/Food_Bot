"""
Application factory + helpers for the Telegram Food Poll Bot.

Phase 0 change: the FastAPI app (main.py) owns the lifecycle. This module
now exposes a build_application() factory used by both webhook mode
(production) and an optional polling fallback (local dev when WEBHOOK_URL
is empty).
"""

import logging

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
)
from telegram.ext import Application
from telegram.request import HTTPXRequest

from .config import BOT_TOKEN, setup_logging
from .handlers import setup_handlers
from .scheduler import setup_scheduler

logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    # Member-facing
    BotCommand("start", "Welcome & instructions"),
    BotCommand("app", "Open the Food Bot mini app"),
    BotCommand("subscribe", "Subscribe this chat to reminders"),
    BotCommand("unsubscribe", "Unsubscribe this chat from reminders"),
    BotCommand("vongsa", "Pay Vongsa Hourt (KHQR)"),
    BotCommand("ty", "Pay Ty Hen (KHQR)"),
    BotCommand("ai", "Ask about your orders, invoices & how to use the bot"),
    BotCommand("exchange_rate", "NBC official USD/KHR rate"),
    # Admin-only (decorator rejects non-admins)
    # NOTE: /admin is intentionally NOT listed here — it still works if typed
    # (handler stays registered), but it's hidden from the command menu.
    BotCommand("set", "Update a setting key"),
    BotCommand("setup_payment_bot", "Setup payment bot & target group"),
    BotCommand("schedule_list", "List configured schedules"),
    BotCommand("schedule_enable", "Enable a schedule"),
    BotCommand("schedule_disable", "Disable a schedule"),
]


async def _post_init(app: Application) -> None:
    """Runs after PTB is initialised but before updates start flowing."""
    # Telegram resolves the most specific command scope first, so a stale
    # per-scope list (e.g. one set before /app existed) shadows the default
    # everywhere it applies. Clear the narrower scopes so the default list
    # below — which includes /app — is what users actually see. Then set the
    # default, and also set it explicitly on the two broad scopes so the menu
    # is correct regardless of what was there before.
    for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
        try:
            await app.bot.delete_my_commands(scope=scope)
        except Exception as e:
            logger.warning(f"delete_my_commands({scope.type}) failed: {e}")

    await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
    cmds = ", ".join("/" + c.command for c in BOT_COMMANDS)
    logger.info(f"Registered {len(BOT_COMMANDS)} bot commands (default scope): {cmds}")

    await setup_scheduler(app)
    logger.info("Bot commands and scheduler registered")


def build_application() -> Application:
    """
    Build a fully configured PTB Application without starting it.

    The caller (FastAPI lifespan in main.py, or run_polling for local dev)
    is responsible for starting and stopping it.

    Note: PTB's built-in JobQueue is disabled because PTB 20.1 has a weakref
    crash on Python 3.14. All scheduling goes through APScheduler in
    bot.scheduler.
    """
    setup_logging()

    # Increase connect / read timeouts beyond the 5 s default.
    # The local network can reach api.telegram.org but TLS handshake can
    # take > 5 s on first connect; 30 s covers any realistic latency.
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
        http_version="1.1",
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .job_queue(None)
        .post_init(_post_init)
        .build()
    )

    setup_handlers(application)
    logger.info("Application built and handlers registered")
    return application
