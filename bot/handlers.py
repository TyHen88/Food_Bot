"""
Message and callback handlers for the Telegram Food Poll Bot.
"""

import logging
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

from .config import (
    ERROR_NO_ORDERS,
    ERROR_POLL_NOT_FOUND,
    KHR_ROUNDING,
    ORDER_CLOSED_MESSAGE,
    ORDER_NAME,
    MINIAPP_URL,
    WELCOME_MESSAGE,
)
from .auth import admin_only
from .menu_processor import (
    get_global_orders,
    get_poll_data,
    get_user_selections,
    hide_order_buttons,
    process_food_menu,
    record_user_vote,
)
from .scheduler import (
    add_chat_for_scheduled_messages,
    reload_schedules,
    remove_chat_from_scheduled_messages,
    send_scheduled_message,
    send_vongsa_qr_now,
)
from .sheets import chat_settings as sheets_chat_settings
from .sheets import events as sheets_events
from .sheets import orders as sheets_orders
from .sheets import payers as sheets_payers
from .sheets import repo as sheets_repo
from .sheets import settings as sheets_settings
from .sheets.client import is_configured
import json
from . import ai, exchange
from .payway import is_payway_text, parse_payway_transaction
from .settlement import process_transaction_settlement
from .utils import format_order_summary, is_food_menu_text, with_retry

logger = logging.getLogger(__name__)


async def _record_user(update: Update) -> None:
    """Upsert the Telegram user into the `user` tab (no-op if Sheets off)."""
    if not is_configured() or not update.effective_user:
        return
    u = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else ""
    try:
        existing = await sheets_repo.find_by_pk("user", u.id)
        if existing:
            await sheets_repo.update("user", u.id, {
                "username": u.username or existing.get("username", ""),
                "full_name": u.full_name or existing.get("full_name", ""),
                "chat_id": chat_id or existing.get("chat_id", ""),
                "last_active_at": sheets_repo.now_iso(),
            })
        else:
            await sheets_repo.create("user", {
                "user_id": u.id,
                "username": u.username or "",
                "full_name": u.full_name or "",
                "phone_number": "",
                "chat_id": chat_id,
                "role": "MEMBER",
                "language": "KH",
                "dietary_notes": "",
                "created_at": sheets_repo.now_iso(),
                "last_active_at": sheets_repo.now_iso(),
            })
    except Exception as e:
        logger.warning(f"_record_user failed for user {u.id}: {e}")


async def _record_user_if_new(tg_user, chat_id=None) -> None:
    """
    Insert-only variant: add a new user row when they vote, but leave
    existing rows untouched. Used from poll-answer handling. `chat_id`,
    when known, is stored on newly inserted rows so members can be looked
    up by chat.
    """
    if not is_configured() or not tg_user:
        return
    try:
        existing = await sheets_repo.find_by_pk("user", tg_user.id)
        if existing:
            return
        await sheets_repo.create("user", {
            "user_id": tg_user.id,
            "username": tg_user.username or "",
            "full_name": tg_user.full_name or "",
            "phone_number": "",
            "chat_id": chat_id or "",
            "role": "MEMBER",
            "language": "KH",
            "dietary_notes": "",
            "created_at": sheets_repo.now_iso(),
            "last_active_at": sheets_repo.now_iso(),
        })
    except Exception as e:
        logger.warning(f"_record_user_if_new failed for user {tg_user.id}: {e}")


def encode_chat_param(chat_id) -> str:
    """Encode a chat id for Telegram's `startapp` parameter without a leading
    "-". startapp only allows [A-Za-z0-9_-] and a leading "-" is unreliable
    across clients, so we use a sign prefix: "g" for negative ids (groups /
    supergroups) and "c" for non-negative ids. The frontend reverses this in
    launchChatId(). Example: -1002308775160 -> "g1002308775160"."""
    n = int(chat_id)
    return f"g{-n}" if n < 0 else f"c{n}"


