"""
Admin-only gate for command handlers.

A user is treated as admin ONLY when the `user` tab row for their
user_id has role='ADMIN'. The env-var ADMIN_USER_IDS is no longer
consulted here — it remains in config solely for the local-dev
DEV_BYPASS_AUTH shortcut in bot/api/auth.py.

To grant admin: set role='ADMIN' on that user's row in the spreadsheet.
The cache picks up the change on its next refresh tick (or restart).
"""

import functools
import logging
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from .sheets import repo
from .sheets.client import is_configured

logger = logging.getLogger(__name__)


async def is_admin(user_id: int) -> bool:
    if not is_configured():
        return False
    row = await repo.find_by_pk("user", user_id)
    return bool(row) and str(row.get("role", "")).upper() == "ADMIN"


def admin_only(func: Callable) -> Callable:
    """Reject the command with a polite message if the caller isn't admin."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return
        if not await is_admin(user.id):
            try:
                await update.message.reply_text(
                    "This command is admin-only. Ask an admin to grant you the ADMIN role."
                )
            except Exception:
                pass
            logger.info(f"Rejected admin command '{func.__name__}' from non-admin {user.id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
