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

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from pydantic import BaseModel

from ..sheets import invoices as sheets_invoices
from ..sheets import orders as sheets_orders
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_chat_id, caller_user_id, require_admin, require_member
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

    # Scope to the launch chat: explicit ?chat_id, else the chat baked into
    # the signed initData (attachment-menu `chat` or startapp start_param).
    auth_chat = caller_chat_id(auth)
    if not chat_id:
        chat_id = auth_chat

    rows = await sheets_orders.list_in_range(date_from, date_to)
    if chat_id:
        wanted = str(chat_id).strip()
        # Members may only request chats they belong to (the signed launch
        # chat always qualifies); anything else returns nothing rather than
        # leaking another group's orders.
        if not auth.get("is_admin") and wanted != auth_chat:
            if wanted not in await user_chats(caller_user_id(auth)):
                return []
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() == wanted]
    elif not auth.get("is_admin"):
        my_chats = await user_chats(caller_user_id(auth))
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() in my_chats]
    if not rows:
        return []

    titles = await _chat_titles()
    polls = await _poll_meta()
    users = await _user_names()
    invoiced = await sheets_invoices.order_ids_with_invoice()

    out = [
        {
            **_shape_order(row, titles=titles, polls=polls, users=users),
            "has_invoice": str(row.get("order_id", "")).strip() in invoiced,
        }
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


def _clean_item_name(name: Any) -> str:
    """Strip leading list markers ("- dish", "• dish") that menu text often
    carries into the item JSON — the invoice adds its own bullets."""
    import re
    return re.sub(r"^[\s\-•*·]+", "", str(name or "")).strip()


class InvoiceItemPrice(BaseModel):
    item_name: str
    price: float


class InvoiceBody(BaseModel):
    prices: List[InvoiceItemPrice]


def _build_invoice_text(
    order_date: str,
    user_orders: Dict[str, List[Dict[str, Any]]],
    payer_name: str,
    khqr_text: str = "",
) -> str:
    """Render the invoice message (Telegram HTML).

    Format:
        🧾 Order Invoice | 2026-07-15

        ▪️ Name
        • dish ×1   $1.75
        Subtotal   $3.50        (only when the person has 2+ items)

        ══════════════════════
        💰 Total Due   $14.00
        💳 Pay to Name
        ══════════════════════
    """
    from html import escape

    sep = "══════════════════════"
    lines = [f"🧾 <b>Order Invoice</b> | {escape(order_date)}", ""]

    grand_total = 0.0
    for user_name, user_items in user_orders.items():
        user_total = sum(i["cost"] for i in user_items)
        grand_total += user_total

        lines.append(f"▪️ <b>{escape(user_name)}</b>")
        for i in user_items:
            lines.append(
                f"• {escape(str(i['item_name']))} ×{i['qty']}   "
                f"<code>${i['cost']:.2f}</code>"
            )
        if len(user_items) > 1:
            lines.append(f"<b>Subtotal</b>   <code>${user_total:.2f}</code>")
        lines.append("")

    lines.append(sep)
    lines.append(f"💰 <b>Total Due</b>   <code>${grand_total:.2f}</code>")
    lines.append(f"💳 <b>Pay to</b> {escape(payer_name)}")
    lines.append(sep)

    if khqr_text:
        lines.append("")
        lines.append("🔗 <b>Scan KHQR to Pay:</b>")
        lines.append(f"<code>{escape(khqr_text)}</code>")

    return "\n".join(lines)


@router.post("/{order_id}/invoice")
async def generate_order_invoice(
    order_id: str,
    body: InvoiceBody,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Calculate the invoice based on admin-provided item prices, and send a

    formatted summary directly to the Telegram group chat.
    """
    row = await sheets_orders.get_by_poll(order_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )

    items = _parse_items(row.get("item"))
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No items in this order"
        )

    # Price lookup tolerant of leading list markers: match the raw stored
    # name first, then the cleaned one, so "- dish" and "dish" both resolve
    # instead of silently falling back to $0.00.
    price_map: Dict[str, float] = {}
    for p in body.prices:
        price_map[p.item_name] = p.price
        price_map.setdefault(_clean_item_name(p.item_name), p.price)

    # Group by Telegram user id (fallback: display name) so a member whose
    # display name changed between votes ("Tii" vs "Tii ♏️") gets ONE
    # section; merge duplicate dishes within a person, summing quantities.
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    display_names: Dict[str, str] = {}
    for it in items:
        uid = str(it.get("user_id") or "").strip()
        user_name = str(it.get("name") or "Guest").strip() or "Guest"
        key = uid or user_name
        display_names[key] = user_name  # newest name wins

        raw_name = str(it.get("item_name") or "Unknown")
        item_name = _clean_item_name(raw_name) or "Unknown"
        qty = int(it.get("qty") or 1)
        price = price_map.get(raw_name, price_map.get(item_name, 0.0))

        slot = grouped.setdefault(key, {}).setdefault(item_name, {
            "item_name": item_name, "qty": 0, "price": price, "cost": 0.0,
        })
        slot["qty"] += qty
        slot["cost"] = slot["price"] * slot["qty"]

    user_orders: Dict[str, List[Dict[str, Any]]] = {}
    for key, dishes in grouped.items():
        user_orders.setdefault(display_names[key], []).extend(dishes.values())

    # Fetch Payer's KHQR info (if exists)
    payer_id = row.get("user_id")
    khqr_text = ""
    payer_full_name = row.get("username") or "the Payer"
    if payer_id:
        from ..sheets import payers as sheets_payers
        payer_info = await sheets_payers.get(payer_id)
        if payer_info:
            khqr_text = payer_info.get("khqr_text") or ""
            payer_full_name = payer_info.get("full_name") or payer_full_name

    invoice_text = _build_invoice_text(
        str(row.get("order_date") or ""),
        user_orders,
        payer_full_name,
        khqr_text,
    )
    grand_total = sum(i["cost"] for items in user_orders.values() for i in items)

    # Send message to Telegram Chat
    chat_id = row.get("chat_id")
    if chat_id:
        try:
            import logging
            bot_logger = logging.getLogger("bot.api.orders")
            application = request.app.state.application
            await application.bot.send_message(
                chat_id=int(chat_id),
                text=invoice_text,
                parse_mode="HTML"
            )
        except Exception as e:
            bot_logger.error(f"Failed to send Telegram invoice: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to send invoice to group chat: {str(e)}"
            )

    # Persist the invoice so it shows up in the Mini App's Invoices page
    # (and the order flips to "View Invoice"). Failure to save must not
    # undo a successful send — log and keep going.
    details = [
        {
            "user_name": user_name,
            "items": user_items,
            "subtotal": round(sum(i["cost"] for i in user_items), 2),
        }
        for user_name, user_items in user_orders.items()
    ]
    try:
        await sheets_invoices.save_sent(
            order_id=str(row.get("order_id") or order_id),
            poll_id=str(row.get("poll_id") or ""),
            chat_id=str(chat_id or ""),
            order_date=str(row.get("order_date") or ""),
            details=details,
            total=grand_total,
            payer_user_id=str(payer_id or ""),
            payer_name=payer_full_name,
            sent_by=(auth.get("user") or {}).get("id"),
        )
    except Exception as e:
        import logging
        logging.getLogger("bot.api.orders").error(
            f"Invoice sent but saving to the invoice sheet failed: {e}", exc_info=True
        )

    return {"ok": True, "total": grand_total}

