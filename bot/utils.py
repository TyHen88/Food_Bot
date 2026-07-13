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
# The (?!\)) lookahead rejects a digit immediately followed by ")", so the
# bot's own order summary \u2014 which lists items as "1) item x 1" \u2014 isn't
# mistaken for a menu when a user re-sends it. Real menus use "1. x" / "1 x".
_NUMERAL_PATTERN = re.compile(r"^[\u17e1\u17e2\u17e3\u17e4\u17e5\u17e61-6](?!\))\.?\s*")

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

def _voter_map(
    order_items: Dict[str, int],
    user_selections: Optional[Dict[int, Dict[str, Any]]],
) -> Dict[str, List[str]]:
    """Map item → [voters] for any items that appear in `order_items`."""
    item_voters: Dict[str, List[str]] = {}
    if not user_selections:
        return item_voters
    for user_id, user_data in user_selections.items():
        user_name = user_data.get("name", f"User{user_id}")
        for item in user_data.get("selections", []):
            if item in order_items:
                item_voters.setdefault(item, []).append(user_name)
    return item_voters


def _format_classic(
    order_items: Dict[str, int],
    order_name: str,
    user_selections: Optional[Dict[int, Dict[str, Any]]],
) -> str:
    """Style 1 — Receipt-like, two clearly separated sections."""
    sep = "━" * 22
    lines = [
        f"🛒 Name: {order_name}",
        sep,
        "🍱 Order",
    ]
    for idx, (item, qty) in enumerate(order_items.items(), start=1):
        lines.append(f"   {idx}) {item} × {qty}")

    voters = _voter_map(order_items, user_selections)
    if voters:
        lines.append(sep)
        lines.append("👥 Detail")
        for item, qty in order_items.items():
            if voters.get(item):
                lines.append(f"   • {item} × {qty} ({', '.join(voters[item])})")

    lines.append(sep)
    total = sum(order_items.values())
    lines.append(f"Total: {total} dish{'es' if total != 1 else ''}")
    return "\n".join(lines)


def _format_compact(
    order_items: Dict[str, int],
    order_name: str,
    user_selections: Optional[Dict[int, Dict[str, Any]]],
) -> str:
    """Style 2 — Chat-friendly one-line-per-item."""
    voters = _voter_map(order_items, user_selections)
    lines = [f"🛒 {order_name}'s order", ""]
    for item, qty in order_items.items():
        tail = f" — {', '.join(voters[item])}" if voters.get(item) else ""
        lines.append(f"• {item} × {qty}{tail}")

    total = sum(order_items.values())
    people = len({n for v in voters.values() for n in v}) if voters else 0
    summary = f"📦 {total} dish{'es' if total != 1 else ''}"
    if people:
        summary += f" · {people} {'people' if people != 1 else 'person'}"
    lines.extend(["", summary])
    return "\n".join(lines)


def _format_card(
    order_items: Dict[str, int],
    order_name: str,
    user_selections: Optional[Dict[int, Dict[str, Any]]],
) -> str:
    """Style 3 — Per-item card blocks with header box."""
    voters = _voter_map(order_items, user_selections)
    inner = f" 🍴 Food Order — for {order_name} "
    top = "╭" + "─" * (len(inner)) + "╮"
    mid = "│" + inner + "│"
    bot = "╰" + "─" * (len(inner)) + "╯"
    lines = [top, mid, bot, ""]

    for item, qty in order_items.items():
        lines.append(f"🥢 {item}")
        tail = f" · {', '.join(voters[item])}" if voters.get(item) else ""
        lines.append(f"    × {qty}{tail}")
        lines.append("")

    total = sum(order_items.values())
    lines.append("━" * 18)
    lines.append(f"Total: {total} item{'s' if total != 1 else ''}")
    return "\n".join(lines)


_STYLE_DISPATCH = {
    "1": _format_classic,
    "classic": _format_classic,
    "2": _format_compact,
    "compact": _format_compact,
    "3": _format_card,
    "card": _format_card,
}


def format_order_summary(
    order_items: Dict[str, int],
    order_name: str = "Seyha",
    user_selections: Optional[Dict[int, Dict[str, Any]]] = None,
    style: str = "1",
) -> str:
    """
    Render the order summary in the chosen template style.
        "1" / "classic" — receipt-style, two sections (default)
        "2" / "compact" — chat-friendly single list
        "3" / "card"    — boxed header + per-item cards
    Unknown values fall back to "1".
    """
    if not order_items:
        return ""
    formatter = _STYLE_DISPATCH.get(str(style).strip().lower(), _format_classic)
    return formatter(order_items, order_name, user_selections)

def remove_job_if_exists(name: str, application: Application) -> bool:
    """Remove job with given name from the job queue."""
    current_jobs = application.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True
