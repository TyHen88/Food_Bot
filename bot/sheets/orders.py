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

First clicker wins: the first person to tap Order on a poll is recorded as
the owner (user_id/username) and is never replaced. Re-tapping by anyone
later keeps that original clicker and only refreshes the `item` snapshot so
late votes still get captured. The Mini App's "Paid by" therefore reflects
whoever ordered first.

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

    First clicker wins: if an order row already exists for this poll, the
    original clicker (user_id/username) and created_at are preserved and
    only the `item` snapshot is refreshed. The returned row's user_id
    therefore identifies whoever ordered first — callers can compare it to
    the current clicker to tell whether this tap actually created the order.

    Returns the saved row (or None if there were no votes to snapshot).
    """
    selections_map = await votes.get_user_selections_map(poll_id)
    if not selections_map:
        return None

    item_json = _build_item_json(selections_map)
    if not item_json or item_json == "[]":
        return None

    # First clicker wins: keep the original owner + created_at if a row
    # already exists; only a brand-new order records the current clicker.
    existing = await get_by_poll(poll_id)
    if existing:
        owner_user_id = existing.get("user_id") or clicker_user_id or ""
        owner_username = existing.get("username") or clicker_username or ""
        created_at = existing.get("created_at") or repo.now_iso()
        order_date = existing.get("order_date") or _today_date()
    else:
        owner_user_id = clicker_user_id or ""
        owner_username = clicker_username or ""
        created_at = repo.now_iso()
        order_date = _today_date()

    row = {
        "order_id": poll_id,
        "poll_id": poll_id,
        "chat_id": chat_id,
        "user_id": owner_user_id,
        "username": owner_username,
        "item": item_json,
        "order_date": order_date,
        "created_at": created_at,
    }

    if is_configured():
        logger.info(
            f"snapshot_from_poll: writing order row poll_id={poll_id} "
            f"owner={owner_user_id} ({owner_username!r}) "
            f"clicker={clicker_user_id} ({clicker_username!r}) "
            f"existing={bool(existing)} item_count={len(json.loads(item_json))}"
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


def _normalize_items(items: Any) -> List[Dict[str, Any]]:
    """Coerce a caller-supplied items list into the stored item shape.

    Drops blank entries (no dish and no name), clamps qty to >= 1, and keeps
    any per-voter user_id so an edited order still maps back to its members.
    """
    out: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        item_name = str(it.get("item_name", "")).strip()
        name = str(it.get("name", "")).strip()
        if not item_name and not name:
            continue
        try:
            qty = int(it.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        uid = it.get("user_id", "") or ""
        out.append({
            "user_id": uid,
            "name": name or (f"User{uid}" if uid else "Guest"),
            "item_name": item_name,
            "qty": max(1, qty),
        })
    return out


async def update_items(order_id: str, items: Any) -> Optional[Dict[str, Any]]:
    """Replace an existing order's `item` list (admin edit from the Mini App).

    Only the item snapshot changes; the owner/payer (user_id/username),
    created_at and order_date are left untouched. Returns the updated row, or
    None if no order exists for `order_id`.
    """
    item_json = json.dumps(_normalize_items(items), ensure_ascii=False)

    if is_configured():
        existing = await repo.find_by_pk("order", order_id)
        if not existing:
            return None
        return await repo.update("order", order_id, {"item": item_json})

    existing = _mem_orders.get(order_id)
    if not existing:
        return None
    existing["item"] = item_json
    return existing


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
