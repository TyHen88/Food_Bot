"""
Central Invoicing Engine for Telegram Food Poll Bot.

Generates and dispatches invoices to Telegram group chats and persists them
to Google Sheets (`invoice` tab). Used by:
- Manual invoice generation API (/api/orders/{id}/invoice)
- Bot commands (/gen_invoice)
- Scheduled cutoff snapshots
"""

import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from telegram import Bot

from . import exchange
from .people import name_variants, norm_name, strip_invisible
from .sheets import events as sheets_events
from .sheets import invoices as sheets_invoices
from .sheets import orders as sheets_orders
from .sheets import payers as sheets_payers
from .sheets import repo
from .sheets.invoices import PAYER_SELF, detail_payment_state

logger = logging.getLogger(__name__)

# Leading list markers (bullet, dash, star, numbered "1.", Khmer numerals)
_LIST_MARKER = re.compile(r"^[\s\-•*·\d១-១០]+[.)\s]*", re.UNICODE)
_ASSETS_DIR = Path(__file__).parent.parent / "assets"
_CAPTION_LIMIT = 1024


def clean_item_name(name: str) -> str:
    """Strip list markers and whitespace/zero-width chars from dish names."""
    return _LIST_MARKER.sub("", strip_invisible(name)).strip()


def price_of(
    raw_name: str, item_name: str, price_map: Dict[str, float]
) -> Optional[float]:
    """Look up price for a dish (most specific key first)."""
    for key in (raw_name, item_name, norm_name(item_name)):
        if key in price_map:
            return price_map[key]
    return None


def payer_qr_path(qr_filename: Any) -> Optional[Path]:
    """Resolve a payer row's qr_filename inside assets/, or None."""
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


def payer_qr_source(payer: Optional[Dict[str, Any]]) -> Union[Path, str, None]:
    """Determine send_photo source for QR image (Path or Telegram file_id)."""
    value = str((payer or {}).get("qr_filename") or "").strip()
    return payer_qr_path(value) or (value or None)


async def payer_qr_data_uri(bot: Any, qr_value: Any) -> str:
    """The payer's QR as a data URI for the Mini App."""
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
    if bot:
        try:
            tg_file = await bot.get_file(value)
            data = bytes(await tg_file.download_as_bytearray())
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except Exception:
            logger.warning(f"Payer QR file_id not downloadable: {value[:24]}...")
    return ""


async def send_invoice_message(
    bot: Bot,
    chat_id: int,
    text: str,
    payer: Optional[Dict[str, Any]],
) -> None:
    """Send an invoice message to the Telegram group chat with KHQR photo if available."""
    qr = payer_qr_source(payer)

    async def _send_qr(caption: str, parse_mode: Optional[str] = None) -> None:
        if isinstance(qr, Path):
            with qr.open("rb") as fh:
                await bot.send_photo(
                    chat_id=chat_id, photo=fh, caption=caption, parse_mode=parse_mode
                )
        else:
            await bot.send_photo(
                chat_id=chat_id, photo=qr, caption=caption, parse_mode=parse_mode
            )

    if qr and len(text) <= _CAPTION_LIMIT:
        try:
            await _send_qr(text, "HTML")
            return
        except Exception:
            logger.warning("QR-caption send failed; falling back to text", exc_info=True)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    if qr:
        payer_name = str((payer or {}).get("full_name") or "").strip()
        try:
            await _send_qr(f"💳 Scan KHQR to pay {payer_name}".strip())
        except Exception:
            logger.warning("QR photo attach failed (invoice text was sent)", exc_info=True)


