"""
/api/invoices — sent-invoice history for the Mini App.

An invoice row is written whenever an admin sends an invoice from the
orders page (see orders.generate_order_invoice). Listing and detail are
open to any verified member (scoped to their groups, like /api/orders);
re-sending to the Telegram group is admin-only.
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import exchange
from ..people import exact_names, is_same_person, name_variants
from ..sheets import invoices as sheets_invoices
from ..sheets.invoices import detail_payment_state, is_detail_payer
from ..sheets import payers as sheets_payers
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_chat_id, caller_user_id, require_admin, require_member
from .members import user_chats
from .orders import _build_invoice_text, _chat_titles, caller_names, payer_qr_data_uri, send_invoice_message

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


def _my_share(
    inv: Dict[str, Any],
    caller_id: str,
    caller_names: set,
) -> Tuple[float, bool, float]:
    """(amount, paid, due) for the caller: the sum of their subtotals,
    whether they have fully paid, and what is still owed after partial payments."""
    total = 0.0
    due = 0.0
    paid = True
    found = False
    for d in inv.get("details") or []:
        if not is_same_person(d.get("user_id"), d.get("user_name"), caller_id, caller_names):
            continue
        found = True
        is_p, p_amt, d_due = detail_payment_state(d, is_payer=is_detail_payer(inv, d))
        total += float(d.get("subtotal") or 0.0)
        due += d_due
        if not is_p:
            paid = False
    return round(total, 2), (paid if found else False), round(due, 2)


@router.get("")
async def list_invoices(
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's invoices."),
    page: Optional[int] = Query(None, ge=1, description="Page number for pagination."),
    page_size: int = Query(10, ge=1, le=100, description="Items per page."),
    date_from: Optional[str] = Query(None, alias="from", description="Inclusive YYYY-MM-DD lower bound."),
    date_to: Optional[str] = Query(None, alias="to", description="Inclusive YYYY-MM-DD upper bound."),
    search: Optional[str] = Query(None, description="Search term for payer, chat title, or date."),
    user_id: Optional[str] = Query(None, description="Target user_id to compute my_amount and my_paid for."),
    auth: dict = Depends(require_member),
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Invoices newest-first, without the full breakdown (see detail). Supports pagination & filtering."""
    auth_chat = caller_chat_id(auth)
    if not chat_id:
        chat_id = auth_chat
    if not chat_id:
        caller_id = caller_user_id(auth)
        if caller_id and is_configured():
            u_row = await repo.find_by_pk("user", caller_id)
            if u_row and str(u_row.get("chat_id", "")).strip():
                chat_id = str(u_row.get("chat_id")).strip()
        if not chat_id and caller_id:
            m_chats = await user_chats(caller_id)
            if m_chats:
                chat_id = sorted(list(m_chats))[0]

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

    # Determine target user for calculating personal invoice amounts
    target_id = caller_id
    target_names = await caller_names(auth)
    if user_id and str(user_id).strip():
        req_uid = str(user_id).strip()
        # Members may target another member too (View Spend): rows are already
        # scoped to the caller's chats and every detail is visible in them, so
        # this exposes nothing new — but ignoring it made the member's spend
        # modal show the VIEWER's payer/paid status instead of the member's.
        if req_uid != caller_id:
            target_id = req_uid
            target_names = set()
            if is_configured():
                u_row = await repo.find_by_pk("user", target_id)
                if u_row:
                    target_names = name_variants(
                        username=u_row.get("username") or "",
                        full_name=u_row.get("full_name") or "",
                    )

    curr_rate_row = await exchange.current()
    system_rate = float((curr_rate_row or {}).get("usd_khr") or 4047)

    out = []
    for r in rows:
        # Compare the date part only, as orders.list_in_range does, so a
        # stray time suffix can't push an order outside an inclusive bound.
        order_date = str(r.get("order_date") or "")[:10]
        if date_from and order_date < date_from:
            continue
        if date_to and order_date > date_to:
            continue

        details = r.get("details") or []

        # Is the targeted user the designated payer of THIS invoice? By id, or
        # (legacy name-only payer) because one of their own details is the payer.
        payer_id = str(r.get("payer_user_id") or "").strip()
        is_current_payer = bool(payer_id and target_id and payer_id == target_id) or any(
            is_detail_payer(r, d) for d in details
            if is_same_person(d.get("user_id"), d.get("user_name"), target_id, target_names)
        )
        my_amount, my_paid, my_due = _my_share(r, target_id, target_names)
        rate = float(r.get("usd_khr_rate") or 0)
        effective_rate = rate if rate > 0 else system_rate
        paid_members: List[Dict[str, Any]] = []
        unpaid_members: List[Dict[str, Any]] = []
        parsed_details: List[Dict[str, Any]] = []

        for d in details:
            d_uid = str(d.get("user_id") or "").strip()
            d_uname = str(d.get("user_name") or "").strip()
            sub = float(d.get("subtotal") or 0.0)

            # Only the payer of THIS invoice is automatically paid for their own dish
            d_is_payer = is_detail_payer(r, d)
            is_p, p_amt, due = detail_payment_state(d, is_payer=d_is_payer)

            d_enriched = {
                "user_id": d_uid,
                "user_name": d_uname,
                "subtotal": sub,
                "paid": is_p,
                "paid_amount": p_amt,
                "due": due,
                "is_payer": d_is_payer,
                "payment_id": "PAYER_SELF" if d_is_payer else (str(d.get("payment_id") or "") if is_p or p_amt > 0 else ""),
                "paid_at": str(d.get("paid_at") or ""),
                "paid_note": str(d.get("paid_note") or "") if is_p else "",
                "items": d.get("items") or [],
            }
            parsed_details.append(d_enriched)

            if is_p:
                paid_members.append({
                    "user_id": d_uid,
                    "user_name": d_uname,
                    "amount": p_amt if p_amt > 0 else sub,
                    "amount_khr": exchange.to_khr(p_amt if p_amt > 0 else sub, effective_rate),
                    "paid_at": str(d.get("paid_at") or ""),
                    "payment_id": "PAYER_SELF" if d_is_payer else str(d.get("payment_id") or ""),
                    "is_payer": d_is_payer,
                    "items": d.get("items") or [],
                })
            else:
                unpaid_members.append({
                    "user_id": d_uid,
                    "user_name": d_uname,
                    "subtotal": sub,
                    "due": due,
                    "due_khr": exchange.to_khr(due, effective_rate),
                    "items": d.get("items") or [],
                })

        paid_count = len(paid_members)
        all_paid = bool(details and len(unpaid_members) == 0)

        # Extract matched user's items for quick display on frontend
        my_items = []
        for d in details:
            if is_same_person(d.get("user_id"), d.get("user_name"), target_id, target_names):
                my_items.extend(d.get("items") or [])

        # Calculate payer receivables
        payer_total_to_collect = sum(m["subtotal"] for m in unpaid_members)
        payer_unpaid_amount = sum(m["due"] for m in unpaid_members)
        payer_unpaid_count = len(unpaid_members)

        chat_title = titles.get(r["chat_id"], "")
        total_val = float(r.get("total") or 0)
        total_khr_val = exchange.to_khr(total_val, effective_rate)
        my_amount_khr_val = exchange.to_khr(my_amount, effective_rate)

        out.append({
            **{k: v for k, v in r.items() if k != "details"},
            "details": parsed_details,
            "chat_title": chat_title,
            "person_count": len(details),
            "paid_count": paid_count,
            "unpaid_count": len(unpaid_members),
            "all_paid": all_paid,
            "my_amount": my_amount,
            "my_paid": my_paid,
            "my_due": my_due,
            "my_items": my_items,
            "usd_khr_rate": effective_rate,
            "my_amount_khr": my_amount_khr_val,
            "my_due_khr": exchange.to_khr(my_due, effective_rate),
            "total_khr": total_khr_val,
            "is_payer": is_current_payer,
            "payer_unpaid_amount": round(payer_unpaid_amount, 2),
            "payer_total_to_collect": round(payer_total_to_collect, 2),
            "payer_unpaid_count": payer_unpaid_count,
            "paid_members": paid_members,
            "unpaid_members": unpaid_members,
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
    total_amount_khr = sum(float(inv.get("total_khr") or 0) for inv in filtered)

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


@router.get("/day-summary")
async def get_day_summary(
    date: str = Query(..., description="Target date in YYYY-MM-DD format"),
    chat_id: Optional[str] = Query(None, description="Scope to a specific chat group."),
    auth: dict = Depends(require_member),
) -> Dict[str, Any]:
    """
    Returns unified payment summary for a specific calendar day:
    who has paid, who has not paid, amount collected, and outstanding debt.
    """
    target_day = date.strip()[:10]
    inv_list = await sheets_invoices.list_all()
    if chat_id:
        wanted_chat = str(chat_id).strip()
        inv_list = [inv for inv in inv_list if str(inv.get("chat_id", "")).strip() == wanted_chat]

    curr_rate_row = await exchange.current()
    system_rate = float((curr_rate_row or {}).get("usd_khr") or 4047)
    titles = await _chat_titles()

    # Match invoices for this day
    day_invoices = [inv for inv in inv_list if str(inv.get("order_date", ""))[:10] == target_day]

    total_spend = 0.0
    total_paid = 0.0
    total_due = 0.0
    paid_members_map: Dict[str, Dict[str, Any]] = {}
    unpaid_members_map: Dict[str, Dict[str, Any]] = {}
    orders_info: List[Dict[str, Any]] = []

    for inv in day_invoices:
        inv_id = str(inv.get("invoice_id") or inv.get("order_id") or "")
        cid = str(inv.get("chat_id") or "")
        rate = float(inv.get("usd_khr_rate") or 0) or system_rate
        details = inv.get("details") or []
        inv_total = float(inv.get("total") or 0.0)
        total_spend += inv_total

        orders_info.append({
            "invoice_id": inv_id,
            "order_id": inv_id,
            "chat_id": cid,
            "chat_title": titles.get(cid, ""),
            "payer_name": inv.get("payer_name") or "",
            "payer_user_id": inv.get("payer_user_id") or "",
            "total": inv_total,
            "total_khr": exchange.to_khr(inv_total, rate),
            "item_count": sum(len(d.get("items") or []) for d in details),
        })

        for d in details:
            d_uid = str(d.get("user_id") or "").strip()
            d_uname = str(d.get("user_name") or "Guest").strip()
            sub = float(d.get("subtotal") or 0.0)

            # Only the payer of this specific invoice is automatically paid for their own dish
            d_is_payer = is_detail_payer(inv, d)
            is_p, p_amt, due = detail_payment_state(d, is_payer=d_is_payer)

            member_key = d_uid or d_uname
            if is_p:
                total_paid += p_amt if p_amt > 0 else sub
                if member_key in paid_members_map:
                    paid_members_map[member_key]["amount"] += (p_amt if p_amt > 0 else sub)
                    paid_members_map[member_key]["items"].extend(d.get("items") or [])
                    if d_is_payer:
                        paid_members_map[member_key]["is_payer"] = True
                else:
                    paid_members_map[member_key] = {
                        "user_id": d_uid,
                        "user_name": d_uname,
                        "amount": p_amt if p_amt > 0 else sub,
                        "amount_khr": exchange.to_khr(p_amt if p_amt > 0 else sub, rate),
                        "paid_at": str(d.get("paid_at") or ""),
                        "payment_id": "PAYER_SELF" if d_is_payer else str(d.get("payment_id") or ""),
                        "is_payer": d_is_payer,
                        "items": list(d.get("items") or []),
                    }
            else:
                total_due += due
                if member_key in unpaid_members_map:
                    unpaid_members_map[member_key]["subtotal"] += sub
                    unpaid_members_map[member_key]["due"] += due
                    unpaid_members_map[member_key]["items"].extend(d.get("items") or [])
                else:
                    unpaid_members_map[member_key] = {
                        "user_id": d_uid,
                        "user_name": d_uname,
                        "subtotal": sub,
                        "due": due,
                        "due_khr": exchange.to_khr(due, rate),
                        "items": list(d.get("items") or []),
                    }

    paid_list = list(paid_members_map.values())
    unpaid_list = list(unpaid_members_map.values())

    return {
        "date": target_day,
        "chat_id": chat_id,
        "total_spend": round(total_spend, 2),
        "total_paid": round(total_paid, 2),
        "total_due": round(total_due, 2),
        "total_spend_khr": exchange.to_khr(total_spend, system_rate),
        "total_paid_khr": exchange.to_khr(total_paid, system_rate),
        "total_due_khr": exchange.to_khr(total_due, system_rate),
        "usd_khr_rate": system_rate,
        "paid_count": len(paid_list),
        "unpaid_count": len(unpaid_list),
        "all_paid": bool(day_invoices and len(unpaid_list) == 0),
        "paid_members": paid_list,
        "unpaid_members": unpaid_list,
        "orders": orders_info,
    }


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
    # Server-computed paid state per detail, same rule as the list — the raw
    # stored flag can be an old auto-paid or stale PAYER_SELF value.
    details = []
    for d in inv["details"]:
        d_is_payer = is_detail_payer(inv, d)
        is_p, p_amt, due = detail_payment_state(d, is_payer=d_is_payer)
        details.append({**d, "paid": is_p, "paid_amount": p_amt, "due": due, "is_payer": d_is_payer})
    out = {**inv, "details": details, "chat_title": titles.get(inv["chat_id"], "")}

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
    payer_names = name_variants(
        username=inv.get("payer_name") or "",
        full_name=(payer_row.get("full_name") if payer_row else "") or inv.get("payer_name") or "",
    )
    text = _build_invoice_text(
        inv["order_date"], user_orders, inv["payer_name"], khqr_text, pinned_rate,
        inv.get("display_currencies"),
        payer_user_id=inv.get("payer_user_id"),
        payer_names=payer_names,
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
    note: Optional[str] = ""


async def _mark_target_names(user_id: Optional[str], user_name: Optional[str]) -> set:
    """Names that identify the person being marked, for id-less invoice
    entries only (ids are matched exactly in sheets_invoices). Exact names
    only — first-name variants let marking "Tii ♏️" settle a guest "Tii"."""
    uid = str(user_id or "").strip()
    names = exact_names(user_name)
    if uid and is_configured():
        u_row = await repo.find_by_pk("user", uid)
        if u_row:
            names |= exact_names(u_row.get("username"), u_row.get("full_name"))
    return names


@router.post("/{invoice_id}/mark-paid")
async def mark_invoice_person_paid(
    invoice_id: str,
    body: MarkPaidRequest,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Admin-only: Mark or unmark a specific person's lunch debt as PAID in an invoice."""
    names = await _mark_target_names(body.user_id, body.user_name)

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
    names = await _mark_target_names(body.user_id, body.user_name)

    updated_count = 0
    for inv_id in body.invoice_ids:
        if await sheets_invoices.set_member_paid_status(
            invoice_id=inv_id,
            user_id=body.user_id or "",
            user_names=names,
            is_paid=body.paid,
            payment_id=f"ADMIN_{caller_user_id(auth)}",
            note=body.note or "",
        ):
            updated_count += 1
    return {"ok": True, "updated_count": updated_count, "paid": body.paid}


class UpdateInvoicePayerRequest(BaseModel):
    payer_user_id: str = ""
    payer_name: str = ""


@router.patch("/{invoice_id}/payer")
async def update_invoice_payer(
    invoice_id: str,
    body: UpdateInvoicePayerRequest,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Admin-only: update the designated payer for an existing invoice."""
    success = await sheets_invoices.update_payer(
        invoice_id=invoice_id,
        payer_user_id=body.payer_user_id,
        payer_name=body.payer_name,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice not found or could not update payer",
        )
    return {"ok": True, "invoice_id": invoice_id, "payer_user_id": body.payer_user_id, "payer_name": body.payer_name}
