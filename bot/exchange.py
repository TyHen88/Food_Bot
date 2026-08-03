"""
Official USD→KHR exchange rate, from the National Bank of Cambodia.

NBC publishes the official rate around 16:30 ICT on working days and has no
official JSON API, so the rate is fetched ONCE A DAY by a scheduler job and
stored in the `exchange_rate` tab. Everything else — invoices, the /api
endpoint, the AI assistant, the /exchange_rate command — reads the stored
value off the Sheets cache. Nothing user-facing ever waits on nbc.gov.kh.

Sources, tried in order (`refresh`):
    1. Cambodia's open-data portal (data.mef.gov.kh), whose "Khmer Riel
       Exchange Rate" dataset is published by NBC itself. Cleanest source,
       but its file endpoint has been returning 502 — hence the fallback.
    2. NBC's own page. It is plain server-rendered HTML: the rate sits in the
       markup as `Official Exchange Rate : <font>4047</font> KHR / USD`. The
       site 403s non-browser clients, so a real User-Agent is required.
    3. Whatever is already in the sheet. NBC does not publish at weekends or
       on public holidays, so "no rate today" is the normal case roughly two
       days in five — the newest stored row is carried forward.

A parsed value outside [EXCHANGE_RATE_MIN, EXCHANGE_RATE_MAX] is treated as a
broken page and discarded: a stale rate is recoverable, a wrong one silently
mis-prices every invoice that quotes it.
"""

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from .config import (
    EXCHANGE_RATE_MAX,
    EXCHANGE_RATE_MIN,
    EXCHANGE_RATE_STALE_DAYS,
    EXCHANGE_USER_AGENT,
    KHR_ROUNDING,
    MEF_EXCHANGE_API_URL,
    NBC_EXCHANGE_URL,
    TIMEZONE,
)
from .sheets import repo
from .sheets.client import is_configured

logger = logging.getLogger(__name__)

# Last successful fetch, kept in memory so the bot still quotes a rate when
# Sheets is unconfigured (local dev) or momentarily unreachable.
_last_known: Optional[Dict[str, Any]] = None

