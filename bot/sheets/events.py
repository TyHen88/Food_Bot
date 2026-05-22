"""
Append-only audit log writer for the `history` tab.

Every significant action (subscribe, vote, setting update, ...) should
go through emit() so we have a single audit stream. Failures are logged
but never raised — the caller's primary action must not be blocked by
audit-log issues.
"""

import json
import logging
from typing import Any, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)


async def emit(
    event_type: str,
    *,
    entity_type: str = "",
    entity_id: Any = "",
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> None:
    """Append a row to the `history` tab. No-op if Sheets not configured."""
    if not is_configured():
        return
    try:
        await repo.create("history", {
            "event_id": repo.new_uuid(),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": "" if entity_id in (None, "") else str(entity_id),
            "user_id": user_id or "",
            "chat_id": chat_id or "",
            "payload": json.dumps(payload, ensure_ascii=False) if payload else "",
            "created_at": repo.now_iso(),
        })
    except Exception as e:
        logger.error(f"Failed to emit history event '{event_type}': {e}")
