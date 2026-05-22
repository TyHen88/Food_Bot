"""
Generic CRUD over Google Sheets tabs.

The whole rest of the codebase should go through `repo` — never call gspread
directly. That keeps caching, retry, and async-thread offloading consistent.

Write strategy:
    - Reads are served from cache.
    - `create` / `update` / `delete` update the cache synchronously, then
      schedule the Sheets write via asyncio.create_task. Callers do not
      await the network round-trip.

If you need a guaranteed-durable write (e.g. critical audit log), call the
*_sync variants that await the API. Most code wants the fire-and-forget
behavior of the plain methods.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from gspread.utils import rowcol_to_a1

from .cache import cache
from .client import get_spreadsheet, run_sync, with_retry
from .schema import PRIMARY_KEYS, TABS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def list_all(tab: str) -> List[Dict[str, Any]]:
    _assert_known(tab)
    return await cache.get_all(tab)


async def find_by(tab: str, field: str, value: Any) -> Optional[Dict[str, Any]]:
    _assert_known(tab)
    return await cache.find(tab, field, value)


async def find_by_pk(tab: str, pk_value: Any) -> Optional[Dict[str, Any]]:
    return await find_by(tab, PRIMARY_KEYS[tab], pk_value)


async def filter_rows(
    tab: str, predicate: Callable[[Dict[str, Any]], bool]
) -> List[Dict[str, Any]]:
    _assert_known(tab)
    return await cache.filter(tab, predicate)


# ---------------------------------------------------------------------------
# Write (fire-and-forget — cache updated immediately, Sheets eventually)
# ---------------------------------------------------------------------------

async def create(tab: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Append a new row (caller must have verified the PK is fresh).
    For situations where the row may already exist, use upsert().
    """
    _assert_known(tab)
    full = _coerce_row(tab, row)
    cache.upsert_local(tab, PRIMARY_KEYS[tab], full)
    _schedule(_append_sync, tab, full)
    return full