def _calendar_keyboard(chat, bot_username: str | None = None):
    """One-button "View Calendar" markup that opens the Mini App.

    Private chats get a persistent reply-keyboard `web_app` button. Groups
    can't use `web_app` keyboard buttons (Telegram raises
    Button_type_invalid), so they get an inline button to the direct-link
    Mini App (t.me/<bot>?startapp), which needs the Main Mini App set in
    BotFather. Returns None when no Mini App URL is configured.
    """
    if not MINIAPP_URL or not chat:
        return None

    if chat.type == "private":
        return ReplyKeyboardMarkup(
            [[KeyboardButton("📅 View Calendar", web_app=WebAppInfo(url=f"{MINIAPP_URL}/"))]],
            resize_keyboard=True,
            is_persistent=True,
        )

    # Group / supergroup → inline direct-link button. Pass the chat id through
    # the startapp parameter so the Mini App can scope the calendar/members to
    # this chat (Telegram exposes it as initDataUnsafe.start_param).
    if bot_username:
        link = f"https://t.me/{bot_username}?startapp={encode_chat_param(chat.id)}"
    else:
        link = f"{MINIAPP_URL}/"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 View Calendar", url=link),
    ]])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages and process menu text."""
    if not update.message or not update.message.text:
        logger.info("No message text, skipping")
        return

    text = update.message.text.strip()
    logger.info(f"Received message text: {repr(text)}")

    if is_payway_text(text):
        rate = 4000.0
        try:
            curr_rate = await exchange.current()
            if curr_rate and curr_rate.get("usd_khr"):
                rate = float(curr_rate["usd_khr"])
        except Exception:
            pass
        tx = parse_payway_transaction(text, usd_khr_rate=rate)
        if tx:
            logger.info(f"Processing PayWay transaction: {tx.trx_id} from {tx.sender_name} (${tx.amount_usd})")
            chat = update.effective_chat
            chat_id = chat.id if chat else None
            res = await process_transaction_settlement(tx)

            # Only send receipt to target group if sender is a registered Food Bot member
            if res.get("status") == "MATCHED":
                receipt_text = res.get("receipt_text")
                if receipt_text:
                    tgt_chat_str = await sheets_settings.get("PAYMENT_TARGET_CHAT_ID", "")
                    send_chat_id = int(tgt_chat_str) if (tgt_chat_str and tgt_chat_str.strip()) else chat_id
                    if send_chat_id:
                        try:
                            await with_retry(
                                context.bot.send_message,
                                chat_id=send_chat_id,
                                text=receipt_text,
                                parse_mode="HTML",
                            )
                            if chat_id and send_chat_id != chat_id:
                                await with_retry(
                                    context.bot.send_message,
                                    chat_id=chat_id,
                                    text=f"✅ Payment from <b>{tx.sender_name}</b> (${tx.amount_usd:.2f}) processed. Receipt posted to target group.",
                                    parse_mode="HTML",
                                )
                        except Exception as e:
                            logger.error(f"Failed to send PayWay receipt: {e}")
            else:
                logger.info(
                    f"PayWay payment from '{tx.sender_name}' (${tx.amount_usd:.2f}) is not a registered member "
                    f"(status={res.get('status')}). Payment saved to sheet, but no receipt sent to group."
                )
            return

    if is_food_menu_text(text):
        user = update.effective_user
        chat = update.effective_chat
        logger.info(f"Processing food menu from user {user.id if user else '?'}")
        poll_id = await process_food_menu(update, context, text)
        if poll_id:
            await sheets_events.emit(
                "POLL_CREATED",
                entity_type="poll", entity_id=poll_id,
                chat_id=chat.id if chat else None,
                user_id=user.id if user else None,
            )


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle poll answers: persist the user's selection (one row per (poll, user))."""
    poll_answer = update.poll_answer
    if not poll_answer or not poll_answer.user:
        logger.warning("Received poll answer without user information")
        return

    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name or poll_answer.user.username or f"User{user_id}"

    poll_data = await get_poll_data(poll_id)
    if not poll_data:
        logger.warning(f"Poll data not found for poll ID: {poll_id}")
        return

    # Add the voter to the `user` tab on their first vote — no-op if already present.
    await _record_user_if_new(poll_answer.user, poll_data.get("chat_id"))

    options = poll_data.get("options", [])
    current_selections = [
        options[idx] for idx in poll_answer.option_ids if idx < len(options)
    ]

    await record_user_vote(poll_id, user_id, user_name, current_selections)
    await sheets_events.emit(
        "VOTE_CAST",
        entity_type="poll", entity_id=poll_id,
        user_id=user_id,
        payload={"selections": current_selections},
    )

    logger.info(f"User {user_name} updated poll {poll_id}: {current_selections}")


