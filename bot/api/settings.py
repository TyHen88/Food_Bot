"""
/api/settings — Mini App CRUD over the `setting` tab.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..sheets import events, repo
from ..sheets.client import is_configured
from ..sheets.schema import SEED_SETTING
from .auth import require_admin

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingUpdate(BaseModel):
    value: str


@router.get("")
async def list_settings(_: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    if not is_configured():
        # Surface seed defaults so the UI has something to show in local dev.
        return list(SEED_SETTING)
    return await repo.list_all("setting")


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
