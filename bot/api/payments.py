"""
/api/payments — payment history and manual assignment for the Mini App.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..payway import PayWayTransaction
from ..settlement import process_transaction_settlement
from ..sheets import payments as sheets_payments
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_user_id, require_admin, require_member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


class AssignPaymentBody(BaseModel):
    user_id: str


@router.get("")
async def list_payments(
    user_id: Optional[str] = Query(None, description="Filter payments by user_id."),
    auth: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    """List payments (admins see all, members see their own)."""
    if not is_configured():
        return []

    caller_id = caller_user_id(auth)
    is_admin = auth.get("is_admin")

    all_payments = await sheets_payments.list_all()
    # Sort newest first
    all_payments.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    if not is_admin:
        return [p for p in all_payments if p.get("user_id") == caller_id]

    if user_id:
        wanted = str(user_id).strip()
        return [p for p in all_payments if p.get("user_id") == wanted]

    return all_payments


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
