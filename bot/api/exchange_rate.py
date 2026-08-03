"""
/api/exchange-rate — the National Bank of Cambodia's official USD→KHR rate.

Served from the `exchange_rate` tab, which a scheduler job refreshes once a
day (see bot/exchange.py). Reads never touch nbc.gov.kh, so the Mini App's
invoice screen can show the rate without waiting on an external site.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from .. import exchange
from ..config import KHR_ROUNDING
from .auth import require_admin, require_member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exchange-rate", tags=["exchange-rate"])


def _shape(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {"available": False, "rate_date": None, "usd_khr": None,
                "display": None, "source": None, "khr_rounding": KHR_ROUNDING}
    return {
        "available": True,
        "rate_date": row["rate_date"],
        "usd_khr": row["usd_khr"],
        # Pre-formatted the way NBC states it, so every surface agrees.
        "display": exchange.format_rate(row["usd_khr"]),
        "source": row.get("source", ""),
        "khr_rounding": KHR_ROUNDING,
    }


@router.get("")
async def get_exchange_rate(
    order_date: Optional[str] = Query(
        None,
        description="Quote the rate in force on this YYYY-MM-DD instead of "
                    "the latest one (used when invoicing a past order).",
    ),
    auth: dict = Depends(require_member),
) -> Dict[str, Any]:
    """The rate in force now, or on `order_date`.

    `stale` is true when the newest stored rate is older than the configured
    threshold — the UI should say so rather than presenting an old rate as
    current. Note NBC does not publish at weekends or on public holidays, so
    a rate_date a few days back is normal, not an error.
    """
    row = await exchange.rate_for(order_date) if order_date else await exchange.current()
    out = _shape(row)
    out["stale"] = await exchange.is_stale()
    out["today"] = exchange.today().isoformat()
    return out


@router.get("/history")
async def list_exchange_rates(
    limit: int = Query(30, ge=1, le=365),
    auth: dict = Depends(require_admin),
) -> List[Dict[str, Any]]:
    """Recent published rates, newest first. Admin-only — it's a diagnostic
    view for checking the daily fetch is actually running."""
    return (await exchange.list_rates())[:limit]
