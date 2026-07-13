"""
/api/templates — per-chat order-summary template (style).

The Mini App's Templates page reads/writes the order-summary style. When the
app is launched from a group, the style is stored per-chat (chat_setting tab)
so each group picks its own template; otherwise the global ORDER_SUMMARY_STYLE
setting is used. Reads always fall back to the global value, then "1".

GET is open to any verified member; PUT is admin-only.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..sheets import chat_settings, events, repo, settings
from ..sheets.client import is_configured
from .auth import require_admin, require_member

router = APIRouter(prefix="/templates", tags=["templates"])

_STYLE_KEY = "ORDER_SUMMARY_STYLE"


class StyleBody(BaseModel):
    value: str


@router.get("/style")
async def get_style(
    chat_id: Optional[str] = Query(None, description="Resolve the style for this chat."),
    _: dict = Depends(require_member),
) -> dict:
    """Effective order-summary style for `chat_id` (per-chat override or
    global), plus whether a per-chat override exists."""
    effective = await chat_settings.get(chat_id, _STYLE_KEY, "1")
    has_override = False
    if chat_id and is_configured():
        row = await repo.find_by_pk("chat_setting", f"{str(chat_id).strip()}:{_STYLE_KEY}")
        has_override = bool(row and str(row.get("value", "")) != "")
    global_value = await settings.get(_STYLE_KEY, "1")
    return {
        "style": effective,
        "chat_id": str(chat_id).strip() if chat_id else "",
        "scoped": bool(chat_id),
        "has_override": has_override,
        "global_style": global_value,
    }


@router.put("/style")
async def set_style(
    body: StyleBody,
    chat_id: Optional[str] = Query(None, description="Set per-chat style; omit to set the global default."),
    auth: dict = Depends(require_admin),
) -> dict:
    """Set the order-summary style. With chat_id → per-chat override; without
    → the global ORDER_SUMMARY_STYLE."""
    value = str(body.value).strip() or "1"
    user_id = (auth.get("user") or {}).get("id")

    if chat_id:
        cid = str(chat_id).strip()
        await chat_settings.set(cid, _STYLE_KEY, value, user_id=user_id)
        await events.emit(
            "SETTING_UPDATED",
            entity_type="chat_setting", entity_id=f"{cid}:{_STYLE_KEY}",
            chat_id=cid, user_id=user_id,
            payload={"key": _STYLE_KEY, "new_value": value, "scope": "chat"},
        )
        return {"style": value, "chat_id": cid, "scoped": True}

    # Global fallback: write the shared ORDER_SUMMARY_STYLE setting.
    existing = await repo.find_by_pk("setting", _STYLE_KEY) if is_configured() else None
    if is_configured():
        await repo.upsert("setting", {
            "key": _STYLE_KEY,
            "value": value,
            "value_type": (existing or {}).get("value_type", "string"),
            "description": (existing or {}).get("description", ""),
            "updated_at": repo.now_iso(),
            "updated_by": user_id or "",
        })
    await events.emit(
        "SETTING_UPDATED",
        entity_type="setting", entity_id=_STYLE_KEY,
        user_id=user_id,
        payload={"key": _STYLE_KEY, "new_value": value, "scope": "global"},
    )
    return {"style": value, "chat_id": "", "scoped": False}
