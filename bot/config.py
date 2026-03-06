"""
Configuration settings for the Telegram Food Poll Bot.
"""

import logging
import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# Timezone Configuration
TIMEZONE = "Asia/Phnom_Penh"
WEEKDAY_REMINDER_MESSAGE_TIME = "8:00"  # 8:00 AM, Monday-Friday
WEEKDAY_VONGSA_QR_TIME = "12:00"  # 12:00 PM, Monday-Friday

# Poll Configuration
POLL_QUESTION = "តើថ្ងៃនេះចង់ញ៉ាំអ្វី?😋🍴"
ORDER_BUTTON_TEXT = "Order"
CLOSE_ORDER_BUTTON_TEXT = "Close Order"
ORDER_INSTRUCTION_TEXT = "Please vote first, then press Order to show the summary."
ORDER_NAME = "Seyha"

# Message Templates
WELCOME_MESSAGE = (
    "សួស្តី! ខ្ញុំជា Food Poll Bot។\n\n"
    "របៀបប្រើ៖\n"
    "- ផ្ញើម៉ឺនុយដែលមានលេខរៀង\n"
    "- Bot នឹងបង្កើត poll អោយ\n"
    "- ចុច Order ដើម្បីមើលសរុបការកុម្ម៉ង់"
)

DAILY_MESSAGE = "តើថ្ងៃនេះបានម្ហូបអ្វី?😋🍴"

# Error Messages
ERROR_POLL_CREATION = "Failed to create poll: {}"
ERROR_POLL_NOT_FOUND = "Poll not found. Please create a new menu poll."
ERROR_NO_ORDERS = "No orders yet."
ERROR_NO_SELECTION = "You haven't selected any food yet!"
ORDER_CLOSED_MESSAGE = "Order has been closed."


def setup_logging() -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )
