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
from apscheduler.triggers.date import DateTrigger
from telegram import Bot
from telegram.ext import Application, ContextTypes

from . import exchange
from .config import (
    DAILY_MESSAGE,
    EXCHANGE_REFRESH_HOUR,
    EXCHANGE_REFRESH_MINUTE,
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

async def _resolve_targets(target_chat_ids: str = "ALL") -> list[int]:
    """Resolve a schedule's target_chat_ids field to concrete chat ids.

    "ALL" (or empty) → every subscribed chat. Otherwise a comma-separated
    list of chat ids is used verbatim, so a schedule can be aimed at specific
    chats from the Mini App's "Target chats" field."""
    raw = (target_chat_ids or "ALL").strip()
    if not raw or raw.upper() == "ALL":
        return await _get_subscribed_chat_ids()
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            logger.warning(f"Schedule target_chat_ids: ignoring non-numeric '{part}'")
    return out


async def _send_text_reminder_to_all(bot: Bot, payload: str = "", target_chat_ids: str = "ALL") -> None:
    text = payload or await settings.get("DAILY_MESSAGE", DAILY_MESSAGE)
    chat_ids = await _resolve_targets(target_chat_ids)
    logger.info(f"Sending text reminder to {len(chat_ids)} chat(s) at {datetime.datetime.now()}")
    if not chat_ids:
        logger.warning("No target chats — /start or /subscribe needed first.")
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            logger.info(f"Text reminder sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send reminder to {chat_id}: {e}")


def _find_qr_asset(payload: str = "payment_qr") -> Optional[Path]:
    """Find a QR asset by name/stem, checking .jpg, .png, .jpeg, .webp extensions."""
    assets_dir = Path(__file__).parent.parent / "assets"
    stem = Path(payload or "payment_qr").stem
    candidates = [
        payload,
        f"{stem}.jpg",
        f"{stem}.png",
        f"{stem}.jpeg",
        f"{stem}.webp",
        "payment_qr.jpg",
        "payment_qr.png",
    ]
    for c in candidates:
        if c:
            p = assets_dir / c
            if p.is_file():
                return p
    return None


async def _send_qr_photo_to_all(bot: Bot, payload: str = "payment_qr.png", target_chat_ids: str = "ALL") -> None:
    qr_path = _find_qr_asset(payload)
    chat_ids = await _resolve_targets(target_chat_ids)
    logger.info(f"Sending QR reminder to {len(chat_ids)} chat(s) at {datetime.datetime.now()}")
    if not chat_ids:
        logger.warning("No target chats for QR reminder.")
        return

    for chat_id in chat_ids:
        try:
            if qr_path and qr_path.exists():
                with open(qr_path, "rb") as photo:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=VONGSA_QR_CAPTION,
                        read_timeout=60.0,
                        write_timeout=60.0,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id, text="QR image not found.",
                )
                logger.warning(f"QR image not found for payload {payload} in {Path(__file__).parent.parent / 'assets'}")
            logger.info(f"QR reminder sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send QR reminder to {chat_id}: {e}")


def _image_send_arg(image: str):
    """Resolve a schedule's image value to something Bot.send_photo accepts.

    If it names a file in assets/ → return that Path (open and upload).
    Otherwise treat it as a Telegram file_id (from a Mini App upload) and
    return the string as-is. Returns None when there's no image."""
    image = (image or "").strip()
    if not image:
        return None
    p = _find_qr_asset(image)
    if p and p.is_file():
        return p
    return image


