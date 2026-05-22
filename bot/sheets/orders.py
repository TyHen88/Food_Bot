"""
Order snapshot persistence — written to the `order` tab when someone
taps the Order button on a poll.

One row per poll (keyed `order_id = poll_id`). The row records the
*clicker* — the person who tapped the Order button — in `user_id` /
`username`. Each voter's selections are folded into the `item` JSON:

    item = [
        {"name": "<voter display name>", "item_name": "<dish>", "qty": 1},
        ...
    ]

Latest clicker wins: re-tapping the button on the same poll upserts the
same row with the new clicker's user_id/username, so the Mini App's
"Paid by" always reflects the most recent person who took ownership.

In-memory fallback for local dev (no Sheets configured) mirrors votes.py.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import repo, votes
from .client import is_configured

logger = logging.getLogger(__name__)

# Fallback for local dev: {poll_id: order_row}
_mem_orders: Dict[str, Dict[str, Any]] = {}


def _today_date() -> str:
    """YYYY-MM-DD in the bot's timezone (best-effort — falls back to UTC)."""
    try:
        from zoneinfo import ZoneInfo
        from ..config import TIMEZONE
        tz = ZoneInfo(TIMEZONE)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def _build_item_json(selections_map: Dict[int, Dict[str, Any]]) -> str:
    """Flatten {user_id: {name, selections}} into the JSON list shape.

    Each entry carries the voter's `user_id` so the Mini App calendar can
    filter an order down to a single member's own dishes (admins see all).
    """
    entries: List[Dict[str, Any]] = []
    for user_id, entry in selections_map.items():
        name = entry.get("name") or f"User{user_id}"
        for sel in entry.get("selections") or []:
            entries.append(
                {"user_id": user_id, "name": name, "item_name": sel, "qty": 1}
            )
    return json.dumps(entries, ensure_ascii=False)


async def snapshot_from_poll(
    poll_id: str,
    chat_id: int,
    *,
    clicker_user_id: Optional[int] = None,
    clicker_username: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Read all current votes for `poll_id` and upsert ONE `order` row
    representing this Order-button click.

    Returns the saved row (or None if there were no votes to snapshot).
    """
    selections_map = await votes.get_user_selections_map(poll_id)
    if not selections_map:
        return None

    item_json = _build_item_json(selections_map)
    if not item_json or item_json == "[]":
        return None

    row = {
        "order_id": poll_id,
        "poll_id": poll_id,
        "chat_id": chat_id,
        "user_id": clicker_user_id or "",
        "username": clicker_username or "",
        "item": item_json,
        "order_date": _today_date(),
        "created_at": repo.now_iso(),
    }

    if is_configured():
        logger.info(
            f"snapshot_from_poll: writing order row poll_id={poll_id} "
            f"clicker={clicker_user_id} ({clicker_username!r}) "
            f"item_count={len(json.loads(item_json))}"
        )
        # Block on the actual Sheets write so any error (missing tab,
        # permission denied, column mismatch) surfaces to the caller
        # instead of vanishing into a background task.
        await repo.upsert_blocking("order", row)
        logger.info(f"snapshot_from_poll: order row written for poll {poll_id}")
    else:
        _mem_orders[poll_id] = row
        logger.info(f"snapshot_from_poll: stored in-memory for poll {poll_id}")
    return row


async def get_by_poll(poll_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest order row for a poll, or None."""
    if is_configured():
        return await repo.find_by_pk("order", poll_id)
    return _mem_orders.get(poll_id)


async def list_by_date(date_str: str) -> List[Dict[str, Any]]:
    """Read-only helper for the Mini App's calendar detail sheet."""
    if is_configured():
        return await repo.filter_rows(
            "order",
            lambda r: str(r.get("order_date", "")).startswith(date_str),
        )
    return [
        row for row in _mem_orders.values()
        if str(row.get("order_date", "")).startswith(date_str)
    ]


async def list_in_range(
    date_from: Optional[str] = None, date_to: Optional[str] = None
) -> List[Dict[str, Any]]:
    """All order rows whose `order_date` falls in [date_from, date_to].

    Bounds are inclusive YYYY-MM-DD strings (lexical compare works because
    order_date is always zero-padded ISO). Either bound may be None.
    Powers the Mini App calendar's week view.
    """
    def in_range(row: Dict[str, Any]) -> bool:
        d = str(row.get("order_date", ""))[:10]
        if not d:
            return False
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    if is_configured():
        return await repo.filter_rows("order", in_range)
    return [row for row in _mem_orders.values() if in_range(row)]