async def _take_order_snapshot(update: Update, poll_data: dict, poll_id: str) -> str | None:
    """
    Persist a snapshot of `poll_id`'s current votes to the `order` tab.
    Returns an error message string on failure, or None on success.
    Safe to call multiple times on the same poll (upsert keyed on poll_id).
    """
    try:
        chat_id = poll_data.get("chat_id") or (
            update.effective_message.chat.id if update.effective_message else None
        )
        clicker = update.effective_user
        clicker_id = clicker.id if clicker else None
        clicker_name = (
            (clicker.full_name if clicker else None)
            or (clicker.username if clicker else None)
            or (f"User{clicker_id}" if clicker_id else "")
        )
        saved = await sheets_orders.snapshot_from_poll(
            poll_id, chat_id,
            clicker_user_id=clicker_id,
            clicker_username=clicker_name,
        )
        # First clicker wins: only the person who actually created the order
        # row (its user_id) is the payer. A later clicker leaves the order
        # untouched, so we must not record them as a payer either.
        is_first_clicker = bool(saved) and str(saved.get("user_id")) == str(clicker_id)
        if is_first_clicker:
            # Record the clicker as a payer (who pays / collects this order).
            # Best-effort: a payer-tab problem must never report the order as failed.
            try:
                await sheets_payers.record_payment(
                    clicker_id,
                    username=(clicker.username if clicker else "") or "",
                    full_name=(clicker.full_name if clicker else "") or "",
                )
            except Exception as e:
                logger.warning(f"record_payment failed for poll {poll_id} (non-fatal): {e}")
        return None
    except Exception as e:
        logger.exception(f"Order snapshot failed for poll {poll_id}: {e}")
        return str(e)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks (Order / Close Order)."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if not query.data:
        return

    if query.data.startswith("order_"):
        poll_id = query.data.replace("order_", "")
        poll_data = await get_poll_data(poll_id)
        if not poll_data:
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return

        order_items = await get_global_orders(poll_id)
        order_items = {item: count for item, count in order_items.items() if count > 0}
        if not order_items:
            await query.message.reply_text(ERROR_NO_ORDERS)
            return

        user_selections_data = await get_user_selections(poll_id)
        order_name = await sheets_settings.get("ORDER_NAME", ORDER_NAME)
        # Per-chat order-summary template, falling back to the global style.
        style = await sheets_chat_settings.get(
            poll_data.get("chat_id"), "ORDER_SUMMARY_STYLE", "1",
        )
        order_summary = format_order_summary(
            order_items, order_name, user_selections_data, style=style,
        )

        # Persist a snapshot of current votes to the `order` tab.
        snapshot_error = await _take_order_snapshot(update, poll_data, poll_id)

        try:
            await with_retry(query.message.reply_text, order_summary)
            logger.info(f"Order summary sent for poll {poll_id}")
        except Exception as e:
            logger.error(f"Error sending order summary: {e}")
            await query.message.reply_text(f"Error sending order summary: {str(e)}")

        # If the order snapshot failed, post a follow-up so it isn't silent.
        if snapshot_error:
            try:
                await query.message.reply_text(
                    f"⚠️ Order summary sent, but writing to the `order` sheet failed: "
                    f"{snapshot_error}"
                )
            except Exception:
                pass

    elif query.data.startswith("close_order_"):
        poll_id = query.data.replace("close_order_", "")
        poll_data = await get_poll_data(poll_id)
        if not poll_data:
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return

        # Snapshot before closing so the `order` tab always has a row for
        # every poll that's been closed. No-op if there are no votes.
        snapshot_error = await _take_order_snapshot(update, poll_data, poll_id)

        try:
            await hide_order_buttons(context, poll_id)
            await query.message.reply_text(ORDER_CLOSED_MESSAGE)
            await sheets_events.emit(
                "ORDER_CLOSED",
                entity_type="poll", entity_id=poll_id,
                user_id=update.effective_user.id if update.effective_user else None,
            )
            logger.info(f"Order closed for poll {poll_id}")
        except Exception as e:
            logger.error(f"Error closing order for poll {poll_id}: {e}")
            await query.message.reply_text(f"Error closing order: {str(e)}")

        if snapshot_error:
            try:
                await query.message.reply_text(
                    f"⚠️ Order closed, but writing to the `order` sheet failed: "
                    f"{snapshot_error}"
                )
            except Exception:
                pass

    elif query.data.startswith("paycfg_"):
        action = query.data.replace("paycfg_", "")
        chat_id = query.message.chat.id

        if action == "source_here":
            await sheets_settings.set("PAYMENT_SOURCE_CHAT_ID", str(chat_id))
            await query.answer("Source chat updated to current group!")
            text, markup = await _build_payment_bot_config_view(chat_id)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

        elif action == "target_here":
            await sheets_settings.set("PAYMENT_TARGET_CHAT_ID", str(chat_id))
            await query.answer("Target chat updated to current group!")
            text, markup = await _build_payment_bot_config_view(chat_id)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

        elif action == "pick_target":
            chats = await sheets_repo.list_all("chat") if is_configured() else []
            keyboard = []
            for c in chats:
                cid = str(c.get("chat_id", "")).strip()
                title = str(c.get("title", "")).strip() or f"Chat {cid}"
                if cid:
                    keyboard.append([InlineKeyboardButton(f"🎯 {title}", callback_data=f"paycfg_settgt_{cid}")])
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="paycfg_refresh")])
            await query.answer()
            try:
                await query.edit_message_text(
                    "🌐 <b>Select Target Group to Send Receipts:</b>",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        elif action.startswith("settgt_"):
            tgt_cid = action.replace("settgt_", "")
            await sheets_settings.set("PAYMENT_TARGET_CHAT_ID", tgt_cid)
            await query.answer(f"Target chat set to {tgt_cid}!")
            text, markup = await _build_payment_bot_config_view(chat_id)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

        elif action == "test":
            from .payway import PayWayTransaction
            from .settlement import format_receipt_message
            tgt_chat = await sheets_settings.get("PAYMENT_TARGET_CHAT_ID", "")
            send_to = int(tgt_chat) if (tgt_chat and tgt_chat.strip()) else chat_id

            test_tx = PayWayTransaction(
                amount=0.10, currency="USD", amount_usd=0.10,
                sender_name="HEN TY", account_mask="*859",
                date_str="Aug 24, 11:37 AM", payment_method="ABA KHQR (Test)",
                merchant="HEN TY", trx_id="TEST_999999", apv="999999", raw_text=""
            )
            test_user = {"username": "ahh_tiii", "full_name": "Tii ♏️"}
            test_settled = [{"date": "2026-08-24", "amount": 0.10, "status": "PAID"}]
            msg = format_receipt_message(test_user, test_tx, test_settled, remaining_balance=0.0)

            try:
                await context.bot.send_message(chat_id=send_to, text=f"🧪 <i>[TEST PAYMENT RECEIPT]</i>\n\n{msg}", parse_mode="HTML")
                await query.answer(f"Test receipt sent to chat {send_to}!")
            except Exception as e:
                await query.answer(f"Error sending test: {e}", show_alert=True)

        elif action == "refresh":
            await query.answer()
            text, markup = await _build_payment_bot_config_view(chat_id)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe chat and show welcome message."""
    try:
        chat = update.effective_chat
        user = update.effective_user
        await _record_user(update)
        await add_chat_for_scheduled_messages(
            chat.id,
            title=chat.title or chat.full_name or "",
            chat_type=chat.type or "",
            subscribed_by=user.id if user else None,
        )
        welcome = await sheets_settings.get("WELCOME_MESSAGE", WELCOME_MESSAGE)
        await update.message.reply_text(
            welcome, reply_markup=_calendar_keyboard(chat, context.bot.username),
        )
        logger.info(f"Start command from user {user.id if user else '?'}")
    except Exception as e:
        logger.error(f"Error handling start command: {e}")


async def handle_subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe current chat to reminders."""
    try:
        chat = update.effective_chat
        user = update.effective_user
        await _record_user(update)
        await add_chat_for_scheduled_messages(
            chat.id,
            title=chat.title or chat.full_name or "",
            chat_type=chat.type or "",
            subscribed_by=user.id if user else None,
        )
        await update.message.reply_text("This chat is subscribed to reminders.")
        logger.info(f"Subscribed chat {chat.id}")
    except Exception as e:
        logger.error(f"Error handling subscribe command: {e}")


async def handle_unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe current chat from reminders."""
    try:
        chat = update.effective_chat
        user = update.effective_user
        await _record_user(update)
        await remove_chat_from_scheduled_messages(
            chat.id, user_id=user.id if user else None,
        )
        await update.message.reply_text("This chat is unsubscribed from reminders.")
        logger.info(f"Unsubscribed chat {chat.id}")
    except Exception as e:
        logger.error(f"Error handling unsubscribe command: {e}")


async def handle_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminder text immediately for testing."""
    try:
        await send_scheduled_message(context)
        await update.message.reply_text("Debug message sent!")
        logger.info("Debug reminder message sent manually")
    except Exception as e:
        logger.error(f"Error in debug_send command: {e}")


async def handle_debug_qr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Vongsa QR reminder immediately for testing."""
    try:
        await send_vongsa_qr_now(context)
        await update.message.reply_text("Debug QR reminder sent!")
        logger.info("Debug QR reminder sent manually")
    except Exception as e:
        logger.error(f"Error in debug_qr command: {e}")


async def handle_pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /vongsa command and send Vongsa KHQR image."""
    chat_id = update.effective_chat.id
    qr_path = Path(__file__).parent.parent / "assets" / "payment_qr.png"

    pay_message = (
        "*Vongsa Hourt Payment (KHQR)*\n\n"
        "Please scan the QR code below to pay Vongsa Hourt via KHQR."
    )

    try:
        if qr_path.exists():
            with open(qr_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=pay_message,
                    parse_mode="Markdown",
                )
        else:
            await update.message.reply_text("QR image not found.")
            logger.warning(f"QR image not found at {qr_path}")
        logger.info(f"/vongsa command used by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error handling /vongsa command: {e}")
        await update.message.reply_text("Could not send payment info right now.")


async def handle_ty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ty command and send Ty KHQR image."""
    chat_id = update.effective_chat.id
    qr_path = Path(__file__).parent.parent / "assets" / "ty_qr.png"

    pay_message = (
        "*TY HEN Payment (KHQR)*\n\n"
        "Please scan the QR code below to pay Ty Hen via KHQR."
    )

    try:
        if qr_path.exists():
            with open(qr_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=pay_message,
                    parse_mode="Markdown",
                )
        else:
            await update.message.reply_text("TY QR image not found.")
            logger.warning(f"TY QR image not found at {qr_path}")
        logger.info(f"/ty command used by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error handling /ty command: {e}")
        await update.message.reply_text("Could not send payment info right now.")


# ---------------------------------------------------------------------------
# Admin commands (Phase 4)
# ---------------------------------------------------------------------------

def _split_command_args(text: str, max_parts: int) -> list[str]:
    """Split the message text into [command, arg1, arg2, ...] up to max_parts."""
    return (text or "").split(maxsplit=max_parts - 1)


@admin_only
async def handle_set_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/set <KEY> <value...>` — update a `setting` tab row."""
    if not is_configured():
        await update.message.reply_text(
            "Settings storage requires Google Sheets to be configured."
        )
        return

    parts = _split_command_args(update.message.text or "", 3)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /set <KEY> <value>")
        return
    key = parts[1].strip()
    value = parts[2].strip()

    existing = await sheets_repo.find_by_pk("setting", key)
    user = update.effective_user
    row = {
        "key": key,
        "value": value,
        "value_type": (existing or {}).get("value_type", "string"),
        "description": (existing or {}).get("description", ""),
        "updated_at": sheets_repo.now_iso(),
        "updated_by": user.id if user else "",
    }
    await sheets_repo.upsert("setting", row)
    await sheets_events.emit(
        "SETTING_UPDATED",
        entity_type="setting", entity_id=key,
        user_id=user.id if user else None,
        payload={
            "new_value": value,
            "old_value": (existing or {}).get("value", ""),
        },
    )
    await update.message.reply_text(f"Set `{key}` = `{value}`", parse_mode="Markdown")


@admin_only
async def handle_schedule_list_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_configured():
        await update.message.reply_text(
            "Schedule management requires Google Sheets to be configured."
        )
        return
    rows = await sheets_repo.list_all("schedule")
    if not rows:
        await update.message.reply_text("No schedules defined.")
        return
    lines = ["Schedules:"]
    for r in rows:
        flag = "✓" if str(r.get("is_active", "")).upper() == "TRUE" else "✗"
        lines.append(
            f"{flag}  {r.get('schedule_id', '?')}  "
            f"{r.get('time_of_day', '?')}  {r.get('days_of_week', '?')}  "
            f"({r.get('action_type', '?')})"
        )
    await update.message.reply_text("\n".join(lines))


async def _toggle_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, active: bool
) -> None:
    parts = _split_command_args(update.message.text or "", 2)
    if len(parts) < 2 or not parts[1].strip():
        usage = "enable" if active else "disable"
        await update.message.reply_text(f"Usage: /schedule_{usage} <schedule_id>")
        return
    if not is_configured():
        await update.message.reply_text(
            "Schedule management requires Google Sheets to be configured."
        )
        return
    sid = parts[1].strip()
    result = await sheets_repo.update(
        "schedule", sid, {"is_active": "TRUE" if active else "FALSE"},
    )
    if result is None:
        await update.message.reply_text(f"No schedule with id '{sid}'.")
        return

    await reload_schedules(context.application)
    user = update.effective_user
    await sheets_events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=sid,
        user_id=user.id if user else None,
        payload={"action": "enable" if active else "disable"},
    )
    verb = "Enabled" if active else "Disabled"
    await update.message.reply_text(f"{verb} schedule '{sid}'.")


