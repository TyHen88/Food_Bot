"""
Message and callback handlers for the Telegram Food Poll Bot.
"""

import logging
import os
from pathlib import Path
from telegram import Update
from telegram.ext import (
    ContextTypes, MessageHandler, filters, PollAnswerHandler, 
    CallbackQueryHandler, CommandHandler
)
from .config import (
    WELCOME_MESSAGE, DAILY_MESSAGE, ERROR_POLL_NOT_FOUND, 
    ERROR_NO_ORDERS, ERROR_NO_SELECTION, ORDER_NAME, CLOSE_ORDER_BUTTON_TEXT, ORDER_CLOSED_MESSAGE
)
from .utils import is_food_menu_text, format_order_summary, with_retry
from .menu_processor import (
    process_food_menu, get_poll_data, get_global_orders, 
    update_user_selection, update_global_orders, get_user_selections, hide_order_buttons
)
from .scheduler import send_scheduled_message, add_chat_for_scheduled_messages

logger = logging.getLogger(__name__)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle incoming messages and forwarded messages.
    
    Args:
        update: Telegram update object
        context: Bot context
    """
    logger.info(f"Received message: {update.message.text if update.message and update.message.text else 'No text'}")
    
    if not update.message or not update.message.text:
        logger.info("No message or no text, skipping")
        return
    
    text = update.message.text.strip()
    logger.info(f"Processing text: {repr(text)}")
    
    # Check if this is a food menu text
    if is_food_menu_text(text):
        logger.info(f"Processing food menu from user {update.effective_user.id}")
        await process_food_menu(update, context, text)
    else:
        logger.info(f"Text not recognized as food menu: {repr(text)}")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle poll answer updates and update global order counts.
    
    Args:
        update: Telegram update object
        context: Bot context
    """
    poll_answer = update.poll_answer
    
    if not poll_answer or not poll_answer.user:
        logger.warning("Received poll answer without user information")
        return
    
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    user_name = poll_answer.user.full_name or poll_answer.user.username or f'User{user_id}'
    selected_options = poll_answer.option_ids
    
    # Get poll data
    poll_data = get_poll_data(poll_id)
    if not poll_data:
        logger.warning(f"Poll data not found for poll ID: {poll_id}")
        return
    
    options = poll_data.get("options", [])
    
    # Get previous selections for this user
    user_selections_data = get_user_selections(poll_id)
    previous_selections = user_selections_data.get(user_id, {}).get('selections', [])
    
    # Calculate current selections
    current_selections = [options[idx] for idx in selected_options if idx < len(options)]
    
    # Update user selections with name
    update_user_selection(poll_id, user_id, current_selections, user_name)
    
    # Calculate changes and update global orders
    newly_selected = [item for item in current_selections if item not in previous_selections]
    newly_unselected = [item for item in previous_selections if item not in current_selections]
    
    # Update global order counts
    for item in newly_selected:
        update_global_orders(poll_id, item, 1)
        logger.info(f"User {user_name} selected: {item}")
    
    for item in newly_unselected:
        update_global_orders(poll_id, item, -1)
        logger.info(f"User {user_name} unselected: {item}")
    
    logger.info(f"User {user_name} updated poll {poll_id} selections: {current_selections} (previous: {previous_selections})")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle button clicks (e.g., Order button, Close Order button).
    
    Args:
        update: Telegram update object
        context: Bot context
    """
    query = update.callback_query
    await query.answer()
    
    if not query.data:
        return
    
    # Handle Order button
    if query.data.startswith("order_"):
        poll_id = query.data.replace("order_", "")
        
        # Check if poll exists
        poll_data = get_poll_data(poll_id)
        if not poll_data:
            logger.warning(f"Poll not found for callback: {poll_id}")
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return
        
        # Get global orders for this poll
        order_items = get_global_orders(poll_id)
        order_items = {item: count for item, count in order_items.items() if count > 0}
        
        if not order_items:
            await query.message.reply_text(ERROR_NO_ORDERS)
            return
        
        # Get user selections for detail
        user_selections_data = get_user_selections(poll_id)
        
        # Format and send order summary with voter details
        order_summary = format_order_summary(order_items, ORDER_NAME, user_selections_data)
        
        try:
            await with_retry(query.message.reply_text, order_summary)
            logger.info(f"Order summary sent for poll {poll_id}: {order_items}")
        except Exception as e:
            logger.error(f"Error sending order summary: {e}")
            await query.message.reply_text(f"Error sending order summary: {str(e)}")
    
    # Handle Close Order button
    elif query.data.startswith("close_order_"):
        poll_id = query.data.replace("close_order_", "")
        
        # Check if poll exists
        poll_data = get_poll_data(poll_id)
        if not poll_data:
            logger.warning(f"Poll not found for close order callback: {poll_id}")
            await query.message.reply_text(ERROR_POLL_NOT_FOUND)
            return
        
        try:
            # Hide the order buttons
            await hide_order_buttons(context, poll_id)
            
            # Send confirmation message
            await query.message.reply_text(ORDER_CLOSED_MESSAGE)
            logger.info(f"Order closed for poll {poll_id}")
            
        except Exception as e:
            logger.error(f"Error closing order for poll {poll_id}: {e}")
            await query.message.reply_text(f"Error closing order: {str(e)}")

async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /start command.
    
    Args:
        update: Telegram update object
        context: Bot context
    """
    try:
        # Add chat to scheduled messages
        add_chat_for_scheduled_messages(update.effective_chat.id)
        await update.message.reply_text(WELCOME_MESSAGE)
        logger.info(f"Start command received from user {update.effective_user.id}")
        logger.info(f"Username: {update.effective_user.full_name}")
    except Exception as e:
        logger.error(f"Error handling start command: {e}")

