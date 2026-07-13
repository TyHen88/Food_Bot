"""
Menu processing for the Telegram Food Poll Bot.

State previously held in module-level dicts (`poll_data`, `global_orders`,
`user_selections`, `order_button_used`) now lives in the `poll` and `vote`
sheet tabs via bot.sheets.polls / bot.sheets.votes. Those helpers fall
back to in-memory storage when Sheets isn't configured (local dev).

All public functions are async — handlers await them.
"""

import logging
from typing import Any, Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Poll,
    Update,
)
from telegram.ext import ContextTypes

from .config import (
    CLOSE_ORDER_BUTTON_TEXT,
    ERROR_POLL_CREATION,
    ORDER_BUTTON_TEXT,
    ORDER_INSTRUCTION_TEXT,
    POLL_QUESTION,
)
from .sheets import polls, settings, votes
from .utils import extract_menu_options, with_retry

logger = logging.getLogger(__name__)


async def _post_poll_with_buttons(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    options: List[str],
    question: str,
    created_by: Optional[int] = None,
) -> Optional[str]:
    """Send poll + Order/Close buttons, persist metadata. Returns poll_id."""
    order_label = await settings.get("ORDER_BUTTON_TEXT", ORDER_BUTTON_TEXT)
    close_label = await settings.get("CLOSE_ORDER_BUTTON_TEXT", CLOSE_ORDER_BUTTON_TEXT)
    instruction = await settings.get("ORDER_INSTRUCTION_TEXT", ORDER_INSTRUCTION_TEXT)

    try:
        poll_msg = await with_retry(
            context.bot.send_poll,
            chat_id=chat_id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
            type=Poll.REGULAR,
        )
        poll_id = poll_msg.poll.id

        await polls.create(
            poll_id=poll_id,
            chat_id=chat_id,
            message_id=poll_msg.message_id,
            options=options,
            question=question,
            created_by=created_by,
        )

        keyboard = [[
            InlineKeyboardButton(order_label, callback_data=f"order_{poll_id}"),
            InlineKeyboardButton(close_label, callback_data=f"close_order_{poll_id}"),
        ]]
        button_msg = await with_retry(
            context.bot.send_message,
            chat_id=chat_id,
            text=instruction,
            reply_markup=InlineKeyboardMarkup(keyboard),
            reply_to_message_id=poll_msg.message_id,
        )
        await polls.set_button_message_id(poll_id, button_msg.message_id)

        logger.info(f"Created poll {poll_id} with {len(options)} options")
        return poll_id

    except Exception as e:
        logger.error(f"Error creating poll: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=ERROR_POLL_CREATION.format(str(e)),
            )
        except Exception:
            pass
        return None


async def process_food_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> Optional[str]:
    """
    Parse a menu message into options, create a Telegram poll, attach the
    Order/Close Order buttons, and persist poll metadata.
    Returns the poll_id on success, None otherwise.
    """
    options = extract_menu_options(text)
    if len(options) < 2:
        logger.warning(f"Not enough menu options found: {len(options)}")
        return None

    question = await settings.get("POLL_QUESTION", POLL_QUESTION)
    user = update.effective_user
    return await _post_poll_with_buttons(
        context=context,
        chat_id=update.effective_chat.id,
        options=options,
        question=question,
        created_by=user.id if user else None,
    )


# ---------------------------------------------------------------------------
# Thin async wrappers used by handlers.
# Keeping these in menu_processor preserves the existing import surface.
# ---------------------------------------------------------------------------

async def get_poll_data(poll_id: str) -> Optional[Dict[str, Any]]:
    return await polls.get(poll_id)


async def get_global_orders(poll_id: str) -> Dict[str, int]:
    return await votes.aggregate_orders(poll_id)


async def get_user_selections(poll_id: str) -> Dict[int, Dict[str, Any]]:
    return await votes.get_user_selections_map(poll_id)


async def record_user_vote(
    poll_id: str, user_id: int, user_name: str, selected_options: List[str]
) -> None:
    await votes.record(
        poll_id=poll_id,
        user_id=user_id,
        user_name=user_name,
        selected_options=selected_options,
    )


async def hide_order_buttons(
    context: ContextTypes.DEFAULT_TYPE, poll_id: str
) -> None:
    """Remove the inline keyboard from the Order/Close-Order message and mark the poll CLOSED."""
    poll_info = await polls.get(poll_id)
    if not poll_info:
        logger.warning(f"Poll data not found for hiding buttons: {poll_id}")
        return

    button_message_id = poll_info.get("button_message_id")
    chat_id = poll_info.get("chat_id")
    if not button_message_id or not chat_id:
        logger.warning(f"Button message ID or chat ID missing for poll {poll_id}")
        await polls.close(poll_id)
        return

    try:
        await with_retry(
            context.bot.edit_message_reply_markup,
            chat_id=chat_id,
            message_id=button_message_id,
            reply_markup=None,
        )
    except Exception as e:
        logger.error(f"Error editing reply markup for poll {poll_id}: {e}")
    finally:
        await polls.close(poll_id)
        logger.info(f"Order buttons hidden + poll closed: {poll_id}")
