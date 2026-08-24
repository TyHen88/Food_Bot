"""
Payment transaction persistence — backed by the `payment` tab.

One row per incoming transaction notification (e.g. from PayWay ABA @PayWayByABA_bot).
`settled_orders` stores the list of settled order/invoice dates and amounts as JSON:
    [{"order_id": "...", "date": "2026-08-24", "amount": 1.5}]
"""

import json
import logging
from typing import Any, Dict, List, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)


def _parse_settled_orders(raw: Any) -> List[Dict[str, Any]]:
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
    """Raw sheet row → typed API shape."""
    try:
        amount = float(row.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    try:
        amount_usd = float(row.get("amount_usd") or 0)
    except (TypeError, ValueError):
        amount_usd = 0.0

    return {
        "payment_id": str(row.get("payment_id", "")),
        "trx_id": str(row.get("trx_id", "")),
        "user_id": str(row.get("user_id", "")),
        "sender_name": str(row.get("sender_name", "")),
        "amount": amount,
        "currency": str(row.get("currency", "USD")),
        "amount_usd": amount_usd,
        "settled_orders": _parse_settled_orders(row.get("settled_orders")),
        "status": str(row.get("status", "MATCHED")),
        "apv": str(row.get("apv", "")),
        "raw_text": str(row.get("raw_text", "")),
        "created_at": str(row.get("created_at", "")),
    }


async def find_by_trx_id(trx_id: str) -> Optional[Dict[str, Any]]:
    """Check if transaction ID was already processed (deduplication)."""
    if not is_configured() or not trx_id:
        return None
    rows = await repo.list_all("payment")
    for r in rows:
        if str(r.get("trx_id", "")).strip() == str(trx_id).strip():
            return shape(r)
    return None


async def create_payment(
    *,
    payment_id: str,
    trx_id: str,
    user_id: str,
    sender_name: str,
    amount: float,
    currency: str,
    amount_usd: float,
    settled_orders: List[Dict[str, Any]],
    status: str = "MATCHED",
    apv: str = "",
    raw_text: str = "",
) -> Dict[str, Any]:
    """Save an incoming payment record."""
    if not is_configured():
        logger.warning("Sheets not configured — payment %s not persisted", payment_id)
        return {
            "payment_id": payment_id,
            "trx_id": trx_id,
            "user_id": user_id,
            "sender_name": sender_name,
            "amount": amount,
            "currency": currency,
            "amount_usd": amount_usd,
            "settled_orders": settled_orders,
            "status": status,
            "apv": apv,
            "raw_text": raw_text,
            "created_at": repo.now_iso(),
        }

    row_data = {
        "payment_id": payment_id,
        "trx_id": trx_id,
        "user_id": str(user_id or ""),
        "sender_name": sender_name,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "amount_usd": f"{amount_usd:.2f}",
        "settled_orders": json.dumps(settled_orders, ensure_ascii=False),
        "status": status,
        "apv": apv,
        "raw_text": raw_text,
        "created_at": repo.now_iso(),
    }
    await repo.create("payment", row_data)
    return shape(row_data)


async def list_all() -> List[Dict[str, Any]]:
    if not is_configured():
        return []
    return [shape(r) for r in await repo.list_all("payment")]


async def list_by_user(user_id: str) -> List[Dict[str, Any]]:
    if not is_configured() or not user_id:
        return []
    uid = str(user_id).strip()
    all_payments = await list_all()
    return [p for p in all_payments if p["user_id"] == uid]
