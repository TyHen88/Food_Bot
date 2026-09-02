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

import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from pydantic import BaseModel

from .. import exchange
from ..invoicing import (
    build_invoice_text,
    clean_item_name,
    generate_and_send_invoice,
    payer_qr_data_uri,
    payer_qr_path,
    price_of,
    send_invoice_message,
)
from ..people import is_same_person, name_variants, norm_name, strip_invisible
from ..sheets import invoices as sheets_invoices
from ..sheets import orders as sheets_orders
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_chat_id, caller_user_id, require_admin, require_member
from .members import user_chats

# Backwards compatibility aliases for module-level functions
_build_invoice_text = build_invoice_text
_clean_item_name = clean_item_name
_price_of = price_of
_payer_qr_path = payer_qr_path
_send_invoice_message = send_invoice_message

router = APIRouter(prefix="/orders", tags=["orders"])
logger = logging.getLogger(__name__)


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


import math

@router.get("")
async def list_orders(
    date_from: Optional[str] = Query(None, alias="from",
                                     description="Inclusive YYYY-MM-DD lower bound on order_date."),
    date_to: Optional[str] = Query(None, alias="to",
                                   description="Inclusive YYYY-MM-DD upper bound on order_date."),
    date: Optional[str] = Query(None, description="Single-day shorthand (YYYY-MM-DD)."),
    chat_id: Optional[str] = Query(None, description="Restrict to one chat's orders."),
    page: Optional[int] = Query(None, ge=1, description="Page number for pagination."),
    page_size: int = Query(10, ge=1, le=100, description="Items per page."),
    user_id: Optional[str] = Query(None, description="Filter orders containing this user_id."),
    my_only: bool = Query(False, description="Filter orders containing the caller."),
    search: Optional[str] = Query(None, description="Search term for dish name, chat title, or question."),
    auth: dict = Depends(require_member),
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Orders in a date range, annotated for the calendar. Supports pagination, search, and user filtering."""
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
                if page is not None:
                    return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
                return []
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() == wanted]
    elif not auth.get("is_admin"):
        my_chats = await user_chats(caller_user_id(auth))
        rows = [r for r in rows if str(r.get("chat_id", "")).strip() in my_chats]
    if not rows:
        if page is not None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
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

    # Filter for specific user if requested
    target_user_id = caller_user_id(auth) if my_only else (str(user_id).strip() if user_id else None)
    if target_user_id:
        target_names = set()
        if is_configured():
            u_row = await repo.find_by_pk("user", target_user_id)
            if u_row:
                target_names = name_variants(
                    username=u_row.get("username") or "",
                    full_name=u_row.get("full_name") or "",
                )
        if not target_names and (my_only or target_user_id == caller_user_id(auth)):
            u_dict = auth.get("user") or {}
            target_names = name_variants(
                username=u_dict.get("username") or "",
                first_name=u_dict.get("first_name") or "",
                last_name=u_dict.get("last_name") or "",
            )

        def has_user(ord_dict: Dict[str, Any]) -> bool:
            if str(ord_dict.get("paid_by", {}).get("user_id", "")).strip() == target_user_id:
                return True
            for it in ord_dict.get("items", []):
                if is_same_person(it.get("user_id"), it.get("name"), target_user_id, target_names):
                    return True
            return False
        out = [ord_item for ord_item in out if has_user(ord_item)]

    # Search filter
    if search and search.strip():
        q = search.strip().lower()
        def matches_search(ord_dict: Dict[str, Any]) -> bool:
            if q in (ord_dict.get("chat_title") or "").lower():
                return True
            if q in (ord_dict.get("question") or "").lower():
                return True
            if q in (ord_dict.get("paid_by", {}).get("username") or "").lower():
                return True
            for it in ord_dict.get("items", []):
                if q in (it.get("item_name") or it.get("name") or "").lower():
                    return True
            return False
        out = [ord_item for ord_item in out if matches_search(ord_item)]

    # Sort order: if paginating, default to newest first; otherwise chronological (calendar view)
    if page is not None:
        out.sort(key=lambda r: (r.get("order_date") or "", r.get("created_at") or ""), reverse=True)
        total_orders = len(out)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_items = out[start_idx:end_idx]
        total_pages = math.ceil(total_orders / page_size) if total_orders > 0 else 1
        return {
            "items": paged_items,
            "total": total_orders,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

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


_LIST_MARKER = re.compile(r"^[\s\-•*·]+")


def _clean_item_name(name: Any) -> str:
    """Strip leading list markers ("- dish", "• dish") that menu text often
    carries into the item JSON — the invoice adds its own bullets.

    Zero-width characters go too (`strip_invisible`): menu text pasted into
    Telegram carries them, and an invisible character is enough to make the
    stored dish name miss its entry in the admin's price list, which used to
    silently price that dish at $0.00 — see `_price_of`.
    """
    return _LIST_MARKER.sub("", strip_invisible(name)).strip()


def _price_of(raw_name: str, item_name: str,
              price_map: Dict[str, float]) -> Optional[float]:
    """The price for one dish, most-specific key first: the raw stored name,
    the marker-stripped name, then the fully normalized one.

    Returns None — never 0.0 — when the dish has no price at all, so callers
    can tell "this dish is free" apart from "nobody priced this dish".
    """
    for key in (raw_name, item_name, norm_name(item_name)):
        if key in price_map:
            return price_map[key]
    return None


_ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"


def payer_qr_path(qr_filename: Any) -> Optional[Path]:
    """Resolve a payer row's qr_filename inside assets/, or None when unset
    or missing. Supports .jpg, .png, .jpeg, .webp extensions seamlessly."""
    name = Path(str(qr_filename or "").strip()).name
    if not name:
        return None
    stem = Path(name).stem
    candidates = [
        name,
        f"{stem}.jpg",
        f"{stem}.png",
        f"{stem}.jpeg",
        f"{stem}.webp",
    ]
    for c in candidates:
        path = _ASSETS_DIR / c
        if path.is_file():
            return path
    return None


# Telegram caps photo captions at 1024 chars (messages get 4096).
_CAPTION_LIMIT = 1024


def _payer_qr_source(payer: Optional[Dict[str, Any]]) -> Union[Path, str, None]:
    """What to feed send_photo for this payer's QR: an assets/ Path, a
    Telegram file_id string (from the Settings upload), or None."""
    value = str((payer or {}).get("qr_filename") or "").strip()
    return payer_qr_path(value) or (value or None)


async def payer_qr_data_uri(bot, qr_value: Any) -> str:
    """The payer's QR as a data URI for the Mini App: reads assets/ files
    directly, downloads Telegram file_ids via the bot. "" when unset or
    unreadable."""
    value = str(qr_value or "").strip()
    if not value:
        return ""
    path = payer_qr_path(value)
    if path:
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning(f"Payer QR asset not readable: {path}")
            return ""
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    try:
        tg_file = await bot.get_file(value)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.warning(f"Payer QR file_id not downloadable: {value[:24]}...")
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


async def send_invoice_message(bot, chat_id: int, text: str,
                               payer: Optional[Dict[str, Any]]) -> None:
    """Send an invoice to the group chat, attaching the payer's QR image
    when their payer row has one (assets/ filename or uploaded Telegram
    file_id). Short invoices become a single photo with the invoice as
    caption; longer ones are sent as text followed by the QR photo so
    nothing gets truncated. A broken QR never blocks the invoice text."""
    qr = _payer_qr_source(payer)

    async def _send_qr(caption: str, parse_mode: Optional[str] = None) -> None:
        if isinstance(qr, Path):
            with qr.open("rb") as fh:
                await bot.send_photo(chat_id=chat_id, photo=fh,
                                     caption=caption, parse_mode=parse_mode)
        else:
            await bot.send_photo(chat_id=chat_id, photo=qr,
                                 caption=caption, parse_mode=parse_mode)

    if qr and len(text) <= _CAPTION_LIMIT:
        try:
            await _send_qr(text, "HTML")
            return
        except Exception:
            logger.warning("QR-caption send failed; falling back to text",
                           exc_info=True)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    if qr:
        payer_name = str((payer or {}).get("full_name") or "").strip()
        try:
            await _send_qr(f"💳 Scan KHQR to pay {payer_name}".strip())
        except Exception:
            logger.warning("QR photo attach failed (invoice text was sent)",
                           exc_info=True)


class InvoiceItemPrice(BaseModel):
    item_name: str
    price: float


class InvoiceBody(BaseModel):
    prices: List[InvoiceItemPrice]
    # Which currencies the sent invoice shows. Prices above are ALWAYS in USD
    # — when the admin types riel, the Mini App converts before sending, so
    # one stored amount means the same thing regardless of how it was keyed.
    # Empty/unknown falls back to dollars (see exchange.normalize_currencies).
    currencies: List[str] = ["USD"]


def _build_invoice_text(
    order_date: str,
    user_orders: Dict[str, List[Dict[str, Any]]],
    payer_name: str,
    khqr_text: str = "",
    rate: Optional[Dict[str, Any]] = None,
    currencies: Any = None,
) -> str:
    """Render the invoice message (Telegram HTML).

    `rate` is the NBC exchange-rate row pinned to this invoice; `currencies`
    selects what the reader sees — dollars, riel, or both. Each person's
    total is converted and rounded individually (see exchange.to_khr) so what
    a member is asked for is a payable amount — which means the riel figures
    do not necessarily add up to the riel grand total, and that is deliberate.

    Item lines follow the same selection, EXCEPT that a riel-only invoice
    still shows per-dish prices in riel only; nothing is ever silently
    dropped, and without a rate everything falls back to dollars.

    Format (currencies = USD + KHR):
        🧾 Order Invoice | 2026-07-15
        🏦 NBC 2026-07-15 · 4,047 KHR / USD

        ▪️ Name
        • dish ×1   $1.75 (7,100៛)
        Subtotal   $3.50 (14,200៛)   (only when the person has 2+ items)

        ══════════════════════
        💰 Total Due   $14.00 (56,700៛)
        💳 Pay to Name
        ══════════════════════
    """
    from html import escape

    usd_khr = float((rate or {}).get("usd_khr") or 0) or None
    wanted = exchange.normalize_currencies(currencies)

    def money(amount: float) -> str:
        return exchange.format_money(amount, usd_khr, wanted)

    sep = "══════════════════════"
    lines = [f"🧾 <b>Order Invoice</b> | {escape(order_date)}"]
    # The rate line only earns its place when riel is actually shown.
    if usd_khr and "KHR" in wanted:
        lines.append(
            f"🏦 <i>NBC {escape(str((rate or {}).get('rate_date', '')))} · "
            f"{escape(exchange.format_rate(usd_khr))}</i>"
        )
    lines.append("")

    grand_total = 0.0
    for user_name, user_items in user_orders.items():
        user_total = sum(i["cost"] for i in user_items)
        grand_total += user_total

        lines.append(f"▪️ <b>{escape(user_name)}</b>")
        for i in user_items:
            lines.append(
                f"• {escape(str(i['item_name']))} ×{i['qty']}   "
                f"<code>{money(i['cost'])}</code>"
            )
        # One person, one dish: their line already IS their total, so only
        # repeat it as a subtotal when there is more than one dish.
        if len(user_items) > 1:
            lines.append(f"<b>Subtotal</b>   <code>{money(user_total)}</code>")
        lines.append("")

    lines.append(sep)
    lines.append(f"💰 <b>Total Due</b>   <code>{money(grand_total)}</code>")
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

    # Extract custom price map
    custom_prices = {p.item_name: p.price for p in body.prices}
    currencies = exchange.normalize_currencies(body.currencies)

    rate = await exchange.rate_for(str(row.get("order_date") or ""))
    if "KHR" in currencies and not rate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No exchange rate available yet, so riel amounts can't be "
                   "calculated. Send in USD, or try again once the daily rate "
                   "has been fetched.",
        )

    try:
        application = request.app.state.application
        user_id = (auth.get("user") or {}).get("id")
        result = await generate_and_send_invoice(
            bot=application.bot,
            order_id=order_id,
            custom_prices=custom_prices,
            currencies=currencies,
            sent_by=user_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to generate and send invoice: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to send invoice to group chat: {str(e)}"
        )