@admin_only
async def handle_schedule_enable_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _toggle_schedule(update, context, active=True)


@admin_only
async def handle_schedule_disable_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _toggle_schedule(update, context, active=False)


async def _build_payment_bot_config_view(current_chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build text and keyboard for /setup_payment_bot."""
    bot_name = await sheets_settings.get("PAYMENT_BOT_USERNAME", "PayWayByABA_bot")
    src_chat = await sheets_settings.get("PAYMENT_SOURCE_CHAT_ID", "")
    tgt_chat = await sheets_settings.get("PAYMENT_TARGET_CHAT_ID", "")

    chats = await sheets_repo.list_all("chat") if is_configured() else []
    chat_titles = {str(c.get("chat_id", "")).strip(): str(c.get("title", "")).strip() for c in chats}

    src_label = chat_titles.get(src_chat, src_chat) if src_chat else "All Chats (Any Group)"
    tgt_label = chat_titles.get(tgt_chat, tgt_chat) if tgt_chat else "Current / Same Group"

    text = (
        "⚙️ <b>Payment Bot & Target Group Setup</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Source Bot:</b> @{bot_name}\n"
        f"📥 <b>Source Group (Transactions):</b> <code>{src_label}</code>\n"
        f"📤 <b>Target Group (Send Receipts):</b> <code>{tgt_label}</code>\n\n"
        "<i>Transactions from @PayWayByABA_bot will be parsed and receipts will be sent to the target group.</i>\n\n"
        "Choose an action below to update:"
    )

    keyboard = [
        [
            InlineKeyboardButton("📥 Listen in This Chat", callback_data="paycfg_source_here"),
            InlineKeyboardButton("📤 Send Receipts Here", callback_data="paycfg_target_here"),
        ],
        [
            InlineKeyboardButton("🌐 Choose Target Group", callback_data="paycfg_pick_target"),
            InlineKeyboardButton("🧪 Send Test Receipt", callback_data="paycfg_test"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="paycfg_refresh"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


@admin_only
async def handle_setup_payment_bot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/setup_payment_bot` — configure source bot and target group for payment receipts."""
    if not is_configured():
        await update.message.reply_text("Settings storage requires Google Sheets to be configured.")
        return

    chat_id = update.effective_chat.id
    parts = _split_command_args(update.message.text or "", 3)

    if len(parts) >= 2:
        subcmd = parts[1].strip().lower()
        if subcmd == "target" and len(parts) >= 3:
            target_val = parts[2].strip()
            if target_val.lower() == "here":
                target_val = str(chat_id)
            await sheets_settings.set("PAYMENT_TARGET_CHAT_ID", target_val)
            await update.message.reply_text(f"✅ Set `PAYMENT_TARGET_CHAT_ID` = `{target_val}`", parse_mode="Markdown")
            return
        elif subcmd == "source" and len(parts) >= 3:
            source_val = parts[2].strip()
            if source_val.lower() == "here":
                source_val = str(chat_id)
            await sheets_settings.set("PAYMENT_SOURCE_CHAT_ID", source_val)
            await update.message.reply_text(f"✅ Set `PAYMENT_SOURCE_CHAT_ID` = `{source_val}`", parse_mode="Markdown")
            return
        elif subcmd == "bot" and len(parts) >= 3:
            bot_val = parts[2].strip().lstrip("@")
            await sheets_settings.set("PAYMENT_BOT_USERNAME", bot_val)
            await update.message.reply_text(f"✅ Set `PAYMENT_BOT_USERNAME` = `@{bot_val}`", parse_mode="Markdown")
            return

    text, reply_markup = await _build_payment_bot_config_view(chat_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")



async def handle_app_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/app` — open the Mini App. Works in both private chats and groups.

    Telegram only allows `web_app` inline buttons in PRIVATE chats. In groups
    a web_app button raises Button_type_invalid, so there we use a normal URL
    button to the bot's direct-link Mini App (t.me/<bot>?startapp), which
    requires the Main Mini App to be configured in BotFather.
    """
    if not update.message:
        return

    if not MINIAPP_URL:
        await update.message.reply_text(
            "Mini App requires MINIAPP_URL (or WEBHOOK_URL) to be configured "
            "(set it to the frontend's public HTTPS URL)."
        )
        return

    # Telegram WebAppInfo requires an https:// URL; be robust about the scheme.
    base_url = MINIAPP_URL
    if not base_url.startswith("https://") and not base_url.startswith("http://"):
        base_url = f"https://{base_url}"

    chat = update.effective_chat
    is_private = bool(chat) and chat.type == "private"

    try:
        if is_private:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🍱 Open Food Bot", web_app=WebAppInfo(url=f"{base_url}/")),
            ]])
        else:
            # Group/supergroup: web_app buttons are private-only → link to the
            # direct-link Mini App instead (needs Main Mini App set in BotFather).
            # Pass the chat id via startapp so the Mini App scopes to this chat.
            username = context.bot.username
            if username and chat:
                link = f"https://t.me/{username}?startapp={encode_chat_param(chat.id)}"
            elif username:
                link = f"https://t.me/{username}?startapp"
            else:
                link = f"{base_url}/"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🍱 Open Food Bot", url=link),
            ]])

        await update.message.reply_text(
            "Tap below to open the Food Bot calendar.",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Error in handle_app_command: {e}", exc_info=True)
        await update.message.reply_text(
            f"Failed to open Mini App: {str(e)}"
        )


@admin_only
async def handle_admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/admin` — open the Mini App admin panel via a WebApp button."""
    if not update.message:
        return

    if not MINIAPP_URL:
        await update.message.reply_text(
            "Mini App requires MINIAPP_URL (or WEBHOOK_URL) to be configured "
            "(set it to the frontend's public HTTPS URL)."
        )
        return

    # Telegram WebAppInfo requires an https:// URL; be robust about the scheme.
    base_url = MINIAPP_URL
    if not base_url.startswith("https://") and not base_url.startswith("http://"):
        base_url = f"https://{base_url}"

    try:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Open admin panel", web_app=WebAppInfo(url=f"{base_url}/")),
        ]])
        await update.message.reply_text(
            "Tap below to open the Food Bot admin panel.",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Error in handle_admin_command: {e}", exc_info=True)
        await update.message.reply_text(
            f"Failed to open admin panel: {str(e)}"
        )


async def handle_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/ai <query>` — ask the internal assistant about your own orders,
    invoices, polls, or how to use the bot. Same pipeline as /api/ai."""
    if not update.message:
        return

    # Extract the user prompt
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /ai <your question>\n"
            "Examples:\n"
            "  /ai how much did I spend this month?\n"
            "  /ai what did I order last week?\n"
            "  /ai how do I create a food poll?"
        )
        return

    user_query = parts[1].strip()
    user = update.effective_user

    # Notify that bot is processing
    processing_msg = await update.message.reply_text("Thinking... 🤖")

    try:
        user_info = {
            "id": user.id,
            "username": user.username or "",
            "full_name": user.full_name or f"User{user.id}",
        }
        result = await ai.answer_query(user_query, user_info)
        await processing_msg.edit_text(result["response"], parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in handle_ai_command: {e}", exc_info=True)
        await processing_msg.edit_text(f"Sorry, I encountered an error processing your AI query: {str(e)}")


async def handle_exchange_rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/exchange_rate` — the National Bank of Cambodia's official rate.

    Answers from the stored rate (refreshed daily by the scheduler), so it
    never waits on nbc.gov.kh. NBC doesn't publish at weekends or on
    holidays, so the reply says which date the rate was published on rather
    than implying it is today's.
    """
    if not update.message:
        return

    row = await exchange.current()
    if not row:
        await update.message.reply_text(
            "😕 I don't have the exchange rate yet — it's fetched from the "
            "National Bank of Cambodia once a day. Please try again later.\n\n"
            "មិនទាន់មានអត្រាប្តូរប្រាក់នៅឡើយទេ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ។"
        )
        return

    rate_date = row["rate_date"]
    lines = [
        "🏦 *National Bank of Cambodia — Official Exchange Rate*",
        "",
        f"📅 Date: *{rate_date}*",
        f"💵 Rate: *{exchange.format_rate(row['usd_khr'])}*",
        "",
        # Invoice-style examples: rounded exactly as an invoice would round
        # them, so the numbers here match what people are actually asked to
        # pay. (A raw "$1 = 4,047៛" line would contradict the invoice.)
        f"_On invoices:_ $1.00 ≈ {exchange.format_khr(exchange.to_khr(1, row['usd_khr']))} · "
        f"$5.00 ≈ {exchange.format_khr(exchange.to_khr(5, row['usd_khr']))}",
        f"_(riel rounded to the nearest {KHR_ROUNDING}៛)_" if KHR_ROUNDING > 1 else "",
    ]
    if rate_date != exchange.today().isoformat():
        lines += [
            "",
            "ℹ️ This is the most recent published rate — NBC doesn't publish "
            "on weekends or public holidays.",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all so a handler exception is logged, not left unhandled."""
    logger.error("Unhandled exception while processing update", exc_info=context.error)


def setup_handlers(application) -> None:
    """Register all handlers to the bot application."""
    # Member-facing commands
    application.add_handler(CommandHandler("start", handle_start_command))
    application.add_handler(CommandHandler("subscribe", handle_subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", handle_unsubscribe_command))
    application.add_handler(CommandHandler("debug_send", handle_debug_command))
    application.add_handler(CommandHandler("debug_qr", handle_debug_qr_command))
    application.add_handler(CommandHandler("vongsa", handle_pay_command))
    application.add_handler(CommandHandler("ty", handle_ty_command))
    application.add_handler(CommandHandler("app", handle_app_command))
    application.add_handler(CommandHandler("ai", handle_ai_command))
    application.add_handler(CommandHandler("exchange_rate", handle_exchange_rate_command))

    # Admin commands (decorated with @admin_only)
    application.add_handler(CommandHandler("admin", handle_admin_command))
    application.add_handler(CommandHandler("set", handle_set_command))
    application.add_handler(CommandHandler("setup_payment_bot", handle_setup_payment_bot_command))
    application.add_handler(CommandHandler("payment_bot", handle_setup_payment_bot_command))
    application.add_handler(CommandHandler("schedule_list", handle_schedule_list_command))
    application.add_handler(CommandHandler("schedule_enable", handle_schedule_enable_command))
    application.add_handler(CommandHandler("schedule_disable", handle_schedule_disable_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    application.add_error_handler(handle_error)

    logger.info("All handlers registered successfully")