_TAGS = re.compile(r"<[^>]+>")
_RATE_RE = re.compile(
    r"Official\s+Exchange\s+Rate\s*:\s*([0-9][0-9,\s]*(?:\.[0-9]+)?)\s*KHR\s*/\s*USD",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"Exchange\s+Rate\s+on\s*:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def today() -> date:
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:
        return datetime.now().date()


def _plausible(value: Any) -> Optional[float]:
    """The parsed number, or None when it can't be a USD→KHR rate."""
    try:
        rate = float(str(value).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None
    if not (EXCHANGE_RATE_MIN <= rate <= EXCHANGE_RATE_MAX):
        logger.warning(
            "Discarding implausible exchange rate %s (expected %s-%s) — "
            "the source page has probably changed shape",
            rate, EXCHANGE_RATE_MIN, EXCHANGE_RATE_MAX,
        )
        return None
    return rate


# ---------------------------------------------------------------------------
# Parsing (pure — unit-testable against a saved page)
# ---------------------------------------------------------------------------

def parse_nbc_page(html: str) -> Optional[Tuple[str, float]]:
    """(rate_date, usd_khr) from NBC's exchange-rate page, or None.

    Tag-tolerant: the numbers are wrapped in <font> today, but the labels
    around them have been stable, so tags are stripped before matching.
    """
    text = _TAGS.sub(" ", html or "")
    rate_match = _RATE_RE.search(text)
    if not rate_match:
        return None
    rate = _plausible(rate_match.group(1))
    if rate is None:
        return None
    date_match = _DATE_RE.search(text)
    rate_date = date_match.group(1) if date_match else today().isoformat()
    return rate_date, rate


def parse_mef_payload(payload: Any) -> Optional[Tuple[str, float]]:
    """(rate_date, usd_khr) from the open-data portal's response, or None.

    The portal's file endpoint is documented only as "Real-Time API", so the
    exact envelope is not guaranteed — this walks whatever nested dict/list
    comes back looking for a rate-ish key, and returns None rather than
    guessing when nothing matches.
    """
    rate_keys = ("usd_khr", "usd", "rate", "exchange_rate", "official_rate", "khr")
    date_keys = ("rate_date", "date", "issued_date", "published_date", "day")

    def walk(node: Any) -> Optional[Tuple[Optional[str], float]]:
        if isinstance(node, dict):
            found_rate = None
            found_date = None
            for key, value in node.items():
                lowered = str(key).lower()
                if found_rate is None and any(k == lowered or k in lowered for k in rate_keys):
                    found_rate = _plausible(value)
                if found_date is None and any(k == lowered or k in lowered for k in date_keys):
                    text = str(value or "")[:10]
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                        found_date = text
            if found_rate is not None:
                return found_date, found_rate
            for value in node.values():
                hit = walk(value)
                if hit:
                    return hit
        elif isinstance(node, list):
            for value in node:
                hit = walk(value)
                if hit:
                    return hit
        return None

    hit = walk(payload)
    if not hit:
        return None
    rate_date, rate = hit
    return rate_date or today().isoformat(), rate


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def _fetch_mef() -> Optional[Tuple[str, float]]:
    if not MEF_EXCHANGE_API_URL:
        return None
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            MEF_EXCHANGE_API_URL,
            headers={"User-Agent": EXCHANGE_USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        return parse_mef_payload(response.json())


async def _fetch_nbc() -> Optional[Tuple[str, float]]:
    if not NBC_EXCHANGE_URL:
        return None
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(
            NBC_EXCHANGE_URL,
            headers={
                "User-Agent": EXCHANGE_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        response.raise_for_status()
        return parse_nbc_page(response.text)


async def _store(row: Dict[str, Any]) -> None:
    """Write one rate row, creating the tab if this deploy hasn't yet.

    Upsert by date, so re-running on the same day (startup + the daily job)
    refreshes one row instead of appending duplicates.

    Blocking rather than fire-and-forget on purpose: this runs once a day,
    not in a user-facing path, and a background failure here used to vanish
    into a generic "Background Sheets write failed (_upsert_sync):
    exchange_rate" with the day's rate silently lost. If the tab is missing
    — a first boot after the schema was extended, where the startup
    bootstrap didn't run or errored — re-run the bootstrap once and retry,
    so a new deploy heals itself instead of going rate-less until someone
    reads the log.
    """
    try:
        await repo.upsert_blocking("exchange_rate", row)
        return
    except Exception as e:
        logger.warning(
            "Exchange rate: writing to the `exchange_rate` tab failed (%s: %s) — "
            "re-running the schema bootstrap and retrying once",
            type(e).__name__, e,
        )

    try:
        from .sheets.bootstrap import ensure_schema
        await ensure_schema()
        await repo.upsert_blocking("exchange_rate", row)
        logger.info("Exchange rate: tab created by bootstrap, rate stored")
    except Exception as e:
        # In-memory _last_known still serves reads, so the bot keeps quoting
        # a rate; only persistence is lost until the next refresh.
        logger.error(
            "Exchange rate: could not persist to Sheets even after bootstrap "
            "(%s: %s). Check that the `exchange_rate` tab exists and the "
            "service account has Editor access.",
            type(e).__name__, e, exc_info=True,
        )


async def refresh() -> Optional[Dict[str, Any]]:
    """Fetch today's rate and store it. Returns the stored row, or None when
    every source failed (the caller keeps using the last known rate)."""
    global _last_known

    for source, fetch in (("mef", _fetch_mef), ("nbc", _fetch_nbc)):
        try:
            result = await fetch()
        except Exception as e:
            logger.warning("Exchange rate: %s source failed: %s", source, e)
            continue
        if not result:
            logger.warning("Exchange rate: %s source returned nothing usable", source)
            continue

        rate_date, rate = result
        row = {
            "rate_date": rate_date,
            "usd_khr": f"{rate:.2f}",
            "source": source,
            "fetched_at": repo.now_iso(),
        }
        _last_known = {"rate_date": rate_date, "usd_khr": rate, "source": source}
        if is_configured():
            await _store(row)
        logger.info("Exchange rate %s = %s KHR/USD (source: %s)", rate_date, rate, source)
        return {"rate_date": rate_date, "usd_khr": rate, "source": source}

    logger.error("Exchange rate: all sources failed — keeping the last known rate")
    return None


# ---------------------------------------------------------------------------
# Reading (cache-backed — safe in request paths)
# ---------------------------------------------------------------------------

async def list_rates() -> List[Dict[str, Any]]:
    """Stored rates, newest publication date first."""
    if not is_configured():
        return [_last_known] if _last_known else []
    try:
        stored = await repo.list_all("exchange_rate")
    except Exception as e:
        # The tab may not exist yet on the very first boot after this
        # schema bump, and Sheets can be briefly unreachable. Neither is a
        # reason to fail an invoice or the /exchange_rate command.
        logger.warning("Exchange rate: reading the sheet failed (%s)", e)
        return [_last_known] if _last_known else []
    rows = []
    for r in stored:
        rate = _plausible(r.get("usd_khr"))
        rate_date = str(r.get("rate_date", "") or "")[:10]
        if rate is None or not rate_date:
            continue
        rows.append({
            "rate_date": rate_date,
            "usd_khr": rate,
            "source": str(r.get("source", "") or ""),
            "fetched_at": str(r.get("fetched_at", "") or ""),
        })
    rows.sort(key=lambda r: r["rate_date"], reverse=True)
    return rows


async def current() -> Optional[Dict[str, Any]]:
    """The rate in force now: the newest published one, carried forward over
    weekends and holidays. None only when nothing has ever been fetched."""
    rows = await list_rates()
    if rows:
        return rows[0]
    return _last_known


async def rate_for(order_date: str) -> Optional[Dict[str, Any]]:
    """The rate that was in force on `order_date` — the newest publication on
    or before it. Used when invoicing a past order so the riel figures match
    what the day actually cost, not today's rate."""
    day = str(order_date or "")[:10]
    rows = await list_rates()
    if not day:
        return rows[0] if rows else _last_known
    for row in rows:  # newest first
        if row["rate_date"] <= day:
            return row
    # Order predates anything we ever stored — the oldest rate is the closest.
    return rows[-1] if rows else _last_known


async def is_stale() -> bool:
    """True when the newest stored rate is old enough to suspect the fetch is
    broken (weekends and a public holiday are normal; a fortnight is not)."""
    row = await current()
    if not row:
        return True
    try:
        age = (today() - date.fromisoformat(row["rate_date"])).days
    except (ValueError, TypeError, KeyError):
        return True
    return age > EXCHANGE_RATE_STALE_DAYS


# ---------------------------------------------------------------------------
# Conversion / formatting
# ---------------------------------------------------------------------------

def to_khr(usd: float, rate: float, rounding: Optional[int] = None) -> int:
    """USD → riel, rounded to the nearest note (KHR_ROUNDING, default 100៛).

    Riel is not quoted in cents: $1.75 at 4047 is 7,082.25៛, which nobody
    pays. Rounding is applied per amount — round each person's share, not the
    group total, so what each member is asked for is what they can hand over.
    """
    step = KHR_ROUNDING if rounding is None else rounding
    exact = float(usd or 0) * float(rate or 0)
    if step and step > 1:
        return int(round(exact / step) * step)
    return int(round(exact))


def format_khr(amount: int) -> str:
    """Riel with thousands separators and the riel sign, e.g. "7,100៛"."""
    return f"{int(amount):,}៛"


def format_rate(rate: float) -> str:
    """The rate the way NBC states it, e.g. "4,047 KHR / USD"."""
    rate = float(rate or 0)
    body = f"{rate:,.0f}" if abs(rate - round(rate)) < 0.005 else f"{rate:,.2f}"
    return f"{body} KHR / USD"


def format_dual(usd: float, rate: Optional[float]) -> str:
    """"$3.50 (14,200៛)" — or just the dollars when no rate is known."""
    return format_money(usd, rate, ("USD", "KHR"))


# Currencies an invoice may be rendered in. Amounts are always STORED in USD
# (that is what the admin's prices and every stored subtotal mean); this only
# selects what the reader sees.
CURRENCIES = ("USD", "KHR")
DEFAULT_CURRENCIES = ("USD",)


def normalize_currencies(value: Any) -> Tuple[str, ...]:
    """Clean a caller-supplied currency selection.

    Accepts a list, a comma-separated string (how it is stored in the sheet),
    or None. Unknown codes are dropped and an empty selection falls back to
    dollars — an invoice with no currency at all would show no amounts.
    """
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    picked = [c for c in CURRENCIES if c in {str(i).strip().upper() for i in items}]
    return tuple(picked) or DEFAULT_CURRENCIES


def format_money(usd: float, rate: Optional[float],
                 currencies: Any = DEFAULT_CURRENCIES) -> str:
    """One amount, rendered in the selected currencies.

        ("USD",)        → "$3.50"
        ("KHR",)        → "14,200៛"
        ("USD","KHR")   → "$3.50 (14,200៛)"

    Riel needs a rate; without one the amount falls back to dollars rather
    than being dropped, so a missing rate can never hide what is owed.
    """
    wanted = normalize_currencies(currencies)
    usd_text = f"${float(usd or 0):.2f}"
    if not rate:
        return usd_text
    khr_text = format_khr(to_khr(usd, rate))
    if "USD" in wanted and "KHR" in wanted:
        return f"{usd_text} ({khr_text})"
    if "KHR" in wanted:
        return khr_text
    return usd_text


def khr_to_usd(khr: float, rate: float) -> float:
    """Riel typed by an admin → the USD amount actually stored/invoiced."""
    if not rate:
        return 0.0
    return round(float(khr or 0) / float(rate), 2)
