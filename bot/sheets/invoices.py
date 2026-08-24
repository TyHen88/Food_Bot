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
from typing import Any, Dict, List, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)


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
) -> bool:
    """Mark a specific user's subtotal in an invoice as PAID."""
    if not is_configured():
        return False
    row = await repo.find_by_pk("invoice", str(invoice_id))
    if not row:
        return False

    from ..people import is_same_person
    details = _parse_details(row.get("details"))
    updated = False
    now = repo.now_iso()

    for d in details:
        if is_same_person(d.get("user_id"), d.get("user_name"), user_id, user_names):
            d["paid"] = True
            d["paid_at"] = now
            d["payment_id"] = payment_id
            if paid_amount:
                d["paid_amount"] = paid_amount
            else:
                d["paid_amount"] = float(d.get("subtotal") or 0)
            updated = True

    if updated:
        await repo.update("invoice", str(invoice_id), {
            "details": json.dumps(details, ensure_ascii=False)
        })
    return updated

