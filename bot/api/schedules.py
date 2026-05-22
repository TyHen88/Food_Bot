"""
/api/schedules — list + toggle active state of `schedule` rows.

Schedule creation/editing is intentionally out of scope here (multi-field
form, validation rules, day-of-week parsing). Admins edit the spreadsheet
directly for now; Mini App just toggles existing rows on/off.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..sheets import events, repo
from ..sheets.client import is_configured
from .auth import require_admin, require_member

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleToggle(BaseModel):
    active: bool


@router.get("")
async def list_schedules(_: dict = Depends(require_member)) -> List[Dict[str, Any]]:
    # Read access for any verified user; mutation stays admin-only.
    if not is_configured():
        return []
    return await repo.list_all("schedule")


@router.post("/{schedule_id}/toggle")
async def toggle_schedule(
    schedule_id: str,
    body: ScheduleToggle,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Schedule management requires Google Sheets to be configured.",
        )

    flag = "TRUE" if body.active else "FALSE"
    updated = await repo.update("schedule", schedule_id, {"is_active": flag})
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    # Rebuild APScheduler jobs from the new state.
    application = request.app.state.application
    from ..scheduler import reload_schedules
    await reload_schedules(application)

    user_id = (auth.get("user") or {}).get("id")
    await events.emit(
        "SCHEDULE_UPDATED",
        entity_type="schedule", entity_id=schedule_id,
        user_id=user_id,
        payload={"action": "enable" if body.active else "disable"},
    )
    return updated
