"""
Per-tab in-memory cache for Google Sheets.

Why:
    Each Sheets API call is ~300 ms — too slow for the message-handling
    hot path. We read all 10 tabs into memory and refresh every 60 s,
    so reads become O(1) dict lookups.

Consistency model:
    Eventual. Writes go through repo.create/update/delete which update
    both Sheets and the cache, but if a human edits the sheet directly
    the bot will see it within ~60 s.

Tab rows are stored as a list of dicts keyed by the header row, so
column reordering in the sheet does not break code that reads by name.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from .client import get_worksheet, run_sync, with_retry
from .schema import TABS

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60


class SheetCache:
    """In-process cache; one instance per process (see module-level `cache`)."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl = ttl_seconds
        # tab_name -> (rows, fetched_at_monotonic)
        self._data: Dict[str, tuple[List[Dict[str, Any]], float]] = {}
        # tab_name -> last time get_all() was called for it. The refresh loop
        # only re-fetches recently-read tabs, so idle tabs (history,
        # common_code, ...) stop burning the 60 reads/min Sheets quota.
        self._last_access: Dict[str, float] = {}
        self._locks = {tab: asyncio.Lock() for tab in TABS}
        self._refresh_task: Optional[asyncio.Task] = None

    # ---- read path ------------------------------------------------------

    async def get_all(self, tab: str) -> List[Dict[str, Any]]:
        """Return all rows in `tab` as dicts. Refresh if stale or absent."""
        cached = self._data.get(tab)
        now = time.monotonic()
        self._last_access[tab] = now

        if cached is not None:
            # If background refresh is active, trust it to keep data fresh —
            # but a tab the loop skipped as idle may be very stale on its
            # first read after a quiet spell; re-fetch it then.
            if self._refresh_task and not self._refresh_task.done():
                if (now - cached[1]) < 3 * self.ttl:
                    return cached[0]
            # Fallback for when no loop is running
            elif (now - cached[1]) < self.ttl:
                return cached[0]

        await self._refresh_tab(tab)
        return self._data[tab][0]

    async def find(
        self, tab: str, field: str, value: Any
    ) -> Optional[Dict[str, Any]]:
        """First row in `tab` where row[field] == value (string-compared)."""
        rows = await self.get_all(tab)
        target = _stringify(value)
        for row in rows:
            if _stringify(row.get(field)) == target:
                return row
        return None

    async def filter(
        self, tab: str, predicate
    ) -> List[Dict[str, Any]]:
        """Return all rows for which predicate(row) is truthy."""
        rows = await self.get_all(tab)
        return [r for r in rows if predicate(r)]

    # ---- write path (called by repo) -----------------------------------

    def upsert_local(self, tab: str, pk_field: str, row: Dict[str, Any]) -> None:
        """
        Mirror a single-row create/update into the cache so subsequent reads
        see the change before the next refresh. Idempotent on PK.
        """
        cached = self._data.get(tab)
        if cached is None:
            return  # cache will populate on first read
        rows, ts = cached
        pk_val = _stringify(row.get(pk_field))
        for i, existing in enumerate(rows):
            if _stringify(existing.get(pk_field)) == pk_val:
                rows[i] = {**existing, **row}
                return
        rows.append(row)
        self._data[tab] = (rows, ts)

    def delete_local(self, tab: str, pk_field: str, pk_value: Any) -> None:
        cached = self._data.get(tab)
        if cached is None:
            return
        rows, ts = cached
        target = _stringify(pk_value)
        self._data[tab] = (
            [r for r in rows if _stringify(r.get(pk_field)) != target],
            ts,
        )

    def invalidate(self, tab: Optional[str] = None) -> None:
        if tab is None:
            self._data.clear()
        else:
            self._data.pop(tab, None)

    # ---- refresh --------------------------------------------------------

    async def _refresh_tab(self, tab: str) -> None:
        async with self._locks[tab]:
            # Double-check after acquiring the lock; another coroutine may have refreshed.
            cached = self._data.get(tab)
            if cached is not None and (time.monotonic() - cached[1]) < self.ttl:
                return
            try:
                rows = await run_sync(_fetch_tab_sync, tab)
                self._data[tab] = (rows, time.monotonic())
            except Exception as e:
                logger.warning(f"Failed to refresh '{tab}': {e}")
                # Update timestamp anyway so we don't spam retries immediately
                if cached is not None:
                    self._data[tab] = (cached[0], time.monotonic())

    async def refresh_all(self) -> None:
        tasks = [self._refresh_tab(tab) for tab in TABS]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def refresh_active(self) -> None:
        """Refresh only tabs read since the last couple of cycles. Keeps hot
        tabs (vote, poll, order at lunchtime) fresh without spending quota on
        tabs nobody is looking at."""
        cutoff = time.monotonic() - 2 * self.ttl
        active = [t for t in TABS if self._last_access.get(t, 0) >= cutoff]
        if not active:
            return
        tasks = [self._refresh_tab(tab) for tab in active]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_loop(self) -> None:
        try:
            await self.refresh_all()  # Warm up cache immediately on startup
            while True:
                await asyncio.sleep(self.ttl)
                await self.refresh_active()
        except asyncio.CancelledError:
            logger.info("Cache refresh loop cancelled")
            raise

    def start_refresh_loop(self) -> None:
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            logger.info(f"Cache refresh loop started (every {self.ttl}s)")

    async def stop_refresh_loop(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None


@with_retry()
def _fetch_tab_sync(tab: str) -> List[Dict[str, Any]]:
    """Single sync call, suitable for run_sync + the retry decorator."""
    return get_worksheet(tab).get_all_records()


def _stringify(v: Any) -> str:
    """Compare values as strings — Sheets returns ints for numeric cells, but PKs are usually strings or ints interchangeably."""
    return "" if v is None else str(v)


# Singleton for application use.
cache = SheetCache()
