"""
/api/invoices — sent-invoice history for the Mini App.

An invoice row is written whenever an admin sends an invoice from the
orders page (see orders.generate_order_invoice). Listing and detail are
open to any verified member (scoped to their groups, like /api/orders);
re-sending to the Telegram group is admin-only.
"""

import logging
import math
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import exchange
from ..people import is_same_person, name_variants
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
    """Normalized display-name candidates for the caller, used to match old
    invoice details that predate the per-entry user_id field."""
    u = auth.get("user") or {}
    return name_variants(
        username=u.get("username") or "",
        first_name=u.get("first_name") or "",
        last_name=u.get("last_name") or "",
    )


def _my_amount_and_paid(details: List[Dict[str, Any]], caller_id: str, caller_names: set) -> tuple[float, bool]:
    """Sum of caller's subtotals and whether they have fully paid."""
    total = 0.0
    paid = True
    found = False
    for d in details or []:
        if not is_same_person(d.get("user_id"), d.get("user_name"), caller_id, caller_names):
            continue
        found = True
        try:
            total += float(d.get("subtotal") or 0)
        except (TypeError, ValueError):
            pass
        if not d.get("paid"):
            paid = False
    return round(total, 2), (paid if found else False)


@router.get("")
async def list_invoices(
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's invoices."),
    page: Optional[int] = Query(None, ge=1, description="Page number for pagination."),
    page_size: int = Query(10, ge=1, le=100, description="Items per page."),
    date_from: Optional[str] = Query(None, alias="from", description="Inclusive YYYY-MM-DD lower bound."),
    date_to: Optional[str] = Query(None, alias="to", description="Inclusive YYYY-MM-DD upper bound."),
    search: Optional[str] = Query(None, description="Search term for payer, chat title, or date."),
    auth: dict = Depends(require_member),
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Invoices newest-first, without the full breakdown (see detail). Supports pagination & filtering."""
    auth_chat = caller_chat_id(auth)
    if not chat_id:
        chat_id = auth_chat

    rows = await sheets_invoices.list_all()

    allowed = await _allowed_chats(auth)
    if chat_id:
        wanted = str(chat_id).strip()
        if allowed is not None and wanted not in allowed:
            if page is not None:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0, "stats": {"orders": 0, "amount": 0.0, "amount_khr": 0.0}}
            return []
        rows = [r for r in rows if r["chat_id"] == wanted]
    elif allowed is not None:
        rows = [r for r in rows if r["chat_id"] in allowed]

    titles = await _chat_titles()
    caller_id = caller_user_id(auth)
    names = _caller_names(auth)
    out = []
    for r in rows:
        order_date = str(r.get("order_date") or "")
        if date_from and order_date < date_from:
            continue
        if date_to and order_date > date_to:
            continue

        details = r.get("details") or []
        my_amount, my_paid = _my_amount_and_paid(details, caller_id, names)
        rate = r.get("usd_khr_rate") or 0
        paid_count = sum(1 for d in details if d.get("paid"))
        all_paid = bool(details and paid_count == len(details))

        chat_title = titles.get(r["chat_id"], "")
        total_val = float(r.get("total") or 0)
        total_khr_val = exchange.to_khr(total_val, rate) if rate else None
        my_amount_khr_val = exchange.to_khr(my_amount, rate) if rate else None

        out.append({
            **{k: v for k, v in r.items() if k != "details"},
            "chat_title": chat_title,
            "person_count": len(details),
            "paid_count": paid_count,
            "all_paid": all_paid,
            "my_amount": my_amount,
            "my_paid": my_paid,
            # Converted at the invoice's own pinned rate, never today's.
            "my_amount_khr": my_amount_khr_val,
            "total_khr": total_khr_val,
        })

    out.sort(key=lambda r: (r.get("order_date") or "", r.get("last_sent_at") or ""), reverse=True)

    filtered = out
    if search and search.strip():
        q = search.strip().lower()
        filtered = [
            inv for inv in out
            if q in (inv.get("payer_name") or "").lower()
            or q in (inv.get("chat_title") or "").lower()
            or q in (inv.get("order_date") or "").lower()
            or q in (inv.get("order_id") or "").lower()
            or q in (inv.get("invoice_id") or "").lower()
        ]

    total_orders = len(filtered)
    total_amount = sum(float(inv.get("total") or 0) for inv in filtered)
    total_amount_khr = sum(float(inv.get("total_khr") or 0) for inv in filtered if inv.get("total_khr"))

    stats_data = {
        "orders": total_orders,
        "amount": round(total_amount, 2),
        "amount_khr": round(total_amount_khr, 2),
    }

    if page is not None:
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_items = filtered[start_idx:end_idx]
        total_pages = math.ceil(total_orders / page_size) if total_orders > 0 else 1
        return {
            "items": paged_items,
            "total": total_orders,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "stats": stats_data,
        }

    return filtered


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

    # Re-send at the rate the invoice was ORIGINALLY sent at (pinned on the
    # row), so the riel figures people already paid don't move. Invoices from
    # before exchange rates existed have no rate and stay dollars-only.
    pinned_rate = (
        {"usd_khr": inv["usd_khr_rate"], "rate_date": inv["rate_date"]}
        if inv.get("usd_khr_rate") else None
    )
    text = _build_invoice_text(
        inv["order_date"], user_orders, inv["payer_name"], khqr_text, pinned_rate,
        inv.get("display_currencies"),
    )

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
        usd_khr_rate=inv["usd_khr_rate"],
        rate_date=inv["rate_date"],
        display_currencies=inv.get("display_currencies"),
        sent_by=user_id,
    )
    return {"ok": True, "sent_count": inv["sent_count"] + 1}


class MarkPaidRequest(BaseModel):
    user_id: Optional[str] = ""
    user_name: Optional[str] = ""
    paid: bool = True
    paid_amount: Optional[float] = None


class MarkPaidBulkRequest(BaseModel):
    invoice_ids: List[str]
    user_id: Optional[str] = ""
    user_name: Optional[str] = ""
    paid: bool = True


@router.post("/{invoice_id}/mark-paid")
async def mark_invoice_person_paid(
    invoice_id: str,
    body: MarkPaidRequest,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Admin-only: Mark or unmark a specific person's lunch debt as PAID in an invoice."""
    names = name_variants(username=body.user_name or "", full_name=body.user_name or "")
    success = await sheets_invoices.set_member_paid_status(
        invoice_id=invoice_id,
        user_id=body.user_id or "",
        user_names=names,
        is_paid=body.paid,
        payment_id=f"ADMIN_{caller_user_id(auth)}",
        paid_amount=body.paid_amount,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update invoice paid status (invoice or user not found)",
        )
    return {"ok": True, "invoice_id": invoice_id, "paid": body.paid}


@router.post("/mark-paid-bulk")
async def mark_invoices_bulk_paid(
    body: MarkPaidBulkRequest,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Admin-only: Bulk mark or unmark a specific user across multiple invoices."""
    names = name_variants(username=body.user_name or "", full_name=body.user_name or "")
    updated_count = 0
    for inv_id in body.invoice_ids:
        if await sheets_invoices.set_member_paid_status(
            invoice_id=inv_id,
            user_id=body.user_id or "",
            user_names=names,
            is_paid=body.paid,
            payment_id=f"ADMIN_{caller_user_id(auth)}",
        ):
            updated_count += 1
    return {"ok": True, "updated_count": updated_count, "paid": body.paid}
