"""
Scheduler functionality for the Telegram Food Poll Bot.

Two modes:
    - Sheets-backed (when GOOGLE_* env vars are set): subscribed chats
      come from the `chat` tab; scheduled jobs come from the `schedule`
      tab; message text from the `setting` tab.
    - Fallback (local dev, no Sheets): the old in-memory set + JSON
      file + hardcoded WEEKDAY_* constants. Behaviour identical to
      Phase 0.

Both modes use APScheduler (PTB's JobQueue is disabled — Python 3.14
weakref crash in PTB 20.1).
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
from .sheets import chats as sheets_chats
from .sheets import events, orders as sheets_orders, polls, settings, votes
from .sheets.client import is_configured

logger = logging.getLogger(__name__)

# --- Fallback in-memory state (only used when Sheets isn't configured) ---
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


# ---------------------------------------------------------------------------
# Subscribed-chats accessors (Sheets when configured, JSON fallback otherwise)
# ---------------------------------------------------------------------------

def _load_scheduled_chats_from_disk() -> None:
    global _chats_loaded
    if _chats_loaded or is_configured():
        _chats_loaded = True
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


def _save_scheduled_chats_to_disk() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SCHEDULED_CHATS_FILE.write_text(
            json.dumps(sorted(chat_ids_for_scheduled_messages), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"Failed to persist scheduled chats: {e}")


async def _get_subscribed_chat_ids() -> list[int]:
    if is_configured():
        return await sheets_chats.list_subscribed()
    _load_scheduled_chats_from_disk()
    return list(chat_ids_for_scheduled_messages)


async def add_chat_for_scheduled_messages(
    chat_id: int,
    *,
    title: str = "",
    chat_type: str = "",
    subscribed_by: Optional[int] = None,
) -> None:
    """Add a chat ID to receive scheduled messages."""
    if is_configured():
        await sheets_chats.subscribe(
            chat_id, title=title, chat_type=chat_type, subscribed_by=subscribed_by,
        )
        await events.emit(
            "CHAT_SUBSCRIBED",
            entity_type="chat", entity_id=chat_id,
            chat_id=chat_id, user_id=subscribed_by,
        )
        return

    _load_scheduled_chats_from_disk()
    chat_ids_for_scheduled_messages.add(chat_id)
    _save_scheduled_chats_to_disk()
    logger.info(f"Added chat {chat_id} for scheduled messages")


async def remove_chat_from_scheduled_messages(
    chat_id: int,
    *,
    user_id: Optional[int] = None,
) -> None:
    """Remove a chat ID from scheduled messages."""
    if is_configured():
        existed = await sheets_chats.unsubscribe(chat_id)
        if existed:
            await events.emit(
                "CHAT_UNSUBSCRIBED",
                entity_type="chat", entity_id=chat_id,
                chat_id=chat_id, user_id=user_id,
            )
        return

    _load_scheduled_chats_from_disk()
    chat_ids_for_scheduled_messages.discard(chat_id)
    _save_scheduled_chats_to_disk()
    logger.info(f"Removed chat {chat_id} from scheduled messages")


async def get_scheduled_chats() -> Set[int]:
    """All chat IDs that receive scheduled messages."""
    return set(await _get_subscribed_chat_ids())


# ---------------------------------------------------------------------------
# Scheduled actions
# ---------------------------------------------------------------------------

async def _send_text_reminder_to_all(bot: Bot, payload: str = "") -> None:
    text = payload or await settings.get("DAILY_MESSAGE", DAILY_MESSAGE)
    chat_ids = await _get_subscribed_chat_ids()
    logger.info(f"Sending text reminder to {len(chat_ids)} chat(s) at {datetime.datetime.now()}")
    if not chat_ids:
        logger.warning("No subscribed chats — /start or /subscribe needed first.")
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            logger.info(f"Text reminder sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send reminder to {chat_id}: {e}")


async def _send_qr_photo_to_all(bot: Bot, payload: str = "payment_qr.png") -> None:
    qr_path = Path(__file__).parent.parent / "assets" / (payload or "payment_qr.png")
    chat_ids = await _get_subscribed_chat_ids()
    logger.info(f"Sending QR reminder to {len(chat_ids)} chat(s) at {datetime.datetime.now()}")
    if not chat_ids:
        logger.warning("No subscribed chats for QR reminder.")
        return

    for chat_id in chat_ids:
        try:
            if qr_path.exists():
                with open(qr_path, "rb") as photo:
                    await bot.send_photo(
                        chat_id=chat_id, photo=photo, caption=VONGSA_QR_CAPTION,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id, text="QR image not found.",
                )
                logger.warning(f"QR image not found at {qr_path}")
            logger.info(f"QR reminder sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send QR reminder to {chat_id}: {e}")


async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger reminder text (used by debug command)."""
    await _send_text_reminder_to_all(context.bot)