async def handle_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /debug_send command for testing.
    
    Args:
        update: Telegram update object
        context: Bot context
    """
    try:
        await send_scheduled_message(context)
        await update.message.reply_text("Debug message sent!")
        logger.info("Debug message sent manually")
    except Exception as e:
        logger.error(f"Error in debug command: {e}")

async def handle_pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /vongsa command — sends the KHQR payment QR code with a friendly message.
    """
    chat_id = update.effective_chat.id
    qr_path = Path(__file__).parent.parent / "assets" / "payment_qr.png"

    pay_message = (
        "💳 *Vongsa Hourt — ការទូទាត់ប្រាក់* (Payment)\n\n"
        "សូមស្កែន QR Code ខាងក្រោម ដើម្បីទូទាត់ប្រាក់តាម KHQR\n"
        "_Please scan the QR code below to pay Vongsa Hourt via KHQR_\n\n"
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
            await update.message.reply_text(pay_message, parse_mode="Markdown")
            logger.warning(f"QR image not found at {qr_path}")
        logger.info(f"/vongsa command used by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error handling /vongsa command: {e}")
        await update.message.reply_text("Sorry, could not send payment info right now. Please try again later.")


async def handle_ty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /ty command — sends Ty Hen's KHQR payment QR code.
    """
    chat_id = update.effective_chat.id
    qr_path = Path(__file__).parent.parent / "assets" / "ty_qr.png"

    pay_message = (
        "💳 *TY HEN — ការទូទាត់ប្រាក់* (Payment)\n\n"
        "សូមស្កែន QR Code ខាងក្រោម ដើម្បីទូទាត់ប្រាក់តាម KHQR\n"
        "_Please scan the QR code below to pay Ty Hen via KHQR_\n\n"
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
            await update.message.reply_text(pay_message, parse_mode="Markdown")
            logger.warning(f"TY QR image not found at {qr_path}")
        logger.info(f"/ty command used by user {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Error handling /ty command: {e}")
        await update.message.reply_text("Sorry, could not send payment info right now. Please try again later.")


def setup_handlers(application):
    """
    Register all handlers to the bot application.
    
    Args:
        application: Telegram bot application
    """
    # Command handlers
    application.add_handler(CommandHandler("start", handle_start_command))
    application.add_handler(CommandHandler("debug_send", handle_debug_command))
    application.add_handler(CommandHandler("vongsa", handle_pay_command))
    application.add_handler(CommandHandler("ty", handle_ty_command))
    
    # Message handlers (handle all text messages except commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Poll and callback handlers
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    logger.info("All handlers registered successfully")
