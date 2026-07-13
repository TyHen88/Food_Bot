"""
USD -> KHR exchange rate for invoices.

ABA's published "ABA Buys" rate is the intended reference, but ababank.com is
behind Cloudflare and can't be auto-scraped without a headless browser. So the
rate is auto-fetched from a free, no-key FX API (open.er-api.com) by a daily
scheduler job and cached in the `setting` tab. An admin who wants ABA's exact
buy rate sets USD_KHR_RATE_AUTO=FALSE and pins USD_KHR_RATE manually (via /set
or the Mini App Settings page).

Settings used (all in the `setting` tab):
    USD_KHR_RATE         effective riel-per-USD used on invoices
    USD_KHR_RATE_AUTO    "TRUE" => daily job overwrites USD_KHR_RATE from API
    USD_KHR_RATE_AT      ISO timestamp of last update
    USD_KHR_RATE_SOURCE  API host, or "manual"
"""

import logging
from typing import Optional

import httpx

from .sheets import repo
from .sheets import settings as sheets_settings
from .sheets.client import is_configured

logger = logging.getLogger(__name__)

# Free, no-key FX API. Returns {"result":"success","rates":{"KHR":4023.7,...}}.
_FX_URL = "https://open.er-api.com/v6/latest/USD"
_FETCH_TIMEOUT = 20.0

# Last-ditch fallback if the API fails and nothing is cached yet.
DEFAULT_RATE = 4100.0
# Sanity bounds — the KHR has sat near ~4000/USD for years. Reject a wildly off
# value (API glitch) rather than print a nonsense invoice.
_MIN_RATE, _MAX_RATE = 3500.0, 4500.0


def _plausible(rate: Optional[float]) -> bool:
    return rate is not None and _MIN_RATE <= rate <= _MAX_RATE


async def fetch_live_rate() -> Optional[float]:
    """Fetch riel-per-USD from the FX API. None on failure / implausible value."""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            r = await client.get(_FX_URL)
            r.raise_for_status()
            data = r.json()
        rate = float((data.get("rates") or {}).get("KHR"))
    except Exception as e:
        logger.warning(f"FX rate fetch failed: {e}")
        return None
    if not _plausible(rate):
        logger.warning(f"FX rate {rate} outside [{_MIN_RATE},{_MAX_RATE}] — ignoring")
        return None
    return rate


async def get_rate() -> float:
    """Effective riel-per-USD for invoices. Reads the cached setting; falls
    back to the seeded default. Never raises."""
    raw = await sheets_settings.get("USD_KHR_RATE", str(DEFAULT_RATE))
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        rate = DEFAULT_RATE
    return rate if rate > 0 else DEFAULT_RATE


async def get_rate_info() -> dict:
    """Full rate state for display (rate, when, source, auto flag)."""
    auto = (await sheets_settings.get("USD_KHR_RATE_AUTO", "TRUE")).strip().upper()
    return {
        "rate": await get_rate(),
        "updated_at": await sheets_settings.get("USD_KHR_RATE_AT", ""),
        "source": await sheets_settings.get("USD_KHR_RATE_SOURCE", ""),
        "auto": auto == "TRUE",
    }


async def _set_setting(key: str, value: str) -> None:
    """Upsert one `setting` row, preserving its value_type/description."""
    if not is_configured():
        return
    existing = await repo.find_by_pk("setting", key)
    await repo.upsert("setting", {
        "key": key,
        "value": value,
        "value_type": (existing or {}).get("value_type", "string"),
        "description": (existing or {}).get("description", ""),
        "updated_at": repo.now_iso(),
        "updated_by": "system",
    })


async def refresh_rate(force: bool = False) -> dict:
    """Daily-job entry point. If auto mode is on (or force=True), fetch a fresh
    rate and write USD_KHR_RATE (+ timestamp + source). If auto is off, leave
    the admin's pinned rate untouched. Returns the resulting rate info.
    Never raises — a failed refresh keeps the previous rate."""
    auto = (await sheets_settings.get("USD_KHR_RATE_AUTO", "TRUE")).strip().upper() == "TRUE"
    if not auto and not force:
        logger.info("USD_KHR_RATE_AUTO is FALSE — keeping manually pinned rate.")
        return await get_rate_info()

    rate = await fetch_live_rate()
    if rate is None:
        logger.warning("Rate refresh: fetch failed, keeping previous rate.")
        return await get_rate_info()

    host = _FX_URL.split("/")[2]
    await _set_setting("USD_KHR_RATE", f"{rate:.2f}")
    await _set_setting("USD_KHR_RATE_AT", repo.now_iso())
    await _set_setting("USD_KHR_RATE_SOURCE", host)
    logger.info(f"USD_KHR_RATE refreshed to {rate:.2f} from {host}")
    return await get_rate_info()
