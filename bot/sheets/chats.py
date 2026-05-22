"""
Subscribed-chat operations backed by the `chat` tab.

Replaces the old in-memory set + data/scheduled_chats.json file.
Callers should use these helpers rather than touching the repo directly,
so the "subscription" abstraction stays in one place.
"""

import logging
from typing import List, Optional

from . import repo

logger = logging.getLogger(__name__)


async def subscribe(
    chat_id: int,
    *,
    title: str = "",
    chat_type: str = "",
    subscribed_by: Optional[int] = None,
) -> None:
    """Subscribe a chat (create or reactivate)."""
    existing = await repo.find_by_pk("chat", chat_id)
    fields = {
        "is_subscribed": "TRUE",
        "title": title or (existing.get("title", "") if existing else ""),
        "type": chat_type or (existing.get("type", "") if existing else ""),
        "subscribed_at": repo.now_iso(),
        "subscribed_by": subscribed_by or "",
    }
    if existing:
        await repo.update("chat", chat_id, fields)
    else:
        await repo.create("chat", {"chat_id": chat_id, **fields})
    logger.info(f"Chat {chat_id} subscribed")


async def unsubscribe(chat_id: int) -> bool:
    """Mark a chat unsubscribed. Returns True if it existed."""
    existing = await repo.find_by_pk("chat", chat_id)
    if not existing:
        return False
    await repo.update("chat", chat_id, {"is_subscribed": "FALSE"})
    logger.info(f"Chat {chat_id} unsubscribed")
    return True


async def list_subscribed() -> List[int]:
    """Return chat_ids where is_subscribed is truthy."""
    rows = await repo.filter_rows(
        "chat",
        lambda r: str(r.get("is_subscribed", "")).upper() == "TRUE",
    )
    out: List[int] = []
    for row in rows:
        try:
            out.append(int(row["chat_id"]))
        except (KeyError, ValueError):
            logger.warning(f"Skipping chat row with bad chat_id: {row}")
    return out
