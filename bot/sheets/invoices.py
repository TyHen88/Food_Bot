"""
Invoice persistence — backed by the `invoice` tab.

One row per order (PK invoice_id = order_id). Saving again for the same
order (a re-send, or sending with updated prices) upserts the row and
bumps `sent_count`. `details` stores the per-person breakdown as JSON:

    [{"user_id": "123",          # Telegram id; "" on legacy/guest entries
      "user_name": "...",
      "items": [{"item_name": "...", "qty": 2, "price": 1.5, "cost": 3.0}],
      "subtotal": 3.0}]
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from ..people import norm_name
from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)

# Written on the payer's own detail when an invoice is sent. It is NOT a
# payment: it only counts while that person is still the invoice's payer.
PAYER_SELF = "PAYER_SELF"

_GENERIC_PAYER_NAMES = {"the payer", "payer", "admin", "unknown", "guest", ""}


def _to_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_detail_payer(inv: Dict[str, Any], d: Dict[str, Any]) -> bool:
    """Is invoice detail `d` the designated payer of THIS invoice?

    Ids decide when both sides have one. Otherwise the detail's name must
    equal the payer's name exactly — never the first-name variant from
    `name_variants`, which made "Tii" the payer of "Tii ♏️"'s invoices.
    """
    payer_id = str(inv.get("payer_user_id") or "").strip()
    d_uid = str(d.get("user_id") or "").strip()
    if payer_id and d_uid:
        return payer_id == d_uid
    payer_name = norm_name(inv.get("payer_name"))
    if payer_name in _GENERIC_PAYER_NAMES:
        return False
    return norm_name(d.get("user_name")) == payer_name


def detail_payment_state(d: Dict[str, Any], is_payer: bool = False) -> Tuple[bool, float, float]:
    """(is_paid, paid_amount, due) for one invoice detail — the single rule
    every menu (invoices, day summary, members, settlement) must use.

    A paid flag only counts when it is backed by a real payment_id (PayWay
    trx id or ADMIN_<id> manual settle). Flags without one were written by
    the old auto-paid code, and PAYER_SELF only holds while `is_payer`.
    """
    subtotal = _to_float(d.get("subtotal"))
    if is_payer:
        return True, subtotal, 0.0

    payment_id = str(d.get("payment_id") or "").strip()
    if not payment_id or payment_id == PAYER_SELF:
        return False, 0.0, round(subtotal, 2)

    paid_amt = _to_float(d.get("paid_amount"))
    if bool(d.get("paid")):
        return True, (paid_amt if paid_amt > 0 else subtotal), 0.0

    due = round(max(0.0, subtotal - paid_amt), 2)
    return (due <= 0.009 and subtotal > 0), paid_amt, due


def _detail_matches(d: Dict[str, Any], user_id: str, user_names: Set[str]) -> bool:
    """Write-path matcher, stricter than people.is_same_person: with a
    user_id, entries that carry an id must match it exactly; without one,
    only id-less (guest/legacy) entries can match, so marking a guest
    "Tii" paid can never touch member "Tii ♏️"."""
    euid = str(d.get("user_id") or "").strip()
    target = str(user_id or "").strip()
    if euid:
        return bool(target) and euid == target
    return bool(user_names) and norm_name(d.get("user_name")) in user_names


def _parse_details(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def shape(row: Dict[str, Any]) -> Dict[str, Any]:
    """Raw sheet row → API shape (typed total/sent_count, parsed details)."""
    try:
        total = float(row.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        sent_count = int(row.get("sent_count") or 0)
    except (TypeError, ValueError):
        sent_count = 0
    try:
        usd_khr_rate = float(row.get("usd_khr_rate") or 0)
    except (TypeError, ValueError):
        usd_khr_rate = 0.0
    return {
        "invoice_id": str(row.get("invoice_id", "")),
        "order_id": str(row.get("order_id", "")),
        "poll_id": str(row.get("poll_id", "")),
        "chat_id": str(row.get("chat_id", "")),
        "order_date": str(row.get("order_date", "")),
        "details": _parse_details(row.get("details")),
        "total": total,
        "payer_user_id": str(row.get("payer_user_id", "")),
        "payer_name": str(row.get("payer_name", "")),
        # 0.0 on invoices sent before exchange rates existed — callers show
        # dollars only rather than inventing a conversion after the fact.
        "usd_khr_rate": usd_khr_rate,
        "rate_date": str(row.get("rate_date", "")),
        "display_currencies": [
            c for c in str(row.get("display_currencies", "") or "").split(",") if c
        ] or ["USD"],
        "sent_count": sent_count,
        "last_sent_at": str(row.get("last_sent_at", "")),
        "created_at": str(row.get("created_at", "")),
        "created_by": str(row.get("created_by", "")),
    }


async def save_sent(
    *,
    order_id: str,
    poll_id: str,
    chat_id: str,
    order_date: str,
    details: List[Dict[str, Any]],
    total: float,
    payer_user_id: str,
    payer_name: str,
    usd_khr_rate: float = 0.0,
    rate_date: str = "",
    display_currencies: Optional[List[str]] = None,
    sent_by: Optional[int] = None,
) -> None:
    """Record that an invoice was (re)sent. Upserts by order_id and bumps
    sent_count. No-op when Sheets isn't configured (local dev)."""
    if not is_configured():
        logger.warning("Sheets not configured — invoice for %s not persisted", order_id)
        return

    existing = await repo.find_by_pk("invoice", order_id)
    prev_count = 0
    created_at = repo.now_iso()
    if existing:
        try:
            prev_count = int(existing.get("sent_count") or 0)
        except (TypeError, ValueError):
            prev_count = 0
        created_at = str(existing.get("created_at") or created_at)
        # The rate is pinned at first send. A re-send months later must not
        # silently restate the same invoice at a different exchange rate.
        try:
            prev_rate = float(existing.get("usd_khr_rate") or 0)
        except (TypeError, ValueError):
            prev_rate = 0.0
        if prev_rate:
            usd_khr_rate = prev_rate
            rate_date = str(existing.get("rate_date") or rate_date)

    await repo.upsert_blocking("invoice", {
        "invoice_id": order_id,
        "order_id": order_id,
        "poll_id": poll_id,
        "chat_id": chat_id,
        "order_date": order_date,
        "details": json.dumps(details, ensure_ascii=False),
        "total": f"{total:.2f}",
        "payer_user_id": payer_user_id,
        "payer_name": payer_name,
        "usd_khr_rate": f"{float(usd_khr_rate or 0):.2f}",
        "rate_date": rate_date,
        "display_currencies": ",".join(display_currencies or ["USD"]),
        "sent_count": str(prev_count + 1),
        "last_sent_at": repo.now_iso(),
        "created_at": created_at,
        "created_by": "" if sent_by is None else str(sent_by),
    })


