"""
/api/orders — read-only listing of the `order` tab.

Used by the Mini App calendar to show what was ordered on a given day
when the user taps an event in the detail sheet.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ..sheets import orders as sheets_orders
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_admin

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def list_orders(
    date: Optional[str] = Query(None, description="Filter by order_date (YYYY-MM-DD)."),
    _: dict = Depends(require_admin),
) -> List[Dict[str, Any]]:
    if date:
        # Delegate to orders.list_by_date so the in-memory fallback works
        # for local dev (Sheets not configured).
        return await sheets_orders.list_by_date(date)
    if not is_configured():
        return []
    return await repo.list_all("order")
