#!/usr/bin/env python3
"""
Simple working version of the Telegram Food Poll Bot.
This version avoids the complex setup issues and provides a working bot.
"""

import os
import sys
import logging
import asyncio
import re
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, 
    CallbackQueryHandler, CommandHandler, PollAnswerHandler
)
from telegram.error import NetworkError, TimedOut
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global storage
poll_data = {}
global_orders = {}
user_selections = {}
chat_ids_for_scheduled_messages = set()

# Message templates
WELCOME_MESSAGE = """សួស្តី! ខ្ញុំជា Bot ដែលបង្កើត Poll ដោយស្វ័យប្រវត្តិ។

របៀបប្រើប្រាស់៖
១. ជ្រើសរើសមុខម្ហូបដែលអ្នកចង់ Order
២. Vote មុខម្ហូបរបស់អ្នក
៣. រង់ចាំការជ្រើសរើសរួចរាល់ រួចចុចប៊ូតុង Order 🛒"""

async def with_retry(func, *args, max_retries=3, **kwargs):
    """Execute a function with retry logic for network operations."""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except (NetworkError, TimedOut) as e:
            if attempt == max_retries - 1:
                raise
            logger.warning(f"Network error: {e}. Retrying in {2**attempt} seconds...")
            await asyncio.sleep(2**attempt)

def extract_menu_options(text: str):
    """Extract menu options from text, accepting both "1. Option" and "1 Option" styles."""
    options = []
    for line in text.split('\n'):
        line = line.strip()
        # Accept an optional dot after the number
        if re.match(r'^[១២៣៤៥៦1-6]\.?(?:\s+|\s*$)', line):
            # Remove the number and optional dot plus following whitespace
            option_text = re.sub(r'^[១២៣៤៥៦1-6]\.?(?:\s+|\s*$)', '', line)
            if option_text and option_text not in options:
                options.append(option_text)
    return options

def is_food_menu_text(text: str):
    """Check if text appears to be a food menu, allowing numbered items with or without a dot."""
    if not text:
        return False
    text = text.strip()
    if text.startswith("ម្ហូបថ្ងៃ"):
        return True
    # Accept lines that start with a Khmer or Arabic numeral optionally followed by a dot
    numbered_items = re.findall(r'^[១២៣៤៥៦1-6]\.?.+', text, re.MULTILINE)
    return len(numbered_items) >= 2

async def process_food_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process text that contains a food menu and create a poll."""
    options = extract_menu_options(text)
    
    if len(options) < 2:
        logger.warning(f"Not enough menu options found: {len(options)} options")
        return
    
    try:
        message = await with_retry(
            context.bot.send_poll,
            chat_id=update.effective_chat.id,
            question="ជ្រើសរើសម្ហូបដែលអ្នកចូលចិត្ត!",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
            type=Poll.REGULAR
        )
        
        poll_data[message.poll.id] = {
            "options": options,
            "message_id": message.message_id,
            "chat_id": update.effective_chat.id
        }
        
        global_orders[message.poll.id] = {option: 0 for option in options}
        user_selections[message.poll.id] = {}
        
        keyboard = [[InlineKeyboardButton("🛒 Order", callback_data=f"order_{message.poll.id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await with_retry(
            context.bot.send_message,
            chat_id=update.effective_chat.id,
            text="ចុចប៊ូតុងខាងក្រោមដើម្បីបញ្ជាទិញ៖",
            reply_markup=reply_markup,
            reply_to_message_id=message.message_id
        )
        
        logger.info(f"Created poll with {len(options)} options")
        
    except Exception as e:
        logger.error(f"Error creating poll: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"មានបញ្ហាក្នុងការបង្កើត poll: {str(e)}"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages."""
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    if is_food_menu_text(text):
        logger.info(f"Processing food menu from user {update.effective_user.id}")
        await process_food_menu(update, context, text)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answer updates."""
    poll_answer = update.poll_answer
    if not poll_answer or not poll_answer.user:
        return
    
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    selected_options = poll_answer.option_ids
    
    if poll_id not in poll_data:
        return
    
    options = poll_data[poll_id]["options"]
    current_selections = [options[idx] for idx in selected_options if idx < len(options)]
    
    if poll_id not in user_selections:
        user_selections[poll_id] = {}
    
    previous_selections = user_selections[poll_id].get(user_id, [])
    user_selections[poll_id][user_id] = current_selections
    
    newly_selected = [item for item in current_selections if item not in previous_selections]
    newly_unselected = [item for item in previous_selections if item not in current_selections]
    
    for item in newly_selected:
        global_orders[poll_id][item] += 1
    
    for item in newly_unselected:
        global_orders[poll_id][item] = max(0, global_orders[poll_id][item] - 1)
    
    logger.info(f"User {user_id} updated poll {poll_id} selections")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()
    
    if not query.data or not query.data.startswith("order_"):
        return
    
    poll_id = query.data.replace("order_", "")
    
    if poll_id not in global_orders or poll_id not in poll_data:
        await query.message.reply_text("ខ្ញុំមិនអាចរកឃើញការបោះឆ្នោតនេះទេ។")
        return
    
    order_items = {item: count for item, count in global_orders[poll_id].items() if count > 0}
    
    if not order_items:
        await query.message.reply_text("មិនមានការបញ្ជាទិញណាមួយឡើយ។")
        return
    
    order_lines = [f"- {item} x{qty}" for item, qty in order_items.items()]
    response = "\n".join([
        "🛒 Name: Seyha",
        "------------------",
        *order_lines,
        "------------------",
    ])
    
    await query.message.reply_text(response)

async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_ids_for_scheduled_messages.add(update.effective_chat.id)
    await update.message.reply_text(WELCOME_MESSAGE)
    logger.info(f"Start command received from user {update.effective_user.id}")

async def handle_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /debug_send command for testing."""
    await update.message.reply_text("Debug command received!")

def main():
    """Main function to run the bot."""
    try:
        # Create application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", handle_start_command))
        application.add_handler(CommandHandler("debug_send", handle_debug_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        application.add_handler(PollAnswerHandler(handle_poll_answer))
        
        logger.info("Bot starting...")
        application.run_polling(drop_pending_updates=True)
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main() 