def build_invoice_text(
    order_date: str,
    user_orders: Dict[str, List[Dict[str, Any]]],
    payer_name: str,
    khqr_text: str = "",
    rate: Optional[Dict[str, Any]] = None,
    currencies: Any = None,
    payer_user_id: Optional[str] = None,
    payer_names: Optional[Set[str]] = None,
) -> str:
    """Render the invoice message in Telegram HTML format."""
    from html import escape

    usd_khr = float((rate or {}).get("usd_khr") or 0) or None
    wanted = exchange.normalize_currencies(currencies)

    def money(amount: float) -> str:
        return exchange.format_money(amount, usd_khr, wanted)

    payer_variants = set()
    if payer_names:
        payer_variants.update(payer_names)
    if payer_name:
        payer_variants.update(name_variants(full_name=payer_name, username=payer_name))

    sep = "══════════════════════"
    lines = [f"🧾 <b>Order Invoice</b> | {escape(order_date)}"]
    if usd_khr and "KHR" in wanted:
        lines.append(
            f"🏦 <i>NBC {escape(str((rate or {}).get('rate_date', '')))} · "
            f"{escape(exchange.format_rate(usd_khr))}</i>"
        )
    lines.append("")

    grand_total = 0.0
    payer_subtotal = 0.0

    for user_name, user_items in user_orders.items():
        user_total = sum(i["cost"] for i in user_items)
        grand_total += user_total

        u_id = next((str(i.get("user_id") or "").strip() for i in user_items if i.get("user_id")), "")
        if u_id and payer_user_id:
            is_payer = u_id == str(payer_user_id).strip()
        else:
            # Exact name only — first-name variants mislabel a namesake as Payer
            is_payer = bool(payer_variants) and norm_name(user_name) == norm_name(payer_name)
        if is_payer:
            payer_subtotal += user_total

        name_display = escape(user_name)
        if is_payer:
            name_display += " <i>(Payer)</i>"

        lines.append(f"▪️ <b>{name_display}</b>")
        for i in user_items:
            lines.append(
                f"• {escape(str(i['item_name']))} ×{i['qty']}   "
                f"<code>{money(i['cost'])}</code>"
            )
        if len(user_items) > 1:
            lines.append(f"<b>Subtotal</b>   <code>{money(user_total)}</code>")
        lines.append("")

    lines.append(sep)
    amount_due = max(0.0, round(grand_total - payer_subtotal, 2))
    if payer_subtotal > 0.009:
        lines.append(f"🧾 <b>Order Total</b>   <code>{money(grand_total)}</code>")
        lines.append(f"💰 <b>Total Due</b>     <code>{money(amount_due)}</code>")
    else:
        lines.append(f"💰 <b>Total Due</b>   <code>{money(grand_total)}</code>")
    lines.append(f"💳 <b>Pay to</b> {escape(payer_name)}")
    lines.append(sep)

    if khqr_text:
        lines.append("")
        lines.append("🔗 <b>Scan KHQR to Pay:</b>")
        lines.append(f"<code>{escape(khqr_text)}</code>")

    return "\n".join(lines)


