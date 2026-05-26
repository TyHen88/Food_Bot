"""
/api/schedules — full CRUD over `schedule` rows for the Mini App.

List is readable by any verified user; create/edit/delete/toggle are
admin-only. Every mutation rebuilds the APScheduler jobs via
``reload_schedules`` so changes take effect without a restart.
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, field_validator

from ..sheets import events, repo
from ..sheets.client import is_configured
from .auth import require_admin, require_member


def _targets_chat(row: Dict[str, Any], chat_id: str) -> bool:
    """True if a schedule row fires for `chat_id` — i.e. its target_chat_ids
    is "ALL"/empty (broadcast) or lists this chat id."""
    raw = str(row.get("target_chat_ids", "ALL")).strip()
    if not raw or raw.upper() == "ALL":
        return True
    return chat_id in [p.strip() for p in raw.split(",")]

router = APIRouter(prefix="/schedules", tags=["schedules"])

VALID_ACTIONS = {"TEXT", "QR_PHOTO", "POLL"}
VALID_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class ScheduleToggle(BaseModel):
    active: bool


class ScheduleBody(BaseModel):
    """Create/update payload. All fields optional on update (PATCH-style)."""
    name: Optional[str] = None
    action_type: Optional[str] = None
    payload: Optional[str] = None
    days_of_week: Optional[str] = None
    time_of_day: Optional[str] = None
    target_chat_ids: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("action_type")
    @classmethod
    def _check_action(cls, v):
        if v is not None and v.upper() not in VALID_ACTIONS:
            raise ValueError(f"action_type must be one of {sorted(VALID_ACTIONS)}")
        return v.upper() if v else v

    @field_validator("time_of_day")
    @classmethod
    def _check_time(cls, v):
        if v is not None and not _TIME_RE.match(v.strip()):
            raise ValueError("time_of_day must be HH:MM (24h)")
        return v.strip() if v else v

    @field_validator("days_of_week")
    @classmethod
    def _check_days(cls, v):
        if v is None:
            return v
        codes = [c.strip().upper() for c in v.split(",") if c.strip()]
        bad = [c for c in codes if c not in VALID_DAYS]
        if bad:
            raise ValueError(f"Unknown day code(s): {bad}")
        return ",".join(codes)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "schedule"


async def _reload(request: Request) -> None:
    application = request.app.state.application
    from ..scheduler import reload_schedules
    await reload_schedules(application)


def _require_configured() -> None:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Schedule management requires Google Sheets to be configured.",
        )


@router.get("")
async def list_schedules(
    chat_id: Optional[str] = Query(None, description="Restrict to schedules that fire for this chat."),
    _: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    # Read access for any verified user; mutation stays admin-only.
    if not is_configured():
        return []
    rows = await repo.list_all("schedule")
    if chat_id:
        wanted = str(chat_id).strip()
        rows = [r for r in rows if _targets_chat(r, wanted)]
    return rows


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleBody,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    _require_configured()
    if not body.action_type or not body.time_of_day:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action_type and time_of_day are required.",
        )

    base = _slugify(body.name or body.action_type)
    sid = base
    # Ensure a unique schedule_id.
    n = 1
    while await repo.find_by_pk("schedule", sid) is not None:
        n += 1
        sid = f"{base}_{n}"

    row = {
        "schedule_id": sid,
        "name": body.name or sid,
        "action_type": body.action_type,
        "payload": body.payload or "",
        "days_of_week": body.days_of_week or "",
        "time_of_day": body.time_of_day,
        "target_chat_ids": body.target_chat_ids or "ALL",
        "is_active": "TRUE" if (body.is_active is None or body.is_active) else "FALSE",
        "created_at": repo.now_iso(),
    }
    await repo.create("schedule", row)
    await _reload(request)

    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=sid,
        user_id=user_id, payload={"action": "create"},
    )
    return row


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleBody,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    _require_configured()

    fields: Dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.action_type is not None:
        fields["action_type"] = body.action_type
    if body.payload is not None:
        fields["payload"] = body.payload
    if body.days_of_week is not None:
        fields["days_of_week"] = body.days_of_week
    if body.time_of_day is not None:
        fields["time_of_day"] = body.time_of_day
    if body.target_chat_ids is not None:
        fields["target_chat_ids"] = body.target_chat_ids
    if body.is_active is not None:
        fields["is_active"] = "TRUE" if body.is_active else "FALSE"

    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    updated = await repo.update("schedule", schedule_id, fields)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    await _reload(request)
    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=schedule_id,
        user_id=user_id, payload={"action": "update"},
    )
    return updated


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    auth: dict = Depends(require_admin),
) -> None:
    _require_configured()
    ok = await repo.hard_delete("schedule", schedule_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    await _reload(request)
    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=schedule_id,
        user_id=user_id, payload={"action": "delete"},
    )


@router.post("/{schedule_id}/toggle")
async def toggle_schedule(
    schedule_id: str,
    body: ScheduleToggle,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    _require_configured()

    flag = "TRUE" if body.active else "FALSE"
    updated = await repo.update("schedule", schedule_id, {"is_active": flag})
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    await _reload(request)
    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=schedule_id,
        user_id=user_id,
        payload={"action": "enable" if body.active else "disable"},
    )
    return updated