async def get(invoice_id: str) -> Optional[Dict[str, Any]]:
    if not is_configured():
        return None
    row = await repo.find_by_pk("invoice", str(invoice_id))
    return shape(row) if row else None


async def list_all() -> List[Dict[str, Any]]:
    if not is_configured():
        return []
    return [shape(r) for r in await repo.list_all("invoice")]


async def order_ids_with_invoice() -> set:
    """order_ids that have an invoice — used to flag orders in the calendar."""
    if not is_configured():
        return set()
    return {
        str(r.get("order_id", "")).strip()
        for r in await repo.list_all("invoice")
        if str(r.get("order_id", "")).strip()
    }


async def mark_member_paid(
    invoice_id: str,
    user_id: str,
    user_names: set,
    *,
    payment_id: str = "",
    paid_amount: float = 0.0,
    is_fully_paid: bool = True,
) -> bool:
    """Mark a specific user's subtotal in an invoice as PAID or PARTIAL."""
    if not is_configured():
        return False
    row = await repo.find_by_pk("invoice", str(invoice_id))
    if not row:
        return False

    details = _parse_details(row.get("details"))
    updated = False
    now = repo.now_iso()

    for d in details:
        if _detail_matches(d, user_id, user_names):
            subtotal = float(d.get("subtotal") or 0.0)
            # Only carry over an earlier partial payment that was a real one.
            _, prev_paid, _ = detail_payment_state(d)
            if d.get("paid"):
                prev_paid = 0.0
            new_paid_total = round(prev_paid + paid_amount, 2) if paid_amount else subtotal

            d["paid"] = bool(is_fully_paid or (new_paid_total >= (subtotal - 0.05)))
            d["paid_at"] = now
            d["payment_id"] = payment_id
            d["paid_amount"] = new_paid_total
            updated = True

    if updated:
        await repo.update("invoice", str(invoice_id), {
            "details": json.dumps(details, ensure_ascii=False)
        })
    return updated


async def set_member_paid_status(
    invoice_id: str,
    user_id: str,
    user_names: set,
    *,
    is_paid: bool = True,
    payment_id: str = "MANUAL_ADMIN",
    paid_amount: Optional[float] = None,
) -> bool:
    """Explicitly mark or unmark a user's subtotal in an invoice as PAID or UNPAID."""
    if not is_configured():
        return False
    row = await repo.find_by_pk("invoice", str(invoice_id))
    if not row:
        return False

    details = _parse_details(row.get("details"))
    updated = False
    now = repo.now_iso()

    for d in details:
        if _detail_matches(d, user_id, user_names):
            subtotal = float(d.get("subtotal") or 0.0)
            d["paid"] = is_paid
            if is_paid:
                d["paid_at"] = now
                d["payment_id"] = payment_id
                d["paid_amount"] = paid_amount if paid_amount is not None else subtotal
            else:
                d["paid_at"] = ""
                d["payment_id"] = ""
                d["paid_amount"] = 0.0
            updated = True

    if updated:
        await repo.update("invoice", str(invoice_id), {
            "details": json.dumps(details, ensure_ascii=False)
        })
    return updated


async def update_payer(
    invoice_id: str,
    payer_user_id: str,
    payer_name: str,
) -> bool:
    """Update the designated payer on an existing invoice."""
    if not is_configured():
        return False
    row = await repo.find_by_pk("invoice", str(invoice_id))
    if not row:
        return False
    await repo.update("invoice", str(invoice_id), {
        "payer_user_id": str(payer_user_id or "").strip(),
        "payer_name": str(payer_name or "").strip(),
    })
    return True


