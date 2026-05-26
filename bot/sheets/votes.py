"""
Vote storage backed by the `vote` tab — with an in-memory fallback for
local dev (no Sheets configured).

We store one row per (poll, user) and update it in place when the user
changes their vote. The order-aggregate ({item: count}) is derived on
demand rather than stored separately — simpler and impossible to drift.
"""

import json
import logging
from collections import Counter
from typing import Any, Dict, List

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)

# Fallback store: {poll_id: {user_id: {"name": str, "selections": list[str]}}}
_mem_votes: Dict[str, Dict[int, Dict[str, Any]]] = {}


def _vote_id(poll_id: str, user_id: int) -> str:
    return f"{poll_id}_{user_id}"


def _parse_selections(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except json.JSONDecodeError:
        return []


async def record(
    *,
    poll_id: str,
    user_id: int,
    user_name: str,
    selected_options: List[str],
) -> None:
    """Upsert the user's selection for this poll.

    Uses a *blocking* write so the vote reaches the `vote` tab before we
    return. Votes are the source of truth for the Order summary; a
    fire-and-forget write can be lost if the process restarts before the
    background flush, which surfaced as "No orders yet" after a redeploy.
    """
    if is_configured():
        await repo.upsert_blocking("vote", {
            "vote_id": _vote_id(poll_id, user_id),
            "poll_id": poll_id,
            "user_id": user_id,
            "user_name": user_name,
            "selected_options": json.dumps(selected_options, ensure_ascii=False),
            "updated_at": repo.now_iso(),
        })
        return

    bucket = _mem_votes.setdefault(poll_id, {})
    bucket[user_id] = {
        "name": user_name,
        "selections": list(selected_options),
    }


async def get_user_selections_map(poll_id: str) -> Dict[int, Dict[str, Any]]:
    """
    Return {user_id: {"name": str, "selections": list[str]}} for a poll,
    matching the shape format_order_summary() expects.
    """
    if is_configured():
        rows = await repo.filter_rows(
            "vote",
            lambda r: str(r.get("poll_id", "")) == str(poll_id),
        )
        out: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            try:
                uid = int(row["user_id"])
            except (KeyError, ValueError):
                continue
            out[uid] = {
                "name": row.get("user_name", f"User{uid}"),
                "selections": _parse_selections(row.get("selected_options")),
            }
        return out
    return {uid: dict(v) for uid, v in _mem_votes.get(poll_id, {}).items()}


async def aggregate_orders(poll_id: str) -> Dict[str, int]:
    """{item: count} aggregated across all votes for this poll."""
    selections_map = await get_user_selections_map(poll_id)
    counter: Counter[str] = Counter()
    for entry in selections_map.values():
        counter.update(entry.get("selections", []))
    return dict(counter)
