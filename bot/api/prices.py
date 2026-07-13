"""
/api/prices — the per-dish price catalog behind invoice amounts.

Each `price` row maps an item_name (the menu/option text) to a number in a
currency (USD or KHR). The invoice page reads these to compute each user's
amount; the invoice then shows both currencies using the live USD/KHR rate
(see bot/exchange.py).

Access model
------------
    GET  — any verified user (require_member): read the catalog.
    PUT  — admin only (require_admin): bulk upsert prices from the invoice
           page (admin fills in / edits several dishes, then saves).
"""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_user_id, require_admin, require_member

router = APIRouter(prefix="/prices", tags=["prices"])

_ALLOWED_CURRENCIES = {"USD", "KHR"}


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw `price` row to the API shape (price coerced to float)."""
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return {
        "item_name": str(row.get("item_name", "")).strip(),
        "price": price,
        "currency": (str(row.get("currency", "")).strip().upper() or "USD"),
        "updated_at": str(row.get("updated_at", "")),
    }


@router.get("")
async def list_prices(
    _: dict = Depends(require_member),
) -> List[Dict[str, Any]]:
    """All active dish prices."""
    if not is_configured():
        return []
    rows = await repo.list_all("price")
    out = [
        _shape(r)
        for r in rows
        if str(r.get("is_active", "TRUE")).strip().upper() != "FALSE"
        and str(r.get("item_name", "")).strip()
    ]
    out.sort(key=lambda r: r["item_name"].lower())
    return out


class PriceIn(BaseModel):
    item_name: str
    # Accept "1.50" or 1.5 from the form; we coerce/validate below.
    price: Union[float, int, str] = 0
    currency: str = "USD"


class PricesBody(BaseModel):
    prices: List[PriceIn]


@router.put("")
async def upsert_prices(
    body: PricesBody,
    auth: dict = Depends(require_admin),
) -> List[Dict[str, Any]]:
    """Bulk insert/update dish prices (keyed on item_name). Admin-only.

    Returns the full active catalog so the invoice page can re-render.
    """
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sheets not configured",
        )

    editor = caller_user_id(auth)
    now = repo.now_iso()
    for p in body.prices:
        name = (p.item_name or "").strip()
        if not name:
            continue
        try:
            price = float(p.price or 0)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid price for '{name}'",
            )
        currency = (p.currency or "USD").strip().upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Currency must be one of {sorted(_ALLOWED_CURRENCIES)}",
            )
        await repo.upsert("price", {
            "item_name": name,
            "price": price,
            "currency": currency,
            "is_active": "TRUE",
            "updated_at": now,
            "updated_by": editor,
        })

    return await list_prices(auth)


async def price_index() -> Dict[str, Dict[str, Any]]:
    """{item_name: {price, currency}} for active rows. Shared with the invoice
    builder so amount computation has a single source of truth."""
    if not is_configured():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for r in await repo.list_all("price"):
        if str(r.get("is_active", "TRUE")).strip().upper() == "FALSE":
            continue
        name = str(r.get("item_name", "")).strip()
        if not name:
            continue
        shaped = _shape(r)
        out[name] = {"price": shaped["price"], "currency": shaped["currency"]}
    return out