async def upsert(tab: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert or update by primary key. Cache updated immediately; Sheets
    write scheduled. Safe to call repeatedly (e.g. for poll-answer events
    where the same (poll, user) row gets rewritten as votes change).
    """
    _assert_known(tab)
    full = _coerce_row(tab, row)
    cache.upsert_local(tab, PRIMARY_KEYS[tab], full)
    _schedule(_upsert_sync, tab, full)
    return full


async def upsert_blocking(tab: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Like upsert(), but *awaits* the Sheets write so failures bubble up to
    the caller instead of getting swallowed by the background task.

    Use this when you need to be sure the row reached the spreadsheet —
    e.g. the order snapshot fired by the Order button.
    """
    _assert_known(tab)
    full = _coerce_row(tab, row)
    cache.upsert_local(tab, PRIMARY_KEYS[tab], full)
    await run_sync(_upsert_sync, tab, full)
    return full


async def update(tab: str, pk_value: Any, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Patch fields on the row whose PK matches `pk_value`. Cache updated
    immediately. Returns the merged row, or None if not found.
    """
    _assert_known(tab)
    pk = PRIMARY_KEYS[tab]
    existing = await cache.find(tab, pk, pk_value)
    if existing is None:
        logger.warning(f"update({tab}, pk={pk_value}): row not found")
        return None
    merged = {**existing, **fields}
    cache.upsert_local(tab, pk, merged)
    _schedule(_update_sync, tab, pk_value, fields)
    return merged


async def soft_delete(tab: str, pk_value: Any) -> bool:
    """Set is_active=FALSE; returns True if the row existed."""
    if "is_active" not in TABS[tab]:
        raise ValueError(f"Tab '{tab}' has no is_active column — use hard_delete")
    result = await update(tab, pk_value, {"is_active": "FALSE"})
    return result is not None


async def hard_delete(tab: str, pk_value: Any) -> bool:
    """Remove the row entirely. Prefer soft_delete where possible."""
    _assert_known(tab)
    pk = PRIMARY_KEYS[tab]
    existing = await cache.find(tab, pk, pk_value)
    if existing is None:
        return False
    cache.delete_local(tab, pk, pk_value)
    _schedule(_delete_sync, tab, pk_value)
    return True


# ---------------------------------------------------------------------------
# Convenience for common patterns
# ---------------------------------------------------------------------------

def new_uuid() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Internals — sync gspread calls, all decorated with retry
# ---------------------------------------------------------------------------

@with_retry()
def _append_sync(tab: str, row: Dict[str, Any]) -> None:
    ws = get_spreadsheet().worksheet(tab)
    headers = ws.row_values(1)
    values = [row.get(col, "") for col in headers]
    ws.append_row(values, value_input_option="USER_ENTERED")


@with_retry()
def _upsert_sync(tab: str, row: Dict[str, Any]) -> None:
    """Update the row whose PK matches; append if no match."""
    ws = get_spreadsheet().worksheet(tab)
    headers = ws.row_values(1)
    pk_field = PRIMARY_KEYS[tab]
    if pk_field not in headers:
        raise RuntimeError(f"PK column '{pk_field}' missing from tab '{tab}'")
    pk_col_index = headers.index(pk_field) + 1
    pk_str = str(row.get(pk_field, ""))

    pk_column = ws.col_values(pk_col_index)
    row_index = None
    for i, val in enumerate(pk_column[1:], start=2):  # row 1 is header
        if val == pk_str:
            row_index = i
            break

    values = [_to_cell(row.get(col, "")) for col in headers]
    if row_index is None:
        ws.append_row(values, value_input_option="USER_ENTERED")
        return
    first = rowcol_to_a1(row_index, 1)
    last = rowcol_to_a1(row_index, len(headers))
    ws.update(values=[values], range_name=f"{first}:{last}", value_input_option="USER_ENTERED")


@with_retry()
def _update_sync(tab: str, pk_value: Any, fields: Dict[str, Any]) -> None:
    ws = get_spreadsheet().worksheet(tab)
    headers = ws.row_values(1)
    pk_col_name = PRIMARY_KEYS[tab]
    if pk_col_name not in headers:
        raise RuntimeError(f"PK column '{pk_col_name}' missing from tab '{tab}'")
    pk_col_index = headers.index(pk_col_name) + 1  # 1-based

    pk_str = str(pk_value)
    pk_column_values = ws.col_values(pk_col_index)
    # row 1 is the header; offset by 1
    row_index = None
    for i, val in enumerate(pk_column_values[1:], start=2):
        if val == pk_str:
            row_index = i
            break
    if row_index is None:
        logger.warning(f"_update_sync({tab}, {pk_value}): no matching row in sheet")
        return

    updates = []
    for col_name, new_val in fields.items():
        if col_name not in headers:
            logger.warning(f"_update_sync({tab}): unknown column '{col_name}', skipping")
            continue
        col_index = headers.index(col_name) + 1
        a1 = rowcol_to_a1(row_index, col_index)
        updates.append({"range": a1, "values": [[_to_cell(new_val)]]})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")


@with_retry()
def _delete_sync(tab: str, pk_value: Any) -> None:
    ws = get_spreadsheet().worksheet(tab)
    headers = ws.row_values(1)
    pk_col_index = headers.index(PRIMARY_KEYS[tab]) + 1
    pk_str = str(pk_value)
    pk_column_values = ws.col_values(pk_col_index)
    for i, val in enumerate(pk_column_values[1:], start=2):
        if val == pk_str:
            ws.delete_rows(i)
            return


def _assert_known(tab: str) -> None:
    if tab not in TABS:
        raise KeyError(f"Unknown tab '{tab}'. Declared tabs: {sorted(TABS)}")


def _coerce_row(tab: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Stringify cells so Sheets stores them predictably (booleans → TRUE/FALSE)."""
    return {col: _to_cell(row.get(col, "")) for col in TABS[tab]}


def _to_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return str(v)


def _schedule(fn, *args) -> None:
    """Run a sync Sheets call in a background task; log failures, never raise."""
    async def runner():
        try:
            await run_sync(fn, *args)
        except Exception as e:
            logger.error(f"Background Sheets write failed ({fn.__name__}): {e}")

    try:
        asyncio.get_running_loop().create_task(runner())
    except RuntimeError:
        # Called from sync context with no loop (e.g. tests). Fall back to blocking call.
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"Synchronous Sheets write failed ({fn.__name__}): {e}")
