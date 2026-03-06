"""
Scheduler functionality for the Telegram Food Poll Bot.
"""

import asyncio
import datetime
import json
import logging
import zoneinfo
from pathlib import Path
from typing import Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.ext import Application, ContextTypes

from .config import (
    DAILY_MESSAGE,
    TIMEZONE,
    WEEKDAY_REMINDER_MESSAGE_TIME,
    WEEKDAY_VONGSA_QR_TIME,
)

logger = logging.getLogger(__name__)

# Global storage for chat IDs (persisted to disk)
chat_ids_for_scheduled_messages: Set[int] = set()

VONGSA_QR_PATH = Path(__file__).parent.parent / "assets" / "payment_qr.png"
VONGSA_QR_CAPTION = (
    "Vongsa Hourt Payment (KHQR)\n\n"
    "Please scan the QR code below to pay Vongsa Hourt via KHQR."
)

DATA_DIR = Path(__file__).parent.parent / "data"
SCHEDULED_CHATS_FILE = DATA_DIR / "scheduled_chats.json"

_scheduler: Optional[AsyncIOScheduler] = None
_chats_loaded = False


def _load_scheduled_chats() -> None:
    global _chats_loaded

    if _chats_loaded:
        return

    _chats_loaded = True
    if not SCHEDULED_CHATS_FILE.exists():
        return

    try:
        payload = json.loads(SCHEDULED_CHATS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for item in payload:
                chat_ids_for_scheduled_messages.add(int(item))
            logger.info(
                f"Loaded {len(chat_ids_for_scheduled_messages)} scheduled chat(s) from disk"
            )
    except Exception as e:
        logger.error(f"Failed to load scheduled chats file: {e}")


def _save_scheduled_chats() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SCHEDULED_CHATS_FILE.write_text(
            json.dumps(sorted(chat_ids_for_scheduled_messages), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"Failed to persist scheduled chats: {e}")


async def _send_text_reminder_to_all(bot: Bot) -> None:
    _load_scheduled_chats()

    logger.info(f"Attempting to send scheduled message at {datetime.datetime.now()}")
    if not chat_ids_for_scheduled_messages:
        logger.warning(
            "No subscribed chats found for reminders. Send /start or /subscribe in target chat first."
        )
        return

    for chat_id in list(chat_ids_for_scheduled_messages):
        try:
            await bot.send_message(chat_id=chat_id, text=DAILY_MESSAGE)
            logger.info(f"Message sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send reminder to {chat_id}: {e}")


async def _send_vongsa_qr_to_all(bot: Bot) -> None:
    _load_scheduled_chats()

    logger.info(f"Attempting to send Vongsa QR reminder at {datetime.datetime.now()}")
    if not chat_ids_for_scheduled_messages:
        logger.warning(
            "No subscribed chats found for reminders. Send /start or /subscribe in target chat first."
        )
        return

    for chat_id in list(chat_ids_for_scheduled_messages):
        try:
            if VONGSA_QR_PATH.exists():
                with open(VONGSA_QR_PATH, "rb") as photo:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=VONGSA_QR_CAPTION,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text="Vongsa KHQR reminder: QR image not found.",
                )
                logger.warning(f"QR image not found at {VONGSA_QR_PATH}")
            logger.info(f"Vongsa QR reminder sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send QR reminder to {chat_id}: {e}")


async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger reminder text (used by debug command)."""
    await _send_text_reminder_to_all(context.bot)


async def send_vongsa_qr_now(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger Vongsa QR reminder."""
    await _send_vongsa_qr_to_all(context.bot)


def add_chat_for_scheduled_messages(chat_id: int) -> None:
    """
    Add a chat ID to receive scheduled messages.

    Args:
        chat_id: Telegram chat ID
    """
    _load_scheduled_chats()
    chat_ids_for_scheduled_messages.add(chat_id)
    _save_scheduled_chats()
    logger.info(f"Added chat {chat_id} for scheduled messages")


def remove_chat_from_scheduled_messages(chat_id: int) -> None:
    """
    Remove a chat ID from scheduled messages.

    Args:
        chat_id: Telegram chat ID
    """
    _load_scheduled_chats()
    chat_ids_for_scheduled_messages.discard(chat_id)
    _save_scheduled_chats()
    logger.info(f"Removed chat {chat_id} from scheduled messages")


async def setup_scheduler(application: Application) -> None:
    """
    Set up weekday reminder jobs using APScheduler.

    Args:
        application: Telegram bot application
    """
    global _scheduler

    try:
        _load_scheduled_chats()

        tz = zoneinfo.ZoneInfo(TIMEZONE)
        reminder_hour, reminder_minute = map(int, WEEKDAY_REMINDER_MESSAGE_TIME.split(":"))
        qr_hour, qr_minute = map(int, WEEKDAY_VONGSA_QR_TIME.split(":"))

        current_loop = asyncio.get_running_loop()
        if _scheduler is None:
            _scheduler = AsyncIOScheduler(timezone=tz, event_loop=current_loop)

        # Safe to call on re-init/restart scenarios.
        for job_id in ("weekday_message_reminder", "weekday_vongsa_qr_reminder"):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)

        _scheduler.add_job(
            _send_text_reminder_to_all,
            trigger="cron",
            day_of_week="mon-fri",
            hour=reminder_hour,
            minute=reminder_minute,
            id="weekday_message_reminder",
            args=[application.bot],
            replace_existing=True,
        )
        _scheduler.add_job(
            _send_vongsa_qr_to_all,
            trigger="cron",
            day_of_week="mon-fri",
            hour=qr_hour,
            minute=qr_minute,
            id="weekday_vongsa_qr_reminder",
            args=[application.bot],
            replace_existing=True,
        )

        if not _scheduler.running:
            _scheduler.start()

        logger.info(
            f"Scheduled weekday reminder at {WEEKDAY_REMINDER_MESSAGE_TIME} and "
            f"Vongsa QR reminder at {WEEKDAY_VONGSA_QR_TIME} ({TIMEZONE} time)"
        )
    except Exception as e:
        logger.error(f"Failed to setup scheduler: {e}")


def get_scheduled_chats() -> Set[int]:
    """
    Get all chat IDs that receive scheduled messages.

    Returns:
        Set of chat IDs
    """
    _load_scheduled_chats()
    return chat_ids_for_scheduled_messages.copy()
