"""
Per-chat setting overrides, backed by the `chat_setting` tab.

A chat (group) can override a global `setting` key — currently used for
ORDER_SUMMARY_STYLE so each group picks its own order-summary template.
Reads fall back to the global `setting` value (then its seed default) when
the chat has no override, so behaviour is unchanged for chats that never
set one.

Row PK `id` is "<chat_id>:<key>".
"""

import logging
from typing import Any, Optional

from . import repo, settings
from .client import is_configured

logger = logging.getLogger(__name__)


def _row_id(chat_id: Any, key: str) -> str:
    return f"{str(chat_id).strip()}:{key}"


async def get(chat_id: Optional[str], key: str, default: Optional[str] = None) -> str:
    """Per-chat override for `key` if present, else the global setting value."""
    cid = str(chat_id).strip() if chat_id is not None else ""
    if cid and is_configured():
        try:
            row = await repo.find_by_pk("chat_setting", _row_id(cid, key))
            if row and str(row.get("value", "")) != "":
                return str(row["value"])
        except Exception as e:
            logger.warning(f"chat_setting lookup failed for {cid}:{key}: {e}")
    return await settings.get(key, default)


async def set(chat_id: str, key: str, value: str, user_id: Optional[int] = None) -> None:
    """Upsert a per-chat override (blocking write so it survives a restart)."""
    if not is_configured():
        return
    await repo.upsert_blocking("chat_setting", {
        "id": _row_id(chat_id, key),
        "chat_id": str(chat_id).strip(),
        "key": key,
        "value": value,
        "updated_at": repo.now_iso(),
        "updated_by": user_id or "",
    })
