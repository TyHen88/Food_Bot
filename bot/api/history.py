"""
/api/history — paginated read of the audit log with chat and user resolution.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/history", tags=["history"])


async def _chat_titles() -> Dict[str, str]:
    """{chat_id (str): title}."""
    if not is_configured():
        return {}
    rows = await repo.list_all("chat")
    out: Dict[str, str] = {}
    for r in rows:
        cid = str(r.get("chat_id", "")).strip()
        if cid:
            out[cid] = str(r.get("title", "")).strip()
    return out


async def _user_names() -> Dict[str, str]:
    """{user_id (str): display name}."""
    if not is_configured():
        return {}
    rows = await repo.list_all("user")
    out: Dict[str, str] = {}
    for r in rows:
        uid = str(r.get("user_id", "")).strip()
        if not uid:
            continue
        out[uid] = (
            str(r.get("username", "")).strip()
            or str(r.get("name", "")).strip()
            or str(r.get("full_name", "")).strip()
        )
    return out


@router.get("")
async def list_history(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: Optional[str] = Query(default=None),
    _: dict = Depends(require_member),
) -> Dict[str, Any]:
    if not is_configured():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    rows = await repo.list_all("history")
    if event_type and event_type != "ALL":
        rows = [r for r in rows if str(r.get("event_type", "")).upper() == event_type.upper()]
    
    # Newest first by created_at (ISO 8601 is sortable as string).
    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)

    total = len(rows)
    page = rows[offset : offset + limit]

    chat_titles, user_names = await asyncio.gather(
        _chat_titles(),
        _user_names(),
    )

    shaped_items = []
    for r in page:
        cid = str(r.get("chat_id", "")).strip()
        uid = str(r.get("user_id", "")).strip()
        
        payload = r.get("payload")
        if isinstance(payload, str) and payload.strip():
            try:
                payload = json.loads(payload)
            except Exception:
                pass

        # If chat_id or user_id is in payload, fallback check
        if not cid and isinstance(payload, dict):
            cid = str(payload.get("chat_id", "")).strip()
        if not uid and isinstance(payload, dict):
            uid = str(payload.get("user_id", "")).strip()

        chat_title = chat_titles.get(cid, "")
        user_name = user_names.get(uid, "")

        item = dict(r)
        item["payload"] = payload
        item["chat_id"] = cid
        item["chat_title"] = chat_title
        item["user_id"] = uid
        item["user_name"] = user_name
        shaped_items.append(item)

    return {"items": shaped_items, "total": total, "limit": limit, "offset": offset}
