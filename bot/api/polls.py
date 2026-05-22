"""
/api/polls — read-only listing of the `poll` tab plus per-poll votes.

Used by the Mini App calendar to render past food polls as events at
their created_at day/time, and to populate the event detail sheet
with the live vote data from the `vote` tab.

Access model
------------
Admin-only. Members get 403 and the Mini App calendar shows an
"admin role required" empty state instead of leaking any data.
"""

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..sheets import orders as sheets_orders
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_admin

router = APIRouter(prefix="/polls", tags=["polls"])


def _parse_options(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_selections(raw: Any) -> List[str]:
    return _parse_options(raw)


async def _chat_titles() -> Dict[str, str]:
    """{chat_id (as str): title} — used to annotate polls with their chat name."""
    rows = await repo.list_all("chat")
    out: Dict[str, str] = {}
    for r in rows:
        cid = str(r.get("chat_id", "")).strip()
        if cid:
            out[cid] = str(r.get("title", "")).strip()
    return out


@router.get("")
async def list_polls(
    date_from: Optional[str] = Query(None, alias="from",
                                     description="Inclusive YYYY-MM-DD lower bound on created_at."),
    date_to: Optional[str] = Query(None, alias="to",
                                   description="Inclusive YYYY-MM-DD upper bound on created_at."),
    _: dict = Depends(require_admin),
) -> List[Dict[str, Any]]:
    """Polls in a date range, annotated with vote_count and chat_title."""
    if not is_configured():
        return []

    def in_range(row: Dict[str, Any]) -> bool:
        date_prefix = str(row.get("created_at", ""))[:10]
        if not date_prefix:
            return False
        if date_from and date_prefix < date_from:
            return False
        if date_to and date_prefix > date_to:
            return False
        return True

    polls = await repo.filter_rows("poll", in_range)
    if not polls:
        return []

    poll_ids = {str(p.get("poll_id", "")) for p in polls}
    votes = await repo.filter_rows(
        "vote", lambda r: str(r.get("poll_id", "")) in poll_ids,
    )

    vote_counts: Dict[str, int] = {}
    for v in votes:
        pid = str(v.get("poll_id", ""))
        vote_counts[pid] = vote_counts.get(pid, 0) + 1

    titles = await _chat_titles()

    out: List[Dict[str, Any]] = []
    for p in polls:
        pid = str(p.get("poll_id", ""))
        cid = str(p.get("chat_id", ""))
        out.append({
            "poll_id": pid,
            "chat_id": cid,
            "chat_title": titles.get(cid, ""),
            "question": p.get("question", ""),
            "options": _parse_options(p.get("options")),
            "status": str(p.get("status", "OPEN")).upper(),
            "created_at": p.get("created_at", ""),
            "closed_at": p.get("closed_at", ""),
            "vote_count": vote_counts.get(pid, 0),
        })
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


@router.get("/{poll_id}/votes")
async def list_poll_votes(
    poll_id: str,
    _: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Votes for one poll. Admin-only."""
    if not is_configured():
        return {"poll": None, "votes": []}

    poll = await repo.find_by_pk("poll", poll_id)
    if not poll:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Poll {poll_id} not found",
        )

    votes_raw = await repo.filter_rows(
        "vote", lambda r: str(r.get("poll_id", "")) == str(poll_id),
    )
    votes = [
        {
            "user_id": v.get("user_id"),
            "user_name": v.get("user_name", ""),
            "selections": _parse_selections(v.get("selected_options")),
            "updated_at": v.get("updated_at", ""),
        }
        for v in votes_raw
    ]
    votes.sort(key=lambda v: v.get("updated_at") or "")

    # "Paid by" comes from the order row written when someone tapped Order.
    order_row = await sheets_orders.get_by_poll(poll_id)
    paid_by = None
    if order_row:
        paid_by = {
            "user_id": str(order_row.get("user_id", "")),
            "username": str(order_row.get("username", "")),
            "created_at": str(order_row.get("created_at", "")),
        }

    return {
        "poll": {
            "poll_id": poll_id,
            "chat_id": str(poll.get("chat_id", "")),
            "question": poll.get("question", ""),
            "options": _parse_options(poll.get("options")),
            "status": str(poll.get("status", "OPEN")).upper(),
            "created_at": poll.get("created_at", ""),
        },
        "votes": votes,
        "paid_by": paid_by,
    }
