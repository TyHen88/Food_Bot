"""
/api/members — read-only list of users for the Mini App's Members page.

Each row exposes the minimum the UI needs:
    { user_id, name, phone, status, username, last_active_at }

`status` is derived from votes: "Active" if the user voted in the last
ACTIVITY_WINDOW_DAYS, otherwise "Inactive". Newly inserted rows with no
votes yet fall back to comparing against `last_active_at`.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_admin

router = APIRouter(prefix="/members", tags=["members"])

ACTIVITY_WINDOW_DAYS = 30


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        # datetime.fromisoformat accepts "+07:00"-style offsets on 3.11+.
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


@router.get("")
async def list_members(_: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    if not is_configured():
        return []

    users = await repo.list_all("user")
    votes = await repo.list_all("vote")

    # Map user_id → latest vote.updated_at (timezone-aware where possible).
    latest_vote: Dict[str, datetime] = {}
    for v in votes:
        uid = str(v.get("user_id", "")).strip()
        if not uid:
            continue
        ts = _parse_iso(v.get("updated_at"))
        if not ts:
            continue
        if uid not in latest_vote or ts > latest_vote[uid]:
            latest_vote[uid] = ts

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ACTIVITY_WINDOW_DAYS)

    out: List[Dict[str, Any]] = []
    for u in users:
        uid = str(u.get("user_id", "")).strip()
        if not uid:
            continue

        # Pick the freshest timestamp available for this user.
        candidates = [
            latest_vote.get(uid),
            _parse_iso(u.get("last_active_at")),
            _parse_iso(u.get("created_at")),
        ]
        latest = max((t for t in candidates if t is not None), default=None)

        # Normalise both sides to aware UTC for the comparison.
        if latest is not None and latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        is_active = latest is not None and latest >= cutoff

        full_name = (u.get("full_name") or "").strip()
        username = (u.get("username") or "").strip()
        out.append({
            "user_id": uid,
            "name": full_name or username or f"User{uid}",
            "username": username,
            "phone": (u.get("phone_number") or "").strip(),
            "status": "Active" if is_active else "Inactive",
            "last_active_at": latest.isoformat() if latest else "",
        })

    # Active first, then by name.
    out.sort(key=lambda r: (r["status"] != "Active", r["name"].lower()))
    return out
