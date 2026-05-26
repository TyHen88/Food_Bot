"""
Poll storage backed by the `poll` tab — with an in-memory fallback for
local dev (no Sheets configured).

Returned poll dicts always have a normalised shape so callers don't have
to care about the backend:

    {
        "poll_id":           str,
        "chat_id":           int,
        "message_id":        int,
        "button_message_id": int | None,
        "options":           list[str],
        "question":          str,
        "status":            "OPEN" | "CLOSED",
    }
"""

import json
import logging
from typing import Any, Dict, List, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)

# Fallback store (only used when Sheets isn't configured).
_mem_polls: Dict[str, Dict[str, Any]] = {}


def _normalise(row: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw poll row (from Sheets or memory) into the public shape."""
    options = row.get("options", [])
    if isinstance(options, str) and options:
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            options = []
    return {
        "poll_id": str(row.get("poll_id", "")),
        "chat_id": int(row["chat_id"]) if row.get("chat_id") not in (None, "") else 0,
        "message_id": int(row["message_id"]) if row.get("message_id") not in (None, "") else 0,
        "button_message_id": (
            int(row["button_message_id"])
            if row.get("button_message_id") not in (None, "") else None
        ),
        "options": list(options) if options else [],
        "question": str(row.get("question", "")),
        "status": str(row.get("status", "OPEN")).upper() or "OPEN",
    }


async def create(
    *,
    poll_id: str,
    chat_id: int,
    message_id: int,
    options: List[str],
    question: str,
    created_by: Optional[int] = None,
) -> None:
    """Insert a new OPEN poll."""
    row = {
        "poll_id": poll_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "button_message_id": "",
        "question": question,
        "options": json.dumps(options, ensure_ascii=False),
        "status": "OPEN",
        "created_at": repo.now_iso(),
        "closed_at": "",
        "created_by": created_by or "",
    }
    if is_configured():
        # Blocking write: the poll row must survive a restart, otherwise the
        # Order button can't find the poll and votes can't be aggregated.
        # upsert_blocking appends a fresh poll_id just like create(), but
        # awaits the Sheets write instead of firing it in the background.
        await repo.upsert_blocking("poll", row)
    else:
        _mem_polls[poll_id] = row


async def get(poll_id: str) -> Optional[Dict[str, Any]]:
    if is_configured():
        row = await repo.find_by_pk("poll", poll_id)
        return _normalise(row) if row else None
    row = _mem_polls.get(poll_id)
    return _normalise(row) if row else None


async def set_button_message_id(poll_id: str, button_message_id: int) -> None:
    if is_configured():
        await repo.update("poll", poll_id, {"button_message_id": button_message_id})
    elif poll_id in _mem_polls:
        _mem_polls[poll_id]["button_message_id"] = button_message_id


async def close(poll_id: str) -> None:
    if is_configured():
        await repo.update("poll", poll_id, {
            "status": "CLOSED",
            "closed_at": repo.now_iso(),
        })
    elif poll_id in _mem_polls:
        _mem_polls[poll_id]["status"] = "CLOSED"
        _mem_polls[poll_id]["closed_at"] = repo.now_iso()


async def list_open() -> List[Dict[str, Any]]:
    """All OPEN polls (used by the cutoff snapshot job)."""
    if is_configured():
        rows = await repo.filter_rows(
            "poll",
            lambda r: str(r.get("status", "")).upper() == "OPEN",
        )
    else:
        rows = [r for r in _mem_polls.values() if r.get("status") == "OPEN"]
    return [_normalise(r) for r in rows]
