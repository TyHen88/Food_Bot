"""
/api/invoices — sent-invoice history for the Mini App.

An invoice row is written whenever an admin sends an invoice from the
orders page (see orders.generate_order_invoice). Listing and detail are
open to any verified member (scoped to their groups, like /api/orders);
re-sending to the Telegram group is admin-only.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..sheets import invoices as sheets_invoices
from ..sheets import payers as sheets_payers
from .auth import caller_chat_id, caller_user_id, require_admin, require_member
from .members import user_chats
from .orders import _build_invoice_text, _chat_titles, payer_qr_data_uri, send_invoice_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])


async def _allowed_chats(auth: dict) -> Optional[set]:
    """Chats the caller may see; None = unrestricted (admin)."""
    if auth.get("is_admin"):
        return None
    chats = await user_chats(caller_user_id(auth))
    launch = caller_chat_id(auth)
    if launch:
        chats.add(launch)
    return chats


def _caller_names(auth: dict) -> set:
    """Lowercased display-name candidates for the caller, used to match old
    invoice details that predate the per-entry user_id field."""
    u = auth.get("user") or {}
    first = str(u.get("first_name") or "").strip()
    last = str(u.get("last_name") or "").strip()
    return {
        n.lower()
        for n in (u.get("username") or "", first, f"{first} {last}".strip())
        if n
    }


def _my_amount(details: List[Dict[str, Any]], caller_id: str, caller_names: set) -> float:
    """Sum of the caller's per-person subtotals in one invoice's details.
    Entries carrying user_id match on it; legacy entries fall back to a
    display-name match."""
    total = 0.0
    for d in details or []:
        try:
            sub = float(d.get("subtotal") or 0)
        except (TypeError, ValueError):
            sub = 0.0
        did = str(d.get("user_id") or "").strip()
        if did:
            if caller_id and did == caller_id:
                total += sub
        elif str(d.get("user_name") or "").strip().lower() in caller_names:
            total += sub
    return round(total, 2)


@router.get("")
async def list_invoices(
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's invoices."),
    auth: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    """Invoices newest-first, without the full breakdown (see detail)."""
    auth_chat = caller_chat_id(auth)
    if not chat_id:
        chat_id = auth_chat

    rows = await sheets_invoices.list_all()

    allowed = await _allowed_chats(auth)
    if chat_id:
        wanted = str(chat_id).strip()
        if allowed is not None and wanted not in allowed:
            return []
        rows = [r for r in rows if r["chat_id"] == wanted]
    elif allowed is not None:
        rows = [r for r in rows if r["chat_id"] in allowed]

    titles = await _chat_titles()
    caller_id = caller_user_id(auth)
    names = _caller_names(auth)
    out = []
    for r in rows:
        out.append({
            **{k: v for k, v in r.items() if k != "details"},
            "chat_title": titles.get(r["chat_id"], ""),
            "person_count": len(r["details"]),
            "my_amount": _my_amount(r["details"], caller_id, names),
        })
    out.sort(key=lambda r: (r.get("order_date") or "", r.get("last_sent_at") or ""), reverse=True)
    return out


async def _get_visible_invoice(invoice_id: str, auth: dict) -> Dict[str, Any]:
    inv = await sheets_invoices.get(invoice_id)
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    allowed = await _allowed_chats(auth)
    if allowed is not None and inv["chat_id"] not in allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return inv


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    request: Request,
    auth: dict = Depends(require_member),
) -> Dict[str, Any]:
    """Full invoice incl. per-person breakdown and the payer's QR (image as
    data URI + raw KHQR payload) so the viewer can pay. Any member of the chat."""
    inv = await _get_visible_invoice(invoice_id, auth)
    titles = await _chat_titles()
    out = {**inv, "chat_title": titles.get(inv["chat_id"], "")}

    if inv["payer_user_id"]:
        payer = await sheets_payers.get(inv["payer_user_id"])
        if payer:
            out["payer_khqr_text"] = str(payer.get("khqr_text") or "")
            bot = request.app.state.application.bot
            qr = await payer_qr_data_uri(bot, payer.get("qr_filename"))
            if qr:
                out["payer_qr_image"] = qr
    return out


@router.post("/{invoice_id}/resend")
async def resend_invoice(
    invoice_id: str,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Re-send the stored invoice message to its Telegram group. Admin-only."""
    inv = await _get_visible_invoice(invoice_id, auth)
    if not inv["chat_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice has no chat to send to",
        )

    # Rebuild the message from the stored breakdown; KHQR is re-fetched so a
    # payer who added their QR after the first send still gets it included.
    user_orders = {d.get("user_name", "Guest"): d.get("items", []) for d in inv["details"]}
    khqr_text = ""
    payer_row = None
    if inv["payer_user_id"]:
        payer_row = await sheets_payers.get(inv["payer_user_id"])
        if payer_row:
            khqr_text = payer_row.get("khqr_text") or ""

    text = _build_invoice_text(inv["order_date"], user_orders, inv["payer_name"], khqr_text)

    try:
        application = request.app.state.application
        await send_invoice_message(
            application.bot, int(inv["chat_id"]), text, payer_row,
        )
    except Exception as e:
        logger.error(f"Failed to resend invoice {invoice_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to send invoice to group chat: {e}",
        )

    user_id = (auth.get("user") or {}).get("id")
    await sheets_invoices.save_sent(
        order_id=inv["order_id"],
        poll_id=inv["poll_id"],
        chat_id=inv["chat_id"],
        order_date=inv["order_date"],
        details=inv["details"],
        total=inv["total"],
        payer_user_id=inv["payer_user_id"],
        payer_name=inv["payer_name"],
        sent_by=user_id,
    )
    return {"ok": True, "sent_count": inv["sent_count"] + 1}
