"""
/api/payers — the Settings "Paid list".

Rows are created automatically when someone taps the Order button (see
bot/sheets/payers.py). Admins view the list and attach a KHQR string or
QR image filename, or correct the display name. Admin-only.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..sheets import payers as sheets_payers
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_admin

router = APIRouter(prefix="/payers", tags=["payers"])


class PayerUpdate(BaseModel):
    full_name: Optional[str] = None
    qr_filename: Optional[str] = None
    khqr_text: Optional[str] = None


@router.get("")
async def list_payers(_: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    rows = await sheets_payers.list_all()
    # Most recent payers first.
    rows.sort(key=lambda r: str(r.get("last_paid_at") or ""), reverse=True)
    return rows


@router.put("/{user_id}")
async def update_payer(
    user_id: str,
    body: PayerUpdate,
    _: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payer management requires Google Sheets to be configured.",
        )
    fields: Dict[str, Any] = {}
    if body.full_name is not None:
        fields["full_name"] = body.full_name
    if body.qr_filename is not None:
        fields["qr_filename"] = body.qr_filename
    if body.khqr_text is not None:
        fields["khqr_text"] = body.khqr_text
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    updated = await repo.update("payer", user_id, fields)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payer not found")
    return updated
