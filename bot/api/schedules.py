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
from .auth import caller_chat_id, require_admin, require_member


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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ScheduleBody(BaseModel):
    """Create/update payload. All fields optional on update (PATCH-style)."""
    name: Optional[str] = None
    action_type: Optional[str] = None
    payload: Optional[str] = None
    message_text: Optional[str] = None   # text to send
    image: Optional[str] = None          # Telegram file_id (uploaded) or assets/ filename
    image_name: Optional[str] = None     # original filename, for display when editing
    run_date: Optional[str] = None       # YYYY-MM-DD => one-time; "" => recurring weekly
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

    @field_validator("run_date")
    @classmethod
    def _check_date(cls, v):
        if v is None:
            return v
        v = v.strip()
        if v == "":
            return v
        if not _DATE_RE.match(v):
            raise ValueError("run_date must be YYYY-MM-DD")
        try:
            from datetime import date
            y, m, d = (int(x) for x in v.split("-"))
            date(y, m, d)
        except ValueError:
            raise ValueError("run_date is not a valid calendar date")
        return v

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
    auth: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    # Read access for any verified user; mutation stays admin-only.
    if not is_configured():
        return []

    # Scope to the launch chat: explicit ?chat_id, else the chat baked into
    # the signed initData (attachment-menu `chat` or startapp start_param).
    if not chat_id:
        chat_id = caller_chat_id(auth)

    rows = await repo.list_all("schedule")
    if chat_id:
        wanted = str(chat_id).strip()
        rows = [r for r in rows if _targets_chat(r, wanted)]
    return rows


class ImageUpload(BaseModel):
    data_base64: str          # base64 of the image bytes (data: URL prefix tolerated)
    filename: Optional[str] = None


@router.post("/upload-image")
async def upload_image(
    body: ImageUpload,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Accept a base64 image, register it with Telegram, and return a reusable
    file_id to store on the schedule. We capture the file_id by sending the
    photo to the uploader's own chat with the bot (then deleting it) — file_ids
    are reusable across chats, so the scheduled job can later send it anywhere.
    Requires the admin to have started a DM with the bot."""
    import base64
    import binascii
    import io

    raw = body.data_base64 or ""
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid base64 image data.")
    if not blob:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Empty image.")
    if len(blob) > 8 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Image too large (max 8MB).")

    user_id = (auth.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Cannot determine the uploader's chat.")

    bot = request.app.state.application.bot
    bio = io.BytesIO(blob)
    bio.name = body.filename or "upload.jpg"
    try:
        msg = await bot.send_photo(chat_id=int(user_id), photo=bio)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not register image — open a DM with the bot (/start) first. ({e})",
        )
    file_id = msg.photo[-1].file_id if msg.photo else ""
    # Tidy up the capture message in the uploader's DM (best effort).
    try:
        await bot.delete_message(chat_id=int(user_id), message_id=msg.message_id)
    except Exception:
        pass
    return {"file_id": file_id}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleBody,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    _require_configured()
    if not body.time_of_day:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="time_of_day is required.",
        )
    message_text = (body.message_text or "").strip()
    image = (body.image or "").strip()
    if not message_text and not image:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a message text and/or an image.",
        )
    run_date = (body.run_date or "").strip()
    if not run_date and not (body.days_of_week or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pick a date (one-time) or at least one weekday (recurring).",
        )
    # Derived for legacy compatibility; the scheduler uses message_text/image.
    action_type = body.action_type or ("QR_PHOTO" if image else "TEXT")

    base = _slugify(body.name or action_type)
    sid = base
    # Ensure a unique schedule_id.
    n = 1
    while await repo.find_by_pk("schedule", sid) is not None:
        n += 1
        sid = f"{base}_{n}"

    row = {
        "schedule_id": sid,
        "name": body.name or sid,
        "action_type": action_type,
        "payload": body.payload or "",
        "message_text": message_text,
        "image": image,
        "image_name": (body.image_name or "").strip(),
        "run_date": run_date,
        "days_of_week": "" if run_date else (body.days_of_week or ""),
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
@router.patch("/{schedule_id}")
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
    if body.message_text is not None:
        fields["message_text"] = body.message_text
    if body.image is not None:
        fields["image"] = body.image
        # Keep legacy action_type roughly in sync for display.
        fields.setdefault("action_type", "QR_PHOTO" if body.image.strip() else "TEXT")
    if body.image_name is not None:
        fields["image_name"] = body.image_name.strip()
    if body.run_date is not None:
        fields["run_date"] = body.run_date.strip()
        # A one-time date and weekly recurrence are mutually exclusive.
        if body.run_date.strip():
            fields["days_of_week"] = ""
    if body.days_of_week is not None and "days_of_week" not in fields:
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
