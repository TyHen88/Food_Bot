"""
Message and callback handlers for the Telegram Food Poll Bot.
"""

import logging
from pathlib import Path

from telegram import Update
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
    ORDER_CLOSED_MESSAGE,
    ORDER_NAME,
    WELCOME_MESSAGE,
)
from .menu_processor import (
    get_global_orders,
    get_poll_data,
    get_user_selections,
    hide_order_buttons,
    process_food_menu,
    update_global_orders,
    update_user_selection,
)
from .scheduler import (
    add_chat_for_scheduled_messages,
    remove_chat_from_scheduled_messages,
    send_scheduled_message,
    send_vongsa_qr_now,
)
from .utils import format_order_summary, is_food_menu_text, with_retry

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages and process menu text."""
    if not update.message or not update.message.text:
        logger.info("No message text, skipping")
        return

    text = update.message.text.strip()
    logger.info(f"Received message text: {repr(text)}")

    if is_food_menu_text(text):
        logger.info(f"Processing food menu from user {update.effective_user.id}")
        await process_food_menu(update, context, text)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle poll answers and maintain aggregated order counts."""
    poll_answer = update.poll_answer
    if not poll_answer or not poll_answer.user:
        logger.warning("Received poll answer without user information")
        return

    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name or poll_answer.user.username or f"User{user_id}"
    selected_options = poll_answer.option_ids

    poll_data = get_poll_data(poll_id)
    if not poll_data:
        logger.warning(f"Poll data not found for poll ID: {poll_id}")
        return

    options = poll_data.get("options", [])
    user_selections_data = get_user_selections(poll_id)
    previous_selections = user_selections_data.get(user_id, {}).get("selections", [])
    current_selections = [options[idx] for idx in selected_options if idx < len(options)]

    update_user_selection(poll_id, user_id, current_selections, user_name)

    newly_selected = [item for item in current_selections if item not in previous_selections]
    newly_unselected = [item for item in previous_selections if item not in current_selections]

    for item in newly_selected:
        update_global_orders(poll_id, item, 1)
    for item in newly_unselected:
        update_global_orders(poll_id, item, -1)

    logger.info(
        f"User {user_name} updated poll {poll_id} selections: "
        f"{current_selections} (previous: {previous_selections})"
    )


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
        poll_data = get_poll_data(poll_id)
        if not poll_data:
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return

        order_items = get_global_orders(poll_id)
        order_items = {item: count for item, count in order_items.items() if count > 0}
        if not order_items:
            await query.message.reply_text(ERROR_NO_ORDERS)
            return

        user_selections_data = get_user_selections(poll_id)
        order_summary = format_order_summary(order_items, ORDER_NAME, user_selections_data)

        try:
            await with_retry(query.message.reply_text, order_summary)
            logger.info(f"Order summary sent for poll {poll_id}")
        except Exception as e:
            logger.error(f"Error sending order summary: {e}")
            await query.message.reply_text(f"Error sending order summary: {str(e)}")

    elif query.data.startswith("close_order_"):
        poll_id = query.data.replace("close_order_", "")
        poll_data = get_poll_data(poll_id)
        if not poll_data:
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return

        try:
            await hide_order_buttons(context, poll_id)
            await query.message.reply_text(ORDER_CLOSED_MESSAGE)
            logger.info(f"Order closed for poll {poll_id}")
        except Exception as e:
            logger.error(f"Error closing order for poll {poll_id}: {e}")
            await query.message.reply_text(f"Error closing order: {str(e)}")


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe chat and show welcome message."""
    try:
        add_chat_for_scheduled_messages(update.effective_chat.id)
        await update.message.reply_text(WELCOME_MESSAGE)
        logger.info(f"Start command from user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error handling start command: {e}")


async def handle_subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe current chat to reminders."""
    try:
        add_chat_for_scheduled_messages(update.effective_chat.id)
        await update.message.reply_text("This chat is subscribed to reminders.")
        logger.info(f"Subscribed chat {update.effective_chat.id}")
    except Exception as e:
        logger.error(f"Error handling subscribe command: {e}")


async def handle_unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe current chat from reminders."""
    try:
        remove_chat_from_scheduled_messages(update.effective_chat.id)
        await update.message.reply_text("This chat is unsubscribed from reminders.")
        logger.info(f"Unsubscribed chat {update.effective_chat.id}")
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


def setup_handlers(application) -> None:
    """Register all handlers to the bot application."""
    application.add_handler(CommandHandler("start", handle_start_command))
    application.add_handler(CommandHandler("subscribe", handle_subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", handle_unsubscribe_command))
    application.add_handler(CommandHandler("debug_send", handle_debug_command))
    application.add_handler(CommandHandler("debug_qr", handle_debug_qr_command))
    application.add_handler(CommandHandler("vongsa", handle_pay_command))
    application.add_handler(CommandHandler("ty", handle_ty_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    logger.info("All handlers registered successfully")
