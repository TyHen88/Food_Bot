"""
/api/members — read-only list of users for the Mini App's Members page.

Each row exposes the minimum the UI needs:
    { user_id, name, phone, status, username, last_active_at }

`status` is derived from votes: "Active" if the user voted in the last
ACTIVITY_WINDOW_DAYS, otherwise "Inactive". Newly inserted rows with no
votes yet fall back to comparing against `last_active_at`.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, Query

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_member

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


async def _chat_participants(chat_id: str) -> Set[str]:
    """user_ids who took part in `chat_id`: voted on its polls or appear
    in its orders (as a voter inside item JSON, or as the order's payer)."""
    wanted = str(chat_id).strip()
    poll_ids = {
        str(p.get("poll_id", "")).strip()
        for p in await repo.list_all("poll")
        if str(p.get("chat_id", "")).strip() == wanted
    }
    participants: Set[str] = set()

    for v in await repo.list_all("vote"):
        if str(v.get("poll_id", "")).strip() in poll_ids:
            uid = str(v.get("user_id", "")).strip()
            if uid:
                participants.add(uid)

    for o in await repo.list_all("order"):
        if str(o.get("chat_id", "")).strip() != wanted:
            continue
        payer = str(o.get("user_id", "")).strip()
        if payer:
            participants.add(payer)
        try:
            items = json.loads(o.get("item") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        for it in items if isinstance(items, list) else []:
            uid = str(it.get("user_id", "")).strip()
            if uid:
                participants.add(uid)

    return participants


@router.get("")
async def list_members(
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's participants."),
    _: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    if not is_configured():
        return []

    users = await repo.list_all("user")
    votes = await repo.list_all("vote")

    allowed: Optional[Set[str]] = None
    if chat_id:
        wanted = str(chat_id).strip()
        # Union of users tagged with this chat_id directly and those derived
        # from votes/orders, so legacy rows (blank chat_id) still resolve.
        allowed = {
            str(u.get("user_id", "")).strip()
            for u in users
            if str(u.get("chat_id", "")).strip() == wanted
        }
        allowed |= await _chat_participants(chat_id)

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
        if allowed is not None and uid not in allowed:
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
