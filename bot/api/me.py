"""
/api/me — tiny "who am I?" endpoint for the Mini App.

The frontend calls it once on page load so it can hide admin-only UI
(Settings tab, schedule toggles, etc.) for members.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_member

router = APIRouter(prefix="/me", tags=["me"])


@router.get("")
async def whoami(auth: dict = Depends(require_member)) -> Dict[str, Any]:
    user = auth.get("user") or {}

    # Enrich from the caller's `user` tab row (cached read): chat_id is the
    # most recent chat they interacted in — the Invoices "Me" card counts by it.
    row: Dict[str, Any] = {}
    if user.get("id") is not None and is_configured():
        row = await repo.find_by_pk("user", user.get("id")) or {}

    return {
        "user_id": user.get("id"),
        "first_name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "is_admin": bool(auth.get("is_admin")),
        "chat_id": str(row.get("chat_id", "") or "").strip(),
        "full_name": str(row.get("full_name", "") or ""),
        "role": str(row.get("role", "") or ""),
        "language": str(row.get("language", "") or ""),
    }
