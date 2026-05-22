"""
Runtime settings accessor — backed by the `setting` tab.

Lookup order:
    1. Sheets cache (if configured + value present)
    2. SEED_SETTING default for that key (so the bot still works before
       the operator opens the sheet)
    3. Provided `default` argument
    4. Empty string

All accessors are async because the cache fetch may need to refresh.
"""

import logging
from typing import Any, Optional

from .cache import cache
from .client import is_configured
from .schema import SEED_SETTING

logger = logging.getLogger(__name__)

# Build a {key: seed_value} dict once at import time for O(1) fallback lookup.
_SEED_INDEX = {row["key"]: row["value"] for row in SEED_SETTING}


async def get(key: str, default: Optional[str] = None) -> str:
    """Return setting value. Empty string if nothing matches."""
    if is_configured():
        try:
            row = await cache.find("setting", "key", key)
            if row and row.get("value") not in (None, ""):
                return str(row["value"])
        except Exception as e:
            logger.warning(f"Setting cache lookup failed for '{key}': {e}; using fallback")

    seeded = _SEED_INDEX.get(key)
    if seeded:
        return seeded
    return default if default is not None else ""


async def get_int(key: str, default: int = 0) -> int:
    raw = await get(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def get_time(key: str, default: str = "00:00") -> tuple[int, int]:
    """Parse an `HH:MM` setting into (hour, minute). Tolerant of bad input."""
    raw = await get(key, default)
    try:
        hh, mm = raw.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        logger.warning(f"Setting '{key}' is not HH:MM: {raw!r}, using {default}")
        hh, mm = default.split(":")
        return int(hh), int(mm)
