"""
Payer registry — the `payer` tab.

Every time someone taps the Order button they take ownership of that
order (they're the one paying / collecting). We upsert a row per such
person here so the Mini App's Settings → Paid list can show who pays and
let an admin attach a KHQR string or QR image filename to each of them.

One row per user_id; `times_paid` accumulates, `last_paid_at` tracks the
most recent click. In-memory fallback mirrors orders.py for local dev.
"""

import logging
from typing import Any, Dict, List, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)

# Fallback for local dev: {user_id (str): payer_row}
_mem_payers: Dict[str, Dict[str, Any]] = {}


async def record_payment(
    user_id: Optional[int],
    *,
    username: str = "",
    full_name: str = "",
) -> Optional[Dict[str, Any]]:
    """Upsert the payer row for `user_id`, bumping their payment count."""
    if not user_id:
        return None
    uid = str(user_id)
    now = repo.now_iso()

    existing = await get(uid)
    times = 1
    qr_filename = ""
    khqr_text = ""
    created_at = now
    if existing:
        try:
            times = int(existing.get("times_paid") or 0) + 1
        except (TypeError, ValueError):
            times = 1
        qr_filename = existing.get("qr_filename", "") or ""
        khqr_text = existing.get("khqr_text", "") or ""
        created_at = existing.get("created_at") or now

    row = {
        "user_id": uid,
        "username": username or (existing or {}).get("username", ""),
        "full_name": full_name or (existing or {}).get("full_name", ""),
        "qr_filename": qr_filename,
        "khqr_text": khqr_text,
        "times_paid": times,
        "last_paid_at": now,
        "created_at": created_at,
    }

    if is_configured():
        await repo.upsert("payer", row)
    else:
        _mem_payers[uid] = row
    return row


async def get(user_id: Any) -> Optional[Dict[str, Any]]:
    if is_configured():
        return await repo.find_by_pk("payer", str(user_id))
    return _mem_payers.get(str(user_id))


async def list_all() -> List[Dict[str, Any]]:
    if is_configured():
        return await repo.list_all("payer")
    return list(_mem_payers.values())
