"""
/api/members — read-only list of users for the Mini App's Members page.

Each row exposes the minimum the UI needs:
    { user_id, name, phone, role, status, username, last_active_at }

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
from .auth import caller_chat_id, caller_user_id, require_member

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


async def user_chats(user_id: str) -> Set[str]:
    """All chat_ids the caller takes part in: chats where they voted on a
    poll, or appear in an order (as payer or inside the item JSON), plus the
    chat recorded on their `user` row. Used to scope list views to the
    caller's own groups when the Mini App can't supply an explicit chat_id,
    so a user never sees data from a group they don't belong to."""
    uid = str(user_id).strip()
    if not uid:
        return set()

    poll_chat = {
        str(p.get("poll_id", "")).strip(): str(p.get("chat_id", "")).strip()
        for p in await repo.list_all("poll")
    }
    chats: Set[str] = set()

    for v in await repo.list_all("vote"):
        if str(v.get("user_id", "")).strip() == uid:
            c = poll_chat.get(str(v.get("poll_id", "")).strip())
            if c:
                chats.add(c)

    for o in await repo.list_all("order"):
        c = str(o.get("chat_id", "")).strip()
        if not c:
            continue
        if str(o.get("user_id", "")).strip() == uid:
            chats.add(c)
            continue
        try:
            items = json.loads(o.get("item") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        items = items if isinstance(items, list) else []
        if any(str(it.get("user_id", "")).strip() == uid for it in items):
            chats.add(c)

    row = await repo.find_by_pk("user", uid)
    if row:
        c = str(row.get("chat_id", "")).strip()
        if c:
            chats.add(c)

    return chats


@router.get("")
async def list_members(
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's participants."),
    auth: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    if not is_configured():
        return []

    users = await repo.list_all("user")
    votes = await repo.list_all("vote")

    # Scope to the launch chat: explicit ?chat_id, else the chat baked into
    # the signed initData (attachment-menu `chat` or startapp start_param).
    auth_chat = caller_chat_id(auth)
    if not chat_id:
        chat_id = auth_chat
    # Members may only request chats they belong to (the signed launch chat
    # always qualifies); otherwise fall back to their own chats below.
    if chat_id and not auth.get("is_admin"):
        wanted = str(chat_id).strip()
        if wanted != auth_chat and wanted not in await user_chats(caller_user_id(auth)):
            chat_id = None

    # `allowed` is the set of user_ids the caller is permitted to see. It is
    # never None — we always scope, so a user can't see members of a group
    # they don't belong to.
    allowed: Set[str] = set()
    if chat_id:
        # Explicit chat (the Mini App launched from a group passes it via
        # ?startapp=<chat_id>): union of users tagged with this chat_id and
        # those derived from its votes/orders (legacy rows have blank chat_id).
        wanted = str(chat_id).strip()
        allowed = {
            str(u.get("user_id", "")).strip()
            for u in users
            if str(u.get("chat_id", "")).strip() == wanted
        }
        allowed |= await _chat_participants(chat_id)
    else:
        # No explicit chat (e.g. opened from the bot DM) → members of every
        # chat the caller belongs to, never other groups.
        caller_id = caller_user_id(auth)
        for c in await user_chats(caller_id):
            allowed |= {
                str(u.get("user_id", "")).strip()
                for u in users
                if str(u.get("chat_id", "")).strip() == c
            }
            allowed |= await _chat_participants(c)
        if caller_id:
            allowed.add(caller_id)  # always include the caller themselves

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
        if uid not in allowed:
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
        is_admin = str(u.get("role", "")).strip().upper() == "ADMIN"
        out.append({
            "user_id": uid,
            "name": full_name or username or f"User{uid}",
            "username": username,
            "bank_name": (u.get("bank_name") or "").strip(),
            "phone": (u.get("phone_number") or "").strip(),
            "role": "Admin" if is_admin else "Member",
            "status": "Active" if is_active else "Inactive",
            "last_active_at": latest.isoformat() if latest else "",
        })

    # Active first, then by name.
    out.sort(key=lambda r: (r["status"] != "Active", r["name"].lower()))
    return out


from pydantic import BaseModel

class MemberUpdate(BaseModel):
    bank_name: Optional[str] = None
    role: Optional[str] = None
    full_name: Optional[str] = None


@router.put("/{user_id}")
async def update_member(
    user_id: str,
    body: MemberUpdate,
    auth: dict = Depends(require_member),
) -> Dict[str, Any]:
    """Update member's bank_name or profile."""
    caller_id = caller_user_id(auth)
    is_admin = auth.get("is_admin")
    if not is_admin and caller_id != str(user_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Permission denied")

    updates: Dict[str, Any] = {}
    if body.bank_name is not None:
        updates["bank_name"] = body.bank_name.strip()
    if body.full_name is not None:
        updates["full_name"] = body.full_name.strip()
    if body.role is not None and is_admin:
        updates["role"] = body.role.strip().upper()

    if updates and is_configured():
        await repo.update("user", user_id, updates)

    row = await repo.find_by_pk("user", user_id) if is_configured() else {}
    return {
        "user_id": user_id,
        "username": str(row.get("username", "")),
        "name": str(row.get("full_name", "")),
        "bank_name": str(row.get("bank_name", "")),
        "role": str(row.get("role", "")),
    }

