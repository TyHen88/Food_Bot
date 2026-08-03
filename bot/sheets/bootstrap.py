"""
Idempotent schema bootstrap for Google Sheets.

ensure_schema():
    For each tab defined in schema.TABS, create it if missing and write
    the header row. If the tab exists but the header row is wrong, append
    any missing columns (never reorder or delete — that would surprise
    a user who has been editing the sheet).

seed_defaults():
    Insert SEED_COMMON_CODE / SEED_SETTING / SEED_SCHEDULE rows only if
    the corresponding tab is empty (i.e. header row present but no data
    rows yet). Safe to re-run.

Both functions are safe to call on every startup.
"""

import logging
from typing import List

from gspread.utils import rowcol_to_a1

from .client import get_spreadsheet, run_sync, with_retry
from .schema import (
    SEED_COMMON_CODE,
    SEED_SCHEDULE,
    SEED_SETTING,
    SEED_TEMPLATE,
    TABS,
)

logger = logging.getLogger(__name__)

_META_TAB = "_meta"


@with_retry()
def _ensure_schema_sync() -> None:
    ss = get_spreadsheet()
    existing = {ws.title: ws for ws in ss.worksheets()}
    created: List[str] = []
    extended: List[str] = []

    for tab, headers in TABS.items():
        # Name the tab in the log before doing anything to it: this runs
        # ~2 API calls per tab against a 60/min quota, and a failure part-way
        # through used to surface only as a bare exception in main.py's
        # "Sheets bootstrap failed" line, with no clue which tab broke.
        try:
            if tab not in existing:
                ws = ss.add_worksheet(title=tab, rows=200, cols=max(len(headers), 10))
                ws.update(values=[headers], range_name=f"A1:{rowcol_to_a1(1, len(headers))}")
                ws.freeze(rows=1)
                created.append(tab)
                logger.info(f"Created tab '{tab}' with {len(headers)} columns")
                continue

            ws = existing[tab]
            first_row = ws.row_values(1)
            # Append any missing headers at the end (preserve manual reorders by user).
            missing = [h for h in headers if h not in first_row]
            if missing:
                start_col = len(first_row) + 1
                end_col = start_col + len(missing) - 1
                # A tab's grid is only as wide as it was created; writing past
                # its last column fails with "Range (invoice!N1:O1) exceeds
                # grid limits", NOT by growing the sheet. Widen it first, or
                # every schema addition beyond the original column count
                # aborts the whole bootstrap.
                current_cols = ws.col_count
                if current_cols < end_col:
                    ws.add_cols(end_col - current_cols)
                    logger.info(
                        f"Widened '{tab}' from {current_cols} to {end_col} columns"
                    )
                ws.update(
                    values=[missing],
                    range_name=f"{rowcol_to_a1(1, start_col)}:{rowcol_to_a1(1, end_col)}",
                )
                extended.append(tab)
                logger.info(f"Appended missing columns to '{tab}': {missing}")
            ws.freeze(rows=1)
        except Exception:
            # Re-raise unchanged so with_retry still sees the original APIError
            # (and its 429/5xx status) — this only adds the missing context.
            logger.error(
                "ensure_schema failed while processing tab '%s' "
                "(%d/%d tabs done: created=%s, extended=%s)",
                tab, len(created) + len(extended), len(TABS), created, extended,
            )
            raise

    logger.info(
        "Schema check complete: %d tab(s) present, created=%s, columns added to=%s",
        len(TABS), created or "none", extended or "none",
    )


@with_retry()
def _seed_tab_if_empty_sync(tab_name: str, rows: List[dict]) -> int:
    """Append `rows` to `tab_name` only if the tab currently has no data rows."""
    ss = get_spreadsheet()
    ws = ss.worksheet(tab_name)
    headers = ws.row_values(1)
    if not headers:
        logger.warning(f"Tab '{tab_name}' has no header row, skipping seed")
        return 0

    # all_values includes the header; >1 means there's already data.
    if len(ws.get_all_values()) > 1:
        return 0

    payload = [[row.get(col, "") for col in headers] for row in rows]
    if not payload:
        return 0
    ws.append_rows(payload, value_input_option="USER_ENTERED")
    logger.info(f"Seeded '{tab_name}' with {len(payload)} default row(s)")
    return len(payload)


async def ensure_schema() -> None:
    """Async wrapper — safe to call from FastAPI lifespan."""
    await run_sync(_ensure_schema_sync)


async def seed_defaults() -> None:
    await run_sync(_seed_tab_if_empty_sync, "common_code", SEED_COMMON_CODE)
    await run_sync(_seed_tab_if_empty_sync, "setting", SEED_SETTING)
    await run_sync(_seed_tab_if_empty_sync, "schedule", SEED_SCHEDULE)
    await run_sync(_seed_tab_if_empty_sync, "template", SEED_TEMPLATE)


async def bootstrap() -> None:
    """One-stop: schema + seeds. Called from FastAPI lifespan."""
    logger.info("Bootstrapping Google Sheets schema...")
    await ensure_schema()
    await seed_defaults()
    logger.info("Sheets bootstrap complete")
