"""
gspread client singleton + retry helper.

Reads GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS_JSON from bot.config.
If either is missing, is_configured() returns False and callers should
no-op gracefully — this lets the bot boot in Phase 0 / local dev without
a service account.

All gspread calls are synchronous (HTTP). Wrap them with run_sync() to
keep the FastAPI event loop responsive.
"""

import asyncio
import functools
import json
import logging
import random
from typing import Callable, Optional, TypeVar

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

from ..config import GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_client: Optional[gspread.Client] = None
_spreadsheet: Optional[gspread.Spreadsheet] = None

T = TypeVar("T")


def is_configured() -> bool:
    """True iff credentials and sheet ID are both present."""
    return bool(GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS_JSON)


def _parse_credentials_json(raw: str) -> dict:
    """Parse GOOGLE_CREDENTIALS_JSON, repairing one common paste mistake.

    Some env var UIs half-decode a pasted service-account file: the
    private_key's ``\\n`` escapes get split into a literal backslash
    followed by a real newline byte (instead of staying as the two
    characters ``\\`` + ``n``), while surrounding quotes get manually
    backslash-escaped. Both together make otherwise-correct JSON fail to
    parse, so retry once with that specific pattern repaired before
    giving up.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        repaired = raw.replace("\\\n", "\\n").replace('\\"', '"')
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON is not valid JSON. It must be the ENTIRE "
                "service-account JSON file (the object that begins with "
                "'{\"type\": \"service_account\", ...}'), pasted on one line — "
                "not just the private_key field. "
                f"Parse error: {e}"
            ) from e


def _build_client() -> gspread.Client:
    info = _parse_credentials_json(GOOGLE_CREDENTIALS_JSON)
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON parsed but doesn't look like a service-account "
            "credential (missing 'type: service_account'). Re-download the JSON key "
            "from Google Cloud Console → IAM → Service Accounts → Keys → Add Key → JSON."
        )
    creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    return gspread.authorize(creds)


def get_client() -> gspread.Client:
    """Return the cached gspread client; build on first use."""
    global _client
    if _client is None:
        if not is_configured():
            raise RuntimeError(
                "Sheets client requested but GOOGLE_SHEET_ID or "
                "GOOGLE_CREDENTIALS_JSON is not configured."
            )
        _client = _build_client()
        logger.info("gspread client initialised")
    return _client


def get_spreadsheet() -> gspread.Spreadsheet:
    """Return the cached Spreadsheet handle; open on first use."""
    global _spreadsheet
    if _spreadsheet is None:
        try:
            _spreadsheet = get_client().open_by_key(GOOGLE_SHEET_ID)
        except PermissionError as e:
            try:
                info = _parse_credentials_json(GOOGLE_CREDENTIALS_JSON)
                sa_email = info.get("client_email", "<service-account-email>")
            except Exception:
                sa_email = "<service-account-email>"
            raise RuntimeError(
                f"Service account does not have access to spreadsheet "
                f"{GOOGLE_SHEET_ID}. Open the sheet in your browser, click "
                f"'Share', and grant Editor access to: {sa_email}. "
                f"Also verify Google Sheets API and Drive API are both ENABLED "
                f"on the GCP project."
            ) from e
        except APIError as e:
            raise RuntimeError(f"Sheets API error opening spreadsheet: {e}") from e
        logger.info(f"Opened spreadsheet: {_spreadsheet.title}")
    return _spreadsheet


def reset_client() -> None:
    """Drop cached client + spreadsheet (test hook, or after auth rotation)."""
    global _client, _spreadsheet
    _client = None
    _spreadsheet = None


def with_retry(
    *, max_attempts: int = 3, base_delay: float = 1.0
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorate a sync gspread call to retry on 429 / 5xx with exponential
    backoff and a little jitter.

    Google enforces 60 writes/min per service account — burst-friendly
    code can still trip 429 RESOURCE_EXHAUSTED.
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except APIError as e:
                    status = getattr(e.response, "status_code", None)
                    retriable = status == 429 or (status is not None and 500 <= status < 600)
                    if not retriable or attempt == max_attempts:
                        logger.error(
                            f"Sheets API failed (attempt {attempt}/{max_attempts}, "
                            f"status={status}): {e}"
                        )
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        f"Sheets API {status} on attempt {attempt}/{max_attempts}, "
                        f"retrying in {delay:.1f}s"
                    )
                    import time
                    time.sleep(delay)
            # Unreachable, but keeps type-checkers happy.
            raise RuntimeError("retry loop exited without returning")
        return wrapper
    return decorator


async def run_sync(fn: Callable[..., T], *args, **kwargs) -> T:
    """Run a blocking gspread call in a worker thread."""
    return await asyncio.to_thread(fn, *args, **kwargs)