async def _send_scheduled(
    bot: Bot, text: str = "", image: str = "", target_chat_ids: str = "ALL"
) -> None:
    """Unified scheduled send: a photo (with text as caption) when an image is
    set, otherwise a text message. Used by all new schedules."""
    chat_ids = await _resolve_targets(target_chat_ids)
    if not chat_ids:
        logger.warning("Scheduled send: no target chats.")
        return
    img = _image_send_arg(image)
    caption = text or None
    for chat_id in chat_ids:
        try:
            if img is not None:
                if isinstance(img, Path):
                    with open(img, "rb") as photo:
                        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
                else:
                    await bot.send_photo(chat_id=chat_id, photo=img, caption=caption)
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text or await settings.get("DAILY_MESSAGE", DAILY_MESSAGE),
                )
            logger.info(f"Scheduled message sent to {chat_id}")
        except Exception as e:
            logger.error(f"Scheduled send to {chat_id} failed: {e}")


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

        # Check if auto-invoicing is enabled (default TRUE, $1.75 fixed price per item)
        auto_invoice_enabled = await settings.get("AUTO_INVOICE_ENABLED", "TRUE")
        if str(auto_invoice_enabled).upper() == "TRUE" and saved:
            try:
                from .invoicing import generate_and_send_invoice
                price_str = await settings.get("AUTO_INVOICE_PRICE", "1.75")
                try:
                    item_price = float(price_str)
                except ValueError:
                    item_price = 1.75
                await generate_and_send_invoice(
                    bot=bot,
                    order_id=poll_id,
                    chat_id=chat_id,
                    price_per_item=item_price,
                )
                logger.info(f"Cutoff auto-invoice generated & sent for poll {poll_id} (${item_price:.2f}/item)")
            except Exception as e:
                logger.error(f"Failed to generate cutoff auto-invoice for poll {poll_id}: {e}", exc_info=True)

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
    tz = getattr(scheduler, "timezone", None)
    now = datetime.datetime.now(tz)
    for row in rows:
        schedule_id = row.get("schedule_id") or ""
        action_type = str(row.get("action_type", "")).upper()
        hour, minute = _parse_hhmm(str(row.get("time_of_day", "")), "08:00")
        targets = str(row.get("target_chat_ids", "ALL"))
        run_date = str(row.get("run_date", "")).strip()

        # New model: message_text + image columns. Fall back to legacy
        # action_type/payload so old rows keep working unchanged.
        message_text = str(row.get("message_text", "") or "")
        image = str(row.get("image", "") or "")
        payload = str(row.get("payload", "") or "")
        has_new = bool(message_text or image or run_date)

        if not has_new:
            # Pure legacy row → dispatch on action_type as before (preserves
            # e.g. the Vongsa QR caption in _send_qr_photo_to_all).
            action = _ACTION_DISPATCH.get(action_type)
            if not action:
                logger.warning(f"Schedule '{schedule_id}': unknown action_type '{action_type}', skipping")
                continue
            days = _normalise_days(str(row.get("days_of_week", "")))
            scheduler.add_job(
                action, trigger="cron", day_of_week=days, hour=hour, minute=minute,
                id=f"sheet_{schedule_id}", args=[application.bot, payload, targets],
                replace_existing=True,
            )
            registered += 1
            logger.info(
                f"Registered schedule '{schedule_id}': {action_type} @ "
                f"{hour:02d}:{minute:02d} {days} -> {targets}"
            )
            continue

        # New model: normalise text/image (also accept legacy payload).
        text = message_text or (payload if action_type == "TEXT" else "")
        img = image or (payload if action_type == "QR_PHOTO" else "")
        kwargs = {"text": text, "image": img, "target_chat_ids": targets}

        if run_date:
            # One-time schedule on a specific date.
            try:
                y, m, d = (int(x) for x in run_date.split("-"))
                when = datetime.datetime(y, m, d, hour, minute, tzinfo=tz)
            except (ValueError, TypeError):
                logger.warning(f"Schedule '{schedule_id}': bad run_date '{run_date}', skipping")
                continue
            if when <= now:
                logger.info(
                    f"Schedule '{schedule_id}': run_date {run_date} {hour:02d}:{minute:02d} "
                    f"is in the past — not registered."
                )
                continue
            scheduler.add_job(
                _send_scheduled, trigger=DateTrigger(run_date=when),
                id=f"sheet_{schedule_id}", args=[application.bot], kwargs=kwargs,
                replace_existing=True,
            )
            registered += 1
            logger.info(f"Registered one-time schedule '{schedule_id}' @ {when.isoformat()} -> {targets}")
        else:
            days = _normalise_days(str(row.get("days_of_week", "")))
            scheduler.add_job(
                _send_scheduled, trigger="cron", day_of_week=days, hour=hour, minute=minute,
                id=f"sheet_{schedule_id}", args=[application.bot], kwargs=kwargs,
                replace_existing=True,
            )
            registered += 1
            logger.info(
                f"Registered schedule '{schedule_id}': "
                f"{'IMG' if img else 'TEXT'} @ {hour:02d}:{minute:02d} {days} -> {targets}"
            )
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


