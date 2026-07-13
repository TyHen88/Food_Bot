"""
/api/me — tiny "who am I?" endpoint for the Mini App.

The frontend calls it once on page load so it can hide admin-only UI
(Settings tab, schedule toggles, etc.) for members.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .auth import require_member

router = APIRouter(prefix="/me", tags=["me"])


@router.get("")
async def whoami(auth: dict = Depends(require_member)) -> Dict[str, Any]:
    user = auth.get("user") or {}
    return {
        "user_id": user.get("id"),
        "first_name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "is_admin": bool(auth.get("is_admin")),
    }
