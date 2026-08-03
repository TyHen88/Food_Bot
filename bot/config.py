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

# Webhook Configuration
# If WEBHOOK_URL is set, the bot runs in webhook mode (production / Render).
# If empty, main.py falls back to long polling for local development.
#
# Render injects RENDER_EXTERNAL_URL (https://<service>.onrender.com) at
# runtime. Falling back to it means the first deploy can register its own
# webhook, without a second pass to paste the URL back in as WEBHOOK_URL.
# Set WEBHOOK_URL explicitly to override (e.g. a custom domain).
WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"

# Mini App URL — where the frontend actually lives (e.g. the Vercel app).
# All Telegram web_app buttons open this. Falls back to WEBHOOK_URL for
# setups that serve the frontend from the same process as the bot; on a
# split deployment (backend on Render, frontend on Vercel) leaving this
# unset makes the buttons open the backend, which 404s inside Telegram
# ("This page couldn't load").
MINIAPP_URL = (
    os.getenv("MINIAPP_URL", "").strip().rstrip("/")
    or WEBHOOK_URL
)

# Admin bootstrap: comma-separated Telegram user IDs that get ADMIN role on first run
_admin_raw = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = {
    int(uid) for uid in _admin_raw.split(",") if uid.strip().isdigit()
}

# LOCAL DEV ONLY — bypass Mini-App auth so the panel works in a regular browser.
# Auth helper only honours this when WEBHOOK_URL is empty (local mode).
DEV_BYPASS_AUTH = os.getenv("DEV_BYPASS_AUTH", "").strip().lower() in ("1", "true", "yes")

# CORS — origins allowed to call the /api/* endpoints from a browser.
# Comma-separated list of origins. In production set to your frontend URL.
# Example: https://foodbot.vercel.app,http://localhost:3000
_cors_raw = os.getenv("CORS_ORIGINS", "http://localhost:3000").strip()
CORS_ORIGINS: list[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# Google Sheets (used from Phase 1 onward; safe to be empty in Phase 0)
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

# Ollama API Integration
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "https://ollama.com/api/chat").strip()
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:120b").strip()

# Web search for the AI assistant's *external* questions (Ollama's hosted
# search API — same key as the chat API). Off automatically without a key:
# the assistant then falls back to answering from the model's own knowledge.
OLLAMA_SEARCH_URL = os.getenv(
    "OLLAMA_SEARCH_URL", "https://ollama.com/api/web_search"
).strip()
AI_WEB_SEARCH = os.getenv("AI_WEB_SEARCH", "true").strip().lower() in ("1", "true", "yes")
AI_WEB_SEARCH_RESULTS = int(os.getenv("AI_WEB_SEARCH_RESULTS", "5"))

# Exchange rate — the National Bank of Cambodia's official USD→KHR rate.
# NBC publishes it around 16:30 ICT on working days; there is no official
# JSON API, so bot/exchange.py tries the government open-data endpoint first
# and falls back to parsing NBC's (server-rendered) page.
NBC_EXCHANGE_URL = os.getenv(
    "NBC_EXCHANGE_URL",
    "https://www.nbc.gov.kh/english/economic_research/exchange_rate.php",
).strip()
MEF_EXCHANGE_API_URL = os.getenv(
    "MEF_EXCHANGE_API_URL",
    "https://data.mef.gov.kh/api/v1/public-datasets/pd_66a0cd503e0bd300012638fb4/file",
).strip()
# nbc.gov.kh 403s any client that doesn't look like a browser.
EXCHANGE_USER_AGENT = os.getenv(
    "EXCHANGE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
).strip()
# Sanity band: a parse that drifts outside this is a broken page, not a rate.
# Keeping the previous value is always better than storing a garbage one.
EXCHANGE_RATE_MIN = float(os.getenv("EXCHANGE_RATE_MIN", "3000"))
EXCHANGE_RATE_MAX = float(os.getenv("EXCHANGE_RATE_MAX", "6000"))
# Warn once the newest stored rate is older than this (weekends + a holiday
# are normal; a fortnight means the fetch has been failing silently).
EXCHANGE_RATE_STALE_DAYS = int(os.getenv("EXCHANGE_RATE_STALE_DAYS", "4"))
# Daily refresh, in TIMEZONE. NBC publishes ~16:30, so 17:10 catches the new
# rate the same afternoon.
EXCHANGE_REFRESH_HOUR = int(os.getenv("EXCHANGE_REFRESH_HOUR", "17"))
EXCHANGE_REFRESH_MINUTE = int(os.getenv("EXCHANGE_REFRESH_MINUTE", "10"))
# Riel is quoted in whole notes — round each person's amount to the nearest
# 100៛, the usual retail convention. Set to 1 for exact riel, 0 to disable.
KHR_ROUNDING = int(os.getenv("KHR_ROUNDING", "100"))

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# Render free-tier spin-down workaround: while inside this daily window the
# app self-pings its own /health every KEEPALIVE_INTERVAL seconds so Render
# never sees 15 idle minutes and never puts it to sleep. Hours are in
# TIMEZONE (Asia/Phnom_Penh); end hour is exclusive, so 8 → 13 covers the
# 8:00 reminder through the 12:00 QR with margin. A sleeping instance cannot
# ping itself, so the first wake-up of the day must still come from outside
# (external cron ping, or any Telegram message hitting the webhook).
KEEPALIVE_START_HOUR = int(os.getenv("KEEPALIVE_START_HOUR", "8"))
KEEPALIVE_END_HOUR = int(os.getenv("KEEPALIVE_END_HOUR", "13"))
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "600"))

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