async def _run_scheduled_auto_invoicing(bot: Bot) -> None:
    """Execute scheduled auto-invoicing at AUTO_INVOICE_TIME for all active polls/orders."""
    if not is_configured():
        logger.info("Auto-invoice job skipped: Sheets not configured")
        return

    enabled = await settings.get("AUTO_INVOICE_ENABLED", "TRUE")
    if str(enabled).upper() != "TRUE":
        logger.info("Auto-invoice job skipped: AUTO_INVOICE_ENABLED is FALSE")
        return

    price_str = await settings.get("AUTO_INVOICE_PRICE", "1.75")
    try:
        item_price = float(price_str)
    except ValueError:
        item_price = 1.75

    from .invoicing import generate_and_send_invoice
    from .sheets import invoices as sheets_invoices, orders as sheets_orders
    from .sheets.orders import _today_date

    # 1. Snapshot any open polls
    open_polls = await polls.list_open()
    for poll in open_polls:
        poll_id = poll["poll_id"]
        chat_id = poll["chat_id"]
        selections_map = await votes.get_user_selections_map(poll_id)
        users = len(selections_map)
        saved = await sheets_orders.snapshot_from_poll(poll_id, chat_id)
        await polls.close(poll_id)
        await events.emit(
            "ORDER_SNAPSHOT", entity_type="poll", entity_id=poll_id,
            chat_id=chat_id,
            payload={"saved": bool(saved), "users": users},
        )

    # 2. Get today's orders that don't have an invoice yet
    today = _today_date()
    orders_today = await sheets_orders.list_by_date(today)
    existing_invoices = await sheets_invoices.order_ids_with_invoice()

    sent_count = 0
    for order in orders_today:
        oid = str(order.get("order_id", ""))
        cid = order.get("chat_id")
        if oid and oid not in existing_invoices:
            try:
                await generate_and_send_invoice(
                    bot=bot,
                    order_id=oid,
                    chat_id=cid,
                    price_per_item=item_price,
                )
                sent_count += 1
                logger.info(f"Auto-invoice sent for order {oid} (${item_price:.2f}/item)")
            except Exception as e:
                logger.error(f"Failed to send auto-invoice for order {oid}: {e}", exc_info=True)

    logger.info(f"Scheduled auto-invoicing complete: sent {sent_count} invoice(s)")


async def _register_auto_invoice_job(
    scheduler: AsyncIOScheduler, application: Application
) -> None:
    """Schedule the daily auto-invoicing job in Cambodia timezone (Asia/Phnom_Penh)."""
    if not is_configured():
        return
    enabled = await settings.get("AUTO_INVOICE_ENABLED", "TRUE")
    if str(enabled).upper() != "TRUE":
        logger.info("Auto-invoice schedule not registered (AUTO_INVOICE_ENABLED is FALSE)")
        return

    hour, minute = await settings.get_time("AUTO_INVOICE_TIME", "11:59")
    scheduler.add_job(
        _run_scheduled_auto_invoicing,
        trigger="cron",
        day_of_week="mon-fri",
        hour=hour,
        minute=minute,
        id="auto_invoice_scheduled_job",
        args=[application.bot],
        replace_existing=True,
    )
    logger.info(f"Registered auto-invoice job at {hour:02d}:{minute:02d} (Mon-Fri Cambodia Time)")


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


async def _refresh_exchange_rate() -> None:
    """Pull NBC's official USD→KHR rate into the `exchange_rate` tab.

    Runs daily after NBC publishes (~16:30 ICT) and once at startup so a
    fresh deploy is never rate-less. Failures are logged, never raised: the
    stored rate carries forward and invoices keep working.
    """
    try:
        row = await exchange.refresh()
        if row is None and await exchange.is_stale():
            current = await exchange.current()
            logger.error(
                "Exchange rate refresh failed and the stored rate is stale "
                "(newest: %s). Invoices will quote an out-of-date rate.",
                (current or {}).get("rate_date", "never fetched"),
            )
    except Exception as e:
        logger.error(f"Exchange rate refresh job failed: {e}", exc_info=True)


async def _register_exchange_rate_job(scheduler: AsyncIOScheduler) -> None:
    """Daily rate refresh. Every day, not Mon-Fri: NBC doesn't publish at
    weekends, but a Saturday run is what recovers a Friday the fetch missed."""
    scheduler.add_job(
        _refresh_exchange_rate,
        trigger="cron",
        hour=EXCHANGE_REFRESH_HOUR,
        minute=EXCHANGE_REFRESH_MINUTE,
        id="exchange_rate_refresh",
        replace_existing=True,
    )
    logger.info(
        f"Registered exchange-rate refresh at "
        f"{EXCHANGE_REFRESH_HOUR:02d}:{EXCHANGE_REFRESH_MINUTE:02d} (daily)"
    )
    # Don't make the first quote wait until this evening.
    asyncio.create_task(_refresh_exchange_rate())


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
            await _register_auto_invoice_job(_scheduler, application)
        else:
            await _register_fallback_jobs(_scheduler, application)
        # Independent of the `schedule` tab — the rate is infrastructure, not
        # a message someone configured.
        await _register_exchange_rate_job(_scheduler)
        if not is_configured():
            logger.info(
                f"Scheduled fallback reminders at {WEEKDAY_REMINDER_MESSAGE_TIME} "
                f"and {WEEKDAY_VONGSA_QR_TIME} ({tz_name})"
            )

        if not _scheduler.running:
            _scheduler.start()
    except Exception as e:
        logger.error(f"Failed to setup scheduler: {e}")
