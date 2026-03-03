"""
Utility functions for the Telegram Food Poll Bot.
"""

import asyncio
import re
import logging
from typing import List, Dict, Any, Optional
from telegram.error import NetworkError, TimedOut
from telegram import Update
from telegram.ext import ContextTypes, Application

logger = logging.getLogger(__name__)

async def with_retry(func, *args, max_retries: int = 3, **kwargs):
    """Execute a function with retry logic for network operations."""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except (NetworkError, TimedOut) as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed after {max_retries} attempts: {e}")
                raise
            logger.warning(f"Network error: {e}. Retrying in {2**attempt} seconds...")
            await asyncio.sleep(2**attempt)

# Khmer digits ១-៦ plus Arabic 1-6
_NUMERAL_PATTERN = re.compile(r"^[\u17e1\u17e2\u17e3\u17e4\u17e5\u17e61-6]\.?\s*")

def extract_menu_options(text: str) -> List[str]:
    """Extract menu options from text.

    Accepts lines starting with a Khmer or Arabic numeral (1-6),
    followed by an optional dot and any amount of whitespace.
    Works for both '1. Option' and '1 Option' formats.
    """
    options = []
    for line in text.split("\n"):
        line = line.strip()
        m = _NUMERAL_PATTERN.match(line)
        if m:
            option_text = line[m.end():].strip()
            if option_text and option_text not in options:
                options.append(option_text)
    return options

def is_food_menu_text(text: str) -> bool:
    """Check if text appears to be a food menu.

    Returns True if the text starts with 'ម្ហូបថ្ងៃ' OR contains
    at least 2 numbered lines (with or without a dot after the number).
    """
    if not text:
        return False
    text = text.strip()
    # Quick check: starts with the Khmer phrase for "today's food"
    if text.startswith("ម្ហូបថ្ងៃ"):
        return True
    # Count numbered lines
    numbered = [l for l in text.split("\n") if _NUMERAL_PATTERN.match(l.strip())]
    return len(numbered) >= 2

def format_order_summary(
    order_items: Dict[str, int],
    order_name: str = "Seyha",
    user_selections: Optional[Dict[int, Dict[str, Any]]] = None,
) -> str:
    """Format order items into a readable summary with voter details."""
    if not order_items:
        return ""

    order_lines = [f"- {item} x{qty}" for item, qty in order_items.items()]

    summary_lines = [
        f"🛒 Name: {order_name}",
        "------------------",
        *order_lines,
        "------------------",
    ]

    if user_selections:
        summary_lines.append("Detail:")
        item_voters: Dict[str, List[str]] = {}
        for user_id, user_data in user_selections.items():
            user_name = user_data.get("name", f"User{user_id}")
            selections = user_data.get("selections", [])
            for item in selections:
                if item in order_items:
                    item_voters.setdefault(item, []).append(user_name)

        for item, qty in order_items.items():
            voters = item_voters.get(item, [])
            if voters:
                summary_lines.append(f"- {item} x{qty} ({', '.join(voters)})")

    return "\n".join(summary_lines)

def remove_job_if_exists(name: str, application: Application) -> bool:
    """Remove job with given name from the job queue."""
    current_jobs = application.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True
