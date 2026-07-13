"""
/api/settings — Mini App CRUD over the `setting` tab.
"""

import re
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..sheets import events, repo
from ..sheets.client import is_configured
from ..sheets.schema import SEED_SETTING
from .auth import require_admin

router = APIRouter(prefix="/settings", tags=["settings"])

_KEY_RE = re.compile(r"^[A-Z0-9_]+$")


class SettingUpdate(BaseModel):
    value: str


class SettingCreate(BaseModel):
    key: str
    value: str = ""
    value_type: str = "string"
    description: str = ""


@router.get("")
async def list_settings(_: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    if not is_configured():
        # Surface seed defaults so the UI has something to show in local dev.
        return list(SEED_SETTING)
    return await repo.list_all("setting")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_setting(
    body: SettingCreate,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings storage requires Google Sheets to be configured.",
        )
    key = body.key.strip().upper()
    if not _KEY_RE.match(key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key must be UPPER_SNAKE_CASE (A-Z, 0-9, underscore).",
        )
    if await repo.find_by_pk("setting", key) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Setting '{key}' already exists.",
        )
    user_id = (auth.get("user") or {}).get("id")
    row = {
        "key": key,
        "value": body.value,
        "value_type": body.value_type or "string",
        "description": body.description or "",
        "updated_at": repo.now_iso(),
        "updated_by": user_id or "",
    }
    await repo.create("setting", row)
    await events.emit(
        "SETTING_UPDATED",
        entity_type="setting", entity_id=key,
        user_id=user_id, payload={"action": "create", "new_value": body.value},
    )
    return row


@router.put("/{key}")
async def update_setting(
    key: str,
    body: SettingUpdate,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings storage requires Google Sheets to be configured.",
        )
    existing = await repo.find_by_pk("setting", key)
    user_id = (auth.get("user") or {}).get("id")
    row = {
        "key": key,
        "value": body.value,
        "value_type": (existing or {}).get("value_type", "string"),
        "description": (existing or {}).get("description", ""),
        "updated_at": repo.now_iso(),
        "updated_by": user_id or "",
    }
    await repo.upsert("setting", row)
    await events.emit(
        "SETTING_UPDATED",
        entity_type="setting", entity_id=key,
        user_id=user_id,
        payload={"new_value": body.value, "old_value": (existing or {}).get("value", "")},
    )
    return row


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_setting(
    key: str,
    auth: dict = Depends(require_admin),
) -> None:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings storage requires Google Sheets to be configured.",
        )
    ok = await repo.hard_delete("setting", key)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Setting not found")
    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SETTING_UPDATED",
        entity_type="setting", entity_id=key,
        user_id=user_id, payload={"action": "delete"},
    )