async def generate_and_send_invoice(
    bot: Bot,
    order_id: str,
    chat_id: Optional[Union[int, str]] = None,
    price_per_item: float = 1.75,
    custom_prices: Optional[Dict[str, float]] = None,
    currencies: Optional[List[str]] = None,
    sent_by: Optional[Union[int, str]] = None,
    payer_user_id: Optional[str] = None,
    payer_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core function to calculate, send, and persist an order invoice.

    - If custom_prices is given, items are priced accordingly; otherwise,
      each item gets price_per_item (default $1.75).
    - Fetches the pinned/daily exchange rate and payer KHQR details.
    - Sends message to the group chat.
    - Persists the record to the `invoice` tab in Google Sheets.
    """
    row = await sheets_orders.get_by_poll(str(order_id))
    if not row:
        raise ValueError(f"Order not found for ID '{order_id}'")

    raw_items = row.get("item") or "[]"
    if isinstance(raw_items, str):
        try:
            items = json.loads(raw_items)
        except Exception:
            items = []
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        items = []

    if not items:
        raise ValueError(f"Order '{order_id}' has no items")

    # Build price lookup map
    price_map: Dict[str, float] = {}
    if custom_prices:
        for name, price in custom_prices.items():
            price_map[name] = price
            price_map.setdefault(clean_item_name(name), price)
            price_map.setdefault(norm_name(name), price)

    unpriced: set = set()
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    display_names: Dict[str, str] = {}
    uid_of_key: Dict[str, str] = {}

    for it in items:
        uid = str(it.get("user_id") or "").strip()
        user_name = str(it.get("name") or "Guest").strip() or "Guest"
        key = uid or user_name
        display_names[key] = user_name
        uid_of_key[key] = uid

        raw_name = str(it.get("item_name") or "Unknown")
        item_name = clean_item_name(raw_name) or "Unknown"
        qty = int(it.get("qty") or 1)

        if custom_prices:
            price = price_of(raw_name, item_name, price_map)
            if price is None:
                unpriced.add(item_name)
                price = 0.0
        else:
            price = float(price_per_item)

        slot = grouped.setdefault(key, {}).setdefault(item_name, {
            "item_name": item_name, "qty": 0, "price": price, "cost": 0.0, "user_id": uid,
        })
        slot["qty"] += qty
        slot["cost"] = round(slot["price"] * slot["qty"], 2)

    user_orders: Dict[str, List[Dict[str, Any]]] = {}
    for key, dishes in grouped.items():
        user_orders.setdefault(display_names[key], []).extend(dishes.values())

    existing_inv = await sheets_invoices.get(str(order_id))

    # Determine payer information: explicit args > existing invoice > order clicker
    payer_id = payer_user_id
    payer_full_name = payer_name or ""

    if not payer_id and existing_inv and existing_inv.get("payer_user_id"):
        payer_id = existing_inv.get("payer_user_id")
        if not payer_full_name:
            payer_full_name = existing_inv.get("payer_name") or ""

    if not payer_id:
        payer_id = row.get("user_id")
    if not payer_full_name:
        payer_full_name = row.get("username") or "the Payer"

    khqr_text = ""
    payer_row = None
    if payer_id:
        payer_row = await sheets_payers.get(str(payer_id))
        if payer_row:
            khqr_text = str(payer_row.get("khqr_text") or "")
            payer_full_name = str(payer_row.get("full_name") or payer_full_name)

    is_generic_name = (payer_full_name or "").strip().lower() in ("the payer", "payer", "admin", "unknown", "guest")
    # The order row's username belongs to whoever pressed Order — only a
    # payer name when that person IS the payer.
    clicker_is_payer = str(payer_id or "").strip() == str(row.get("user_id") or "").strip()
    payer_names = set() if is_generic_name else name_variants(
        username=(row.get("username") or "") if clicker_is_payer else "",
        full_name=payer_full_name,
    )

    existing_inv = await sheets_invoices.get(str(order_id))
    order_date = str(row.get("order_date") or "")
    rate = None
    if existing_inv and existing_inv.get("usd_khr_rate"):
        rate = {
            "usd_khr": existing_inv["usd_khr_rate"],
            "rate_date": existing_inv.get("rate_date") or "",
        }
    if not rate:
        rate = await exchange.rate_for(order_date)
    if not rate:
        rate = await exchange.current()

    # Default currencies: show KHR if rate is available
    if not currencies:
        currencies = ["USD", "KHR"] if (rate and rate.get("usd_khr")) else ["USD"]
    wanted_currencies = exchange.normalize_currencies(currencies)

    invoice_text = build_invoice_text(
        order_date=order_date,
        user_orders=user_orders,
        payer_name=payer_full_name,
        khqr_text=khqr_text,
        rate=rate,
        currencies=wanted_currencies,
        payer_user_id=str(payer_id or "") if payer_id else None,
        payer_names=payer_names,
    )
    grand_total = round(sum(i["cost"] for items_list in user_orders.values() for i in items_list), 2)

    # Destination chat
    target_chat_id = chat_id or row.get("chat_id")
    if target_chat_id and bot:
        try:
            await send_invoice_message(
                bot, int(target_chat_id), invoice_text, payer_row
            )
            logger.info(f"Invoice sent to chat {target_chat_id} for order {order_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram invoice for order {order_id}: {e}", exc_info=True)
            raise RuntimeError(f"Failed to send invoice to chat {target_chat_id}: {e}")

    # Map existing member payment statuses to preserve them across invoice updates
    existing_paid_map: Dict[str, Dict[str, Any]] = {}
    if existing_inv and existing_inv.get("details"):
        for d in existing_inv["details"]:
            u_id = str(d.get("user_id") or "").strip()
            u_name = str(d.get("user_name") or "").strip()
            if u_id:
                existing_paid_map[u_id] = d
            if u_name:
                existing_paid_map[u_name] = d

    # Persist the invoice
    details = []
    for key, dishes in grouped.items():
        u_id = uid_of_key.get(key, "")
        u_name = display_names[key]
        prev_record = existing_paid_map.get(u_id) or existing_paid_map.get(u_name) or {}

        subtotal = round(sum(i["cost"] for i in dishes.values()), 2)
        payment_id = str(prev_record.get("payment_id") or "").strip()
        paid_at = str(prev_record.get("paid_at") or "").strip()
        paid_note = str(prev_record.get("paid_note") or "").strip()

        # Check if this member is the designated payer of THIS specific order.
        # Name fallback is exact only (no first-name variants).
        is_order_payer = False
        if payer_id and u_id:
            is_order_payer = (str(payer_id).strip() == str(u_id).strip())
        elif not is_generic_name:
            is_order_payer = norm_name(u_name) == norm_name(payer_full_name)

        if is_order_payer:
            # The payer of this specific order fronted the money to the restaurant,
            # so their own dish in THIS order is recorded as paid.
            is_paid = True
            paid_amt = subtotal
            payment_id = PAYER_SELF
            paid_at = repo.now_iso()
            paid_note = ""
        elif payment_id and payment_id != PAYER_SELF:
            # A non-payer diner keeps only a real payment (PayWay trx or admin
            # settle). PAYER_SELF from a previous payer and old auto-paid flags
            # without a payment_id are dropped.
            prev_paid, prev_paid_amt, _ = detail_payment_state(prev_record)
            is_paid = prev_paid and prev_paid_amt >= subtotal - 0.009
            paid_amt = prev_paid_amt
        else:
            # Do not auto-mark non-payer diners as paid without recorded payment
            is_paid = False
            paid_amt = 0.0
            payment_id = ""
            paid_at = ""
            paid_note = ""

        details.append({
            "user_id": u_id,
            "user_name": u_name,
            "items": list(dishes.values()),
            "subtotal": subtotal,
            "paid": is_paid,
            "paid_amount": paid_amt,
            "payment_id": payment_id,
            "paid_at": paid_at,
            "paid_note": paid_note,
        })

    await sheets_invoices.save_sent(
        order_id=str(row.get("order_id") or order_id),
        poll_id=str(row.get("poll_id") or ""),
        chat_id=str(target_chat_id or ""),
        order_date=order_date,
        details=details,
        total=grand_total,
        payer_user_id=str(payer_id or ""),
        payer_name=payer_full_name,
        usd_khr_rate=float((rate or {}).get("usd_khr") or 0),
        rate_date=str((rate or {}).get("rate_date") or ""),
        display_currencies=list(wanted_currencies),
        sent_by=int(sent_by) if sent_by and str(sent_by).isdigit() else None,
    )

    await sheets_events.emit(
        "INVOICE_SENT",
        entity_type="order",
        entity_id=str(order_id),
        chat_id=int(target_chat_id) if target_chat_id and str(target_chat_id).lstrip("-").isdigit() else None,
        user_id=int(sent_by) if sent_by and str(sent_by).isdigit() else None,
        payload={"total": grand_total, "items_count": len(items)},
    )

    return {
        "ok": True,
        "order_id": order_id,
        "total": grand_total,
        "unpriced_items": list(unpriced),
        "details": details,
        "chat_id": target_chat_id,
    }
