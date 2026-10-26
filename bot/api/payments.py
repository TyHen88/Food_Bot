"""
/api/payments — payment history and manual assignment for the Mini App.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from zoneinfo import ZoneInfo

from ..config import TIMEZONE
from ..payway import PayWayTransaction
from ..settlement import process_transaction_settlement
from ..sheets import payments as sheets_payments
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_user_id, require_admin, require_member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


def _local_day(ts: Any) -> str:
    """created_at is stored in UTC; bucket it by the group's local day so a
    06:30 ICT payment isn't filed under yesterday."""
    raw = str(ts or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()


class AssignPaymentBody(BaseModel):
    user_id: str


@router.get("")
async def list_payments(
    user_id: Optional[str] = Query(None, description="Filter payments by user_id."),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (MATCHED, UNMATCHED, etc.)."),
    search: Optional[str] = Query(None, description="Search by sender name, trx_id, or username."),
    date_from: Optional[str] = Query(None, alias="from", description="Inclusive YYYY-MM-DD lower bound on created_at."),
    date_to: Optional[str] = Query(None, alias="to", description="Inclusive YYYY-MM-DD upper bound on created_at."),
    page: Optional[int] = Query(None, ge=1, description="Page number for pagination."),
    page_size: int = Query(20, ge=1, le=100, description="Items per page."),
    auth: dict = Depends(require_member),
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """List payments (admins see all, members see their own) enriched with user profile and stats."""
    if not is_configured():
        if page is not None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0, "stats": {"total": 0, "matched": 0, "unmatched": 0, "total_usd": 0.0}}
        return []

    caller_id = caller_user_id(auth)
    is_admin = auth.get("is_admin")

    all_payments = await sheets_payments.list_all()
    # Sort newest first
    all_payments.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    users_map: Dict[str, Dict[str, Any]] = {}
    if is_configured():
        for u in await repo.list_all("user"):
            uid = str(u.get("user_id") or "").strip()
            if uid:
                users_map[uid] = u

    # Enrich payments with resolved member information
    enriched: List[Dict[str, Any]] = []
    for p in all_payments:
        uid = str(p.get("user_id") or "").strip()
        u_info = users_map.get(uid, {})
        full_name = str(u_info.get("full_name") or "").strip()
        username = str(u_info.get("username") or "").strip()
        
        enriched.append({
            **p,
            "user_name": full_name or username or p.get("sender_name") or "",
            "username": username,
            "bank_name": str(u_info.get("bank_name") or "").strip(),
        })

    # Scope permissions
    if not is_admin:
        filtered = [p for p in enriched if p.get("user_id") == caller_id]
    else:
        filtered = enriched

    if user_id:
        wanted_u = str(user_id).strip()
        filtered = [p for p in filtered if p.get("user_id") == wanted_u]

    if status_filter and status_filter.strip() and status_filter.upper() != "ALL":
        sf = status_filter.strip().upper()
        filtered = [p for p in filtered if str(p.get("status", "")).upper() == sf]

    if date_from or date_to:
        def _in_range(p: Dict[str, Any]) -> bool:
            day = _local_day(p.get("created_at"))
            if date_from and day < date_from:
                return False
            if date_to and day > date_to:
                return False
            return True
        filtered = [p for p in filtered if _in_range(p)]

    if search and search.strip():
        q = search.strip().lower()
        filtered = [
            p for p in filtered
            if q in str(p.get("sender_name", "")).lower()
            or q in str(p.get("trx_id", "")).lower()
            or q in str(p.get("user_name", "")).lower()
            or q in str(p.get("username", "")).lower()
            or q in str(p.get("apv", "")).lower()
        ]

    total_count = len(filtered)
    matched_count = sum(1 for p in filtered if str(p.get("status", "")).upper() == "MATCHED")
    unmatched_count = sum(1 for p in filtered if str(p.get("status", "")).upper() == "UNMATCHED")
    total_amount_usd = round(sum(float(p.get("amount_usd") or 0.0) for p in filtered), 2)

    stats = {
        "total": total_count,
        "matched": matched_count,
        "unmatched": unmatched_count,
        "total_usd": total_amount_usd,
    }

    if page is not None:
        import math
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_items = filtered[start_idx:end_idx]
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1
        return {
            "items": paged_items,
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "stats": stats,
        }

    return filtered


@router.post("/{payment_id}/assign")
async def assign_payment(
    payment_id: str,
    body: AssignPaymentBody,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Manually assign an UNMATCHED payment to a member."""
    if not is_configured():
        raise HTTPException(status_code=500, detail="Sheets not configured")

    rows = await repo.list_all("payment")
    target = None
    for r in rows:
        if str(r.get("payment_id", "")).strip() == str(payment_id).strip():
            target = r
            break

    if not target:
        raise HTTPException(status_code=404, detail="Payment not found")

    user_row = await repo.find_by_pk("user", str(body.user_id))
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    # Construct transaction object to re-run settlement
    tx = PayWayTransaction(
        amount=float(target.get("amount") or 0),
        currency=str(target.get("currency", "USD")),
        amount_usd=float(target.get("amount_usd") or 0),
        sender_name=str(target.get("sender_name", "")),
        account_mask="",
        date_str=str(target.get("created_at", "")),
        payment_method="ABA KHQR",
        merchant="MANUAL",
        trx_id=str(target.get("trx_id", "")),
        apv=str(target.get("apv", "")),
        raw_text=str(target.get("raw_text", "")),
    )

    # Update payment with user_id and re-trigger settlement
    res = await process_transaction_settlement(
        tx, force_settle=True, assigned_user=user_row
    )
    return res
