"""
/api/orders — the Mini App calendar's data source.

Each `order` row is one poll's order snapshot, written when someone taps
the Order button. We annotate it with the chat title and poll question,
expose the per-voter `items`, and record who paid (`paid_by`).

Access model
------------
Any verified Telegram user may call this (``require_member``):
    - Admins see every order, with the full item list.
    - Members see only orders they personally appear in, and the `items`
      are trimmed to that member's own dishes.
"""

import json
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..sheets import orders as sheets_orders
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_user_id, require_admin, require_member
from .members import user_chats

router = APIRouter(prefix="/orders", tags=["orders"])


def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _chat_titles() -> Dict[str, str]:
    """{chat_id (str): title}."""
    if not is_configured():
        return {}
    rows = await repo.list_all("chat")
    out: Dict[str, str] = {}
    for r in rows:
        cid = str(r.get("chat_id", "")).strip()
        if cid:
            out[cid] = str(r.get("title", "")).strip()
    return out


async def _poll_meta() -> Dict[str, Dict[str, str]]:
    """{poll_id (str): {question, status}}."""
    if not is_configured():
        return {}
    rows = await repo.list_all("poll")
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        pid = str(r.get("poll_id", "")).strip()
        if pid:
            out[pid] = {
                "question": str(r.get("question", "")),
                "status": str(r.get("status", "OPEN")).upper(),
            }
    return out


async def _user_names() -> Dict[str, str]:
    """{user_id (str): display name} — username preferred, then full name."""
    if not is_configured():
        return {}
    rows = await repo.list_all("user")
    out: Dict[str, str] = {}
    for r in rows:
        uid = str(r.get("user_id", "")).strip()
        if not uid:
            continue
        out[uid] = (
            str(r.get("username", "")).strip()
            or str(r.get("full_name", "")).strip()
        )
    return out


def _paid_by_display(row: Dict[str, Any], users: Dict[str, str]) -> str:
    """Best display name for the payer: user-tab username, else stored
    username, else empty (the frontend then shows the id as a last resort)."""
    uid = str(row.get("user_id", ""))
    return users.get(uid, "") or str(row.get("username", "")).strip()


def _shape_order(
    row: Dict[str, Any],
    *,
    titles: Dict[str, str],
    polls: Dict[str, Dict[str, str]],
    users: Dict[str, str],
) -> Dict[str, Any]:
    """Map a raw `order` row to the calendar's event shape.

    Shows the whole group order (every voter's items) — the calendar is a
    shared view, scoped to a chat via the `chat_id` query param.
    """
    items = _parse_items(row.get("item"))

    cid = str(row.get("chat_id", ""))
    pid = str(row.get("poll_id", ""))
    meta = polls.get(pid, {})
    people = {str(it.get("user_id", "")) or it.get("name", "") for it in items}

    return {
        "order_id": str(row.get("order_id", "")),
        "poll_id": pid,
        "chat_id": cid,
        "chat_title": titles.get(cid, ""),
        "question": meta.get("question", ""),
        "status": meta.get("status", ""),
        "order_date": str(row.get("order_date", "")),
        "created_at": str(row.get("created_at", "")),
        "paid_by": {
            "user_id": str(row.get("user_id", "")),
            "username": _paid_by_display(row, users),
        },
        "items": items,
        "item_count": sum(int(it.get("qty", 1) or 1) for it in items),
        "person_count": len(people),
    }


@router.get("")
async def list_orders(
    date_from: Optional[str] = Query(None, alias="from",
                                     description="Inclusive YYYY-MM-DD lower bound on order_date."),
    date_to: Optional[str] = Query(None, alias="to",
                                   description="Inclusive YYYY-MM-DD upper bound on order_date."),
    date: Optional[str] = Query(None, description="Single-day shorthand (YYYY-MM-DD)."),
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's orders."),
    auth: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    """Orders in a date range, annotated for the calendar. Role-aware.

    When `chat_id` is given (the Mini App launched from a group passes it via
    ?startapp=<chat_id>), only that chat's orders are returned. If it's absent
    (e.g. opened from the bot DM), we scope to every chat the caller takes
    part in — never other groups' orders.
    """
    if date and not (date_from or date_to):
        date_from = date_to = date

    rows = await sheets_orders.list_in_range(date_from, date_to)
    if chat_id:
        wanted = str(chat_id).strip()
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() == wanted]
    else:
        my_chats = await user_chats(caller_user_id(auth))
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() in my_chats]
    if not rows:
        return []

    titles = await _chat_titles()
    polls = await _poll_meta()
    users = await _user_names()

    out = [
        _shape_order(row, titles=titles, polls=polls, users=users)
        for row in rows
    ]
    out.sort(key=lambda r: (r.get("order_date") or "", r.get("created_at") or ""))
    return out


class ItemIn(BaseModel):
    item_name: str = ""
    name: str = ""
    qty: int = 1
    # The snapshot JSON stores user_id as an int, but admin-added items send ""
    # (no Telegram user). Accept either; we stringify before persisting.
    user_id: Optional[Union[int, str]] = None


class ItemsBody(BaseModel):
    items: List[ItemIn]


@router.put("/{order_id}/items")
async def update_order_items(
    order_id: str,
    body: ItemsBody,
    _: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Replace an order's item list. Admin-only (CRUD from the calendar detail
    sheet: add / edit / remove items). Returns the reshaped order so the Mini
    App can re-render without a reload; 404 if the order doesn't exist."""
    items = [
        {
            "item_name": it.item_name,
            "name": it.name,
            "qty": it.qty,
            "user_id": "" if it.user_id is None else str(it.user_id),
        }
        for it in body.items
    ]
    row = await sheets_orders.update_items(order_id, items)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )

    titles = await _chat_titles()
    polls = await _poll_meta()
    users = await _user_names()
    return _shape_order(row, titles=titles, polls=polls, users=users)