async def send_vongsa_qr_now(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger Vongsa QR reminder."""
    await _send_qr_photo_to_all(context.bot)


# ---------------------------------------------------------------------------
# Cutoff snapshot — vote rows → order rows at ORDER_CUTOFF_TIME
# ---------------------------------------------------------------------------

async def _snapshot_orders_at_cutoff(bot: Bot, payload: str = "") -> None:
    """Walk every OPEN poll; emit `order` rows for each (user, item) and close the poll."""
    if not is_configured():
        logger.info("Cutoff job skipped: Sheets not configured")
        return

    open_polls = await polls.list_open()
    if not open_polls:
        logger.info("Cutoff job: no open polls to snapshot")
        return

    snapshot_count = 0

    for poll in open_polls:
        poll_id = poll["poll_id"]
        chat_id = poll["chat_id"]
        selections_map = await votes.get_user_selections_map(poll_id)
        users = len(selections_map)

        # Same row shape as the Order button (one row per poll, item JSON
        # array with per-voter user_id) so the calendar reads both alike.
        # No clicker at cutoff time → paid_by stays empty.
        saved = await sheets_orders.snapshot_from_poll(poll_id, chat_id)
        if saved:
            snapshot_count += 1

        await polls.close(poll_id)
        await events.emit(
            "ORDER_SNAPSHOT", entity_type="poll", entity_id=poll_id,
            chat_id=chat_id,
            payload={"saved": bool(saved), "users": users},
        )

    logger.info(f"Cutoff snapshot: wrote {snapshot_count} order row(s) across {len(open_polls)} poll(s)")


# ---------------------------------------------------------------------------
# Schedule setup
# ---------------------------------------------------------------------------

_ACTION_DISPATCH = {
    "TEXT": _send_text_reminder_to_all,
    "QR_PHOTO": _send_qr_photo_to_all,
}

_DAY_TO_APS = {
    "MON": "mon", "TUE": "tue", "WED": "wed", "THU": "thu",
    "FRI": "fri", "SAT": "sat", "SUN": "sun",
}


def _normalise_days(raw: str) -> str:
    parts = [_DAY_TO_APS.get(p.strip().upper()) for p in (raw or "").split(",")]
    parts = [p for p in parts if p]
    return ",".join(parts) if parts else "mon-fri"


def _parse_hhmm(raw: str, default: str) -> tuple[int, int]:
    try:
        hh, mm = (raw or default).split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        hh, mm = default.split(":")
        return int(hh), int(mm)


async def _register_jobs_from_sheets(
    scheduler: AsyncIOScheduler, application: Application
) -> int:
    """Read `schedule` tab; create one APScheduler cron job per active row."""
    from .sheets import repo
    rows = await repo.filter_rows(
        "schedule",
        lambda r: str(r.get("is_active", "")).upper() == "TRUE",
    )
    registered = 0
    for row in rows:
        schedule_id = row.get("schedule_id") or ""
        action_type = str(row.get("action_type", "")).upper()
        action = _ACTION_DISPATCH.get(action_type)
        if not action:
            logger.warning(f"Schedule '{schedule_id}': unknown action_type '{action_type}', skipping")
            continue

        days = _normalise_days(str(row.get("days_of_week", "")))
        hour, minute = _parse_hhmm(str(row.get("time_of_day", "")), "08:00")
        payload = str(row.get("payload", ""))

        scheduler.add_job(
            action,
            trigger="cron",
            day_of_week=days,
            hour=hour,
            minute=minute,
            id=f"sheet_{schedule_id}",
            args=[application.bot, payload],
            replace_existing=True,
        )
        registered += 1
        logger.info(f"Registered schedule '{schedule_id}': {action_type} @ {hour:02d}:{minute:02d} {days}")
    return registered


async def _register_fallback_jobs(
    scheduler: AsyncIOScheduler, application: Application
) -> None:
    """Hardcoded jobs matching the pre-Sheets behaviour."""
    reminder_hour, reminder_minute = map(int, WEEKDAY_REMINDER_MESSAGE_TIME.split(":"))
    qr_hour, qr_minute = map(int, WEEKDAY_VONGSA_QR_TIME.split(":"))

    scheduler.add_job(
        _send_text_reminder_to_all,
        trigger="cron", day_of_week="mon-fri",
        hour=reminder_hour, minute=reminder_minute,
        id="weekday_message_reminder",
        args=[application.bot, ""],
        replace_existing=True,
    )
    scheduler.add_job(
        _send_qr_photo_to_all,
        trigger="cron", day_of_week="mon-fri",
        hour=qr_hour, minute=qr_minute,
        id="weekday_vongsa_qr_reminder",
        args=[application.bot, "payment_qr.png"],
        replace_existing=True,
    )


async def _register_cutoff_job(
    scheduler: AsyncIOScheduler, application: Application
) -> None:
    """Schedule the daily snapshot of open polls → order rows."""
    if not is_configured():
        return
    hour, minute = await settings.get_time("ORDER_CUTOFF_TIME", "10:30")
    scheduler.add_job(
        _snapshot_orders_at_cutoff,
        trigger="cron",
        day_of_week="mon-fri",
        hour=hour, minute=minute,
        id="order_cutoff_snapshot",
        args=[application.bot, ""],
        replace_existing=True,
    )
    logger.info(f"Registered cutoff snapshot job at {hour:02d}:{minute:02d} (Mon-Fri)")


async def reload_schedules(application: Application) -> None:
    """Re-read `schedule` tab and rebuild APScheduler jobs in place."""
    await setup_scheduler(application)


async def setup_scheduler(application: Application) -> None:
    """Set up weekday reminder jobs using APScheduler."""
    global _scheduler
    try:
        tz_name = await settings.get("TIMEZONE", TIMEZONE)
        tz = zoneinfo.ZoneInfo(tz_name)
        current_loop = asyncio.get_running_loop()

        if _scheduler is None:
            _scheduler = AsyncIOScheduler(timezone=tz, event_loop=current_loop)

        # Wipe any jobs from a previous setup_scheduler call (re-init safety).
        for job in list(_scheduler.get_jobs()):
            _scheduler.remove_job(job.id)

        if is_configured():
            n = await _register_jobs_from_sheets(_scheduler, application)
            if n == 0:
                logger.warning(
                    "Sheets configured but no active rows in `schedule` tab — "
                    "no reminders will fire. Add rows in the spreadsheet."
                )
            await _register_cutoff_job(_scheduler, application)
        else:
            await _register_fallback_jobs(_scheduler, application)
            logger.info(
                f"Scheduled fallback reminders at {WEEKDAY_REMINDER_MESSAGE_TIME} "
                f"and {WEEKDAY_VONGSA_QR_TIME} ({tz_name})"
            )

        if not _scheduler.running:
            _scheduler.start()
    except Exception as e:
        logger.error(f"Failed to setup scheduler: {e}")
