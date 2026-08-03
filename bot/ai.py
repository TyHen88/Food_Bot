"""
AI assistant for the Food Bot — internal-only, data-grounded.

One pipeline shared by the Mini App endpoint (bot/api/ai.py) and the
Telegram /ai command (bot/handlers.py): classify the question, fetch the
caller's OWN records from the four allowed tabs (user, order, invoice,
poll), then generate a friendly grounded answer via Ollama.

Questions are routed to one of four answers: `data` (the group's own
records), `help` (how the bot works), `external` (anything the public web
answers — searched via Ollama's search API and answered from the results),
and `privacy` (a canned refusal).

Hard boundaries, enforced here rather than trusted to the model:
    - The internal and external routes never mix. Group figures are only
      ever produced from the DATA block; the external prompt is explicitly
      forbidden from stating anything about the group's orders or spending,
      and exchange-rate questions are forced internal (we price invoices at
      the stored NBC rate, so a market rate off the web would contradict
      what members actually paid).
    - Privacy: orders/invoices/polls are GROUP-shared (invoices are posted
      to the whole chat, so every member already sees who ordered what) —
      but only for chats the caller belongs to; other groups' rows never
      reach the prompt. Personal profile details (phone numbers etc.) are
      never fetched for anyone but the caller, and questions about another
      member's personal info get a canned refusal.
    - Data sources are limited to user / order / invoice / poll. No
      settings, schedules, history or payer data reaches the model.

Numbers are the model's weak point, so none of them are its job:
    - The reporting period is resolved in Python (`resolve_range`), not by
      asking the LLM to do date arithmetic. The LLM only names a period;
      keyword matches on the raw question override it.
    - Every amount the answer can need is precomputed — per caller, per
      person, and per day — so no question requires the model to add up
      rows. The prompt says so explicitly.
    - The resolved period is stated back in the answer, so a wrong period
      is visible to the user instead of silently changing the amount.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from . import exchange
from .config import (
    AI_WEB_SEARCH,
    AI_WEB_SEARCH_RESULTS,
    OLLAMA_API_KEY,
    OLLAMA_API_URL,
    OLLAMA_MODEL,
    OLLAMA_SEARCH_URL,
    TIMEZONE,
)
from .people import build_uid_index, is_same_person, name_variants, person_key, strip_invisible
from .sheets import invoices as sheets_invoices
from .sheets import orders as sheets_orders
from .sheets import repo
from .sheets.client import is_configured

logger = logging.getLogger(__name__)

# Prompt-size caps. Aggregates are computed over the FULL result set before
# these apply, so a truncated list never changes an amount — but the model
# is told when a list was cut so it can't present it as complete.
MAX_ORDER_LINES = 400
MAX_INVOICES = 100
MAX_POLLS = 60
MAX_DAYS = 180


async def _call_ollama(
    messages: list,
    *,
    json_mode: bool = False,
    temperature: float = 0.0,
) -> str:
    """Make an async call to the Ollama /api/chat endpoint.

    `temperature` is passed explicitly because Ollama's default is 0.8 —
    at that setting the same question routes differently run to run, which
    showed up as amounts that changed between identical questions.
    `json_mode` constrains the reply to a JSON object (used by the router).
    """
    if not OLLAMA_API_URL:
        raise ValueError("OLLAMA_API_URL is not configured.")

    headers = {}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    payload: Dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"

    logger.info(f"Calling Ollama at {OLLAMA_API_URL} with model {OLLAMA_MODEL}...")
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(OLLAMA_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        # Ollama API structure for chat has messages:
        # {"message": {"role": "assistant", "content": "..."}}
        content = data.get("message", {}).get("content", "")
        return content.strip()


async def web_search(query: str, max_results: int = 0) -> List[Dict[str, str]]:
    """Search the public web via Ollama's hosted search API.

    Same credential as the chat API. Returns [] — never raises — when search
    is disabled, unkeyed, or the call fails; the caller then answers from the
    model's own knowledge and says so rather than dropping the question.
    """
    if not (AI_WEB_SEARCH and OLLAMA_API_KEY and OLLAMA_SEARCH_URL):
        return []
    payload = {"query": query, "max_results": max_results or AI_WEB_SEARCH_RESULTS}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                OLLAMA_SEARCH_URL,
                json=payload,
                headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.warning("Web search failed for %r: %s", query[:80], e)
        return []

    results = []
    for item in (data or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append({
            "title": str(item.get("title", "") or "")[:200],
            "url": str(item.get("url", "") or "")[:400],
            # Page bodies can be very long; the model only needs the gist.
            "content": str(item.get("content", "") or "")[:1500],
        })
    logger.info("Web search %r → %d result(s)", query[:80], len(results))
    return results


def _clean_json_response(text: str) -> str:
    """Remove markdown json code block fences if present in Ollama output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Best-effort JSON object out of a model reply.

    Tries the fence-stripped text first, then the outermost {...} block —
    some models prefix a sentence or a reasoning preamble even in JSON mode.
    Raises ValueError when nothing parses.
    """
    cleaned = _clean_json_response(text)
    for candidate in (cleaned, *_brace_blocks(cleaned)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


def _brace_blocks(text: str) -> List[str]:
    start = text.find("{")
    end = text.rfind("}")
    return [text[start:end + 1]] if 0 <= start < end else []


def _today() -> date:
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:
        return datetime.now().date()


def _today_str() -> str:
    return _today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Reporting period — resolved in Python, never by the model.
# ---------------------------------------------------------------------------

PERIODS = (
    "today", "yesterday", "this_week", "last_week", "last_7_days",
    "this_month", "last_month", "last_30_days", "this_year", "all_time",
)

# Checked against the raw question BEFORE the model's answer is considered.
# A phrase this list recognises is resolved deterministically; only novel
# phrasings ("in May", "between the 3rd and the 9th") fall through to the LLM.
# Order matters — first match wins, so specific periods are listed before
# the catch-all. "how much in total did I spend this month" is a question
# about this month, not about all time.
_KEYWORD_PERIODS: Tuple[Tuple[str, str], ...] = (
    (r"\btoday\b|\bthis morning\b|ថ្ងៃនេះ", "today"),
    (r"\byesterday\b|ម្សិលមិញ", "yesterday"),
    (r"\b(last|past)\s*30\s*days\b", "last_30_days"),
    (r"\b(last|past)\s*7\s*days\b", "last_7_days"),
    (r"\b(last|previous)\s+month\b|ខែមុន", "last_month"),
    (r"\b(this|current)\s+month\b|ខែនេះ", "this_month"),
    (r"\b(last|previous)\s+week\b|សប្តាហ៍មុន|សប្ដាហ៍មុន", "last_week"),
    (r"\b(this|current)\s+week\b|សប្តាហ៍នេះ|សប្ដាហ៍នេះ", "this_week"),
    (r"\b(this|current)\s+year\b|ឆ្នាំនេះ", "this_year"),
    (r"\ball[-\s]?time\b|\bso far\b|\bever\b", "all_time"),
)

# Exchange-rate questions are internal: they're answered from the NBC rate we
# store and price invoices at, never from a web search.
_EXCHANGE_RE = re.compile(
    r"exchange\s*rate|\bkhr\b|\briel\b|\bnbc\b|riels?\b|រៀល|អត្រា(ប្តូរ|ប្ដូរ)?ប្រាក់"
    r"|usd\s*(to|/|-)\s*khr|khr\s*(to|/|-)\s*usd|ដុល្លារ",
    re.IGNORECASE,
)

_PERIOD_LABELS = {
    "today": "today",
    "yesterday": "yesterday",
    "this_week": "this week",
    "last_week": "last week",
    "last_7_days": "the last 7 days",
    "this_month": "this month",
    "last_month": "last month",
    "last_30_days": "the last 30 days",
    "this_year": "this year",
    "all_time": "all time",
}


def period_from_text(text: str) -> Optional[str]:
    """Deterministic period detection from the user's own words, or None."""
    lowered = str(text or "").lower()
    for pattern, period in _KEYWORD_PERIODS:
        if re.search(pattern, lowered):
            return period
    return None


def period_bounds(period: str, today: date) -> Tuple[Optional[str], Optional[str]]:
    """Inclusive ISO bounds for a named period. Unknown → all time."""
    if period == "today":
        return today.isoformat(), today.isoformat()
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if period == "this_week":
        return (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    if period == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        return (this_monday - timedelta(days=7)).isoformat(), (this_monday - timedelta(days=1)).isoformat()
    if period == "last_7_days":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "last_30_days":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if period == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if period == "last_month":
        last_day_prev = today.replace(day=1) - timedelta(days=1)
        first_day_prev = last_day_prev.replace(day=1)
        return first_day_prev.isoformat(), last_day_prev.isoformat()
    if period == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return None, None


def _valid_iso(value: Any) -> Optional[str]:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def _range_label(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return "all time"
    if start and end and start == end:
        return start
    return f"{start or 'the beginning'} → {end or 'today'}"


def resolve_range(
    user_query: str,
    intent: Dict[str, Any],
    today: Optional[date] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """(start, end, human label) for the question.

    Precedence: a period phrase the user actually typed > the period the
    model named > explicit dates the model extracted > all time. Model dates
    are validated and clamped — an end date in the future (or a start after
    the end) is a model error, not a real period, and would otherwise widen
    the range and inflate the amount.
    """
    today = today or _today()

    keyword = period_from_text(user_query)
    if keyword:
        start, end = period_bounds(keyword, today)
        return start, end, _PERIOD_LABELS[keyword]

    period = str(intent.get("period") or "").strip().lower()
    if period in PERIODS and period != "all_time":
        start, end = period_bounds(period, today)
        return start, end, _PERIOD_LABELS[period]

    start = _valid_iso(intent.get("start_date"))
    end = _valid_iso(intent.get("end_date"))
    if start and end and start > end:
        start, end = end, start
    if end and end > today.isoformat():
        end = today.isoformat()
    if start and start > today.isoformat():
        start = end = None
    if start or end:
        return start, end, _range_label(start, end)

    if period == "all_time":
        return None, None, "all time"
    return None, None, "all time"


# ---------------------------------------------------------------------------
# Static knowledge: what this bot can do (the "help" grounding).
# ---------------------------------------------------------------------------

FEATURE_GUIDE = """HOW ORDERING WORKS
1. Post a numbered food menu in the group (lines starting 1-6 or ១-៦, or a message beginning with "ម្ហូបថ្ងៃ") — the bot instantly turns it into a poll.
2. Members vote for the dishes they want (several choices allowed; voting again updates your picks).
3. Tap the "Order" button under the poll to post the live order summary; "Close Order" locks it. At the daily cutoff time the votes are snapshotted into that day's order automatically.
4. From the Orders page in the Mini App, an admin enters prices and sends the invoice to the group — it lists each person's dishes, their share, the grand total, and the payer's KHQR to scan.

COMMANDS (everyone)
/start — welcome & instructions · /app — open the Mini App · /subscribe and /unsubscribe — turn the daily reminder on/off for the chat · /vongsa — Vongsa's payment KHQR · /ty — Ty's payment KHQR · /exchange_rate — the National Bank of Cambodia's official USD/KHR rate · /ai <question> — ask this assistant directly in chat
COMMANDS (admins)
/admin — open the admin panel · /set — update a setting · /schedule_list, /schedule_enable, /schedule_disable — manage scheduled reminders

MINI APP PAGES
Dashboard (calendar of order days + quick actions, incl. this AI) · Orders (per-day orders with date picker, search, generate/view invoice) · Invoices (history with date-range filter and Total / My Amount cards) · Members · Admin only: Templates (menu presets), Schedule (reminders), Settings, History (audit log)

ABOUT AMOUNTS
A day only has a money amount once an admin has generated its invoice. Days that were ordered but not yet invoiced count as dishes ordered, but $0.00 spent.
Invoices are priced in US dollars and also shown in riel, converted at the National Bank of Cambodia's official rate and rounded to the nearest 100៛. Each invoice keeps the rate it was sent at, so an old invoice never changes value when the rate moves."""


# Canned replies — deterministic, bilingual, never sent through the LLM.
# OFF_TOPIC_REPLY is now only a fallback: outside questions are normally
# answered by the external route, so this is what the user sees when that
# route itself fails.
OFF_TOPIC_REPLY = (
    "😅 Sorry, I couldn't answer that one just now — my connection to the "
    "outside world failed. I can always help with things inside this system, "
    "though.\n\n"
    "Try asking me:\n"
    "- What did I order last week?\n"
    "- How much did I spend this month?\n"
    "- What's today's exchange rate?\n"
    "- How do I create a food poll?\n\n"
    "សូមទោស ខ្ញុំមិនអាចឆ្លើយសំណួរនោះបានទេឥឡូវនេះ ប៉ុន្តែរឿងក្នុងប្រព័ន្ធ Food Bot "
    "— ការកុម្ម៉ង់ វិក្កយបត្រ poll និងអត្រាប្តូរប្រាក់ — ខ្ញុំអាចជួយបាន 😊"
)

PRIVACY_REPLY = (
    "🔒 Sorry, I can't share members' *personal details* — phone numbers, "
    "contact info and profile data are private.\n\n"
    "Orders, invoices and amounts I can help with, since the whole group "
    "sees those 😊\n\n"
    "សុំទោស ខ្ញុំមិនអាចបង្ហាញព័ត៌មានផ្ទាល់ខ្លួនរបស់សមាជិក (លេខទូរស័ព្ទ ។ល។) "
    "បានទេ ប៉ុន្តែការកុម្ម៉ង់ និងវិក្កយបត្រ ខ្ញុំអាចជួយបាន 😊"
)


# ---------------------------------------------------------------------------
# Step 1 — classify the question and name a reporting period.
# ---------------------------------------------------------------------------

async def classify_query(user_query: str, today_str: str, caller_name: str) -> Dict[str, Any]:
    """Route the question. Returns:
    {"type": "data"|"help"|"privacy"|"off_topic",
     "period": <one of PERIODS>, "start_date": ..., "end_date": ...}

    The model names a period; it does NOT do date arithmetic — `resolve_range`
    turns the name into bounds. Explicit dates are only for periods the enum
    can't express ("between May 3 and May 9"). Falls back to "data" over all
    time when the reply can't be parsed (safe: the fetch is chat-scoped
    regardless, and the answer states the period it used).
    """
    system_prompt = (
        "You are the query router for the Food Bot internal assistant "
        "(a Telegram lunch-ordering system).\n"
        f"Today's date is {today_str}. The person asking is: {caller_name}.\n\n"
        "Classify the user's message into exactly one type:\n"
        '- "data": they ask about orders, food history, spending, invoices/'
        "bills, amounts, polls or menus — their own OR any group member's "
        "(order and invoice data is shared with the whole group). E.g. "
        '"my orders", "how much did I spend", "what did Dara order", '
        '"who spent the most", "what was today\'s menu".\n'
        '- "help": greetings ("hi", "hello"), asking what you can do, or how to '
        "use the bot — its commands, features, polls, invoices or Mini App.\n"
        '- "privacy": they ask for someone\'s PERSONAL details — phone number, '
        'contact info, address, or profile data of another person. E.g. '
        '"Dara\'s phone number", "her contact info".\n'
        '- "external": anything outside this system that the public web can '
        "answer — recipes, cooking advice, general knowledge, news, weather, "
        "prices of things outside the group, coding, chit-chat.\n\n"
        "Also name the time period the question is about, using EXACTLY one "
        f"of these values: {', '.join(PERIODS)}.\n"
        'Use "all_time" when the message mentions no period at all. Do NOT '
        "calculate any dates — the system converts the period name into dates "
        "itself.\n"
        "Only when the period cannot be expressed by those values (e.g. "
        '"between May 3 and May 9", "in March") also fill start_date and '
        "end_date as YYYY-MM-DD; otherwise leave both null.\n\n"
        "Return ONLY a raw JSON object, no markdown, no explanation:\n"
        '{"type": "data", "period": "this_month", "start_date": null, "end_date": null}'
    )
    # A question about the exchange rate is answered from OUR stored NBC rate,
    # not the web — the group's invoices are priced at it, so a search result
    # quoting a market rate would contradict what people actually paid.
    if _EXCHANGE_RE.search(user_query or ""):
        return {"type": "data", "period": period_from_text(user_query) or "all_time",
                "start_date": None, "end_date": None}

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]

    try:
        raw = await _call_ollama(messages, json_mode=True, temperature=0.0)
        logger.info(f"AI router raw response: {raw}")
        result = _parse_json_object(raw)
        q_type = str(result.get("type") or "").strip().lower()
        if q_type == "off_topic":  # pre-web-search name for the same route
            q_type = "external"
        if q_type not in ("data", "help", "privacy", "external"):
            q_type = "data"
        period = str(result.get("period") or "").strip().lower()
        return {
            "type": q_type,
            "period": period if period in PERIODS else "all_time",
            "start_date": result.get("start_date") or None,
            "end_date": result.get("end_date") or None,
        }
    except Exception as e:
        logger.error(f"AI router failed, defaulting to 'data': {e}", exc_info=True)
        return {"type": "data", "period": "all_time", "start_date": None, "end_date": None}


# ---------------------------------------------------------------------------
# Step 2 — fetch the caller's own records (user / order / invoice / poll).
# ---------------------------------------------------------------------------

def _is_mine(entry_uid: Any, entry_name: Any, uid: str, names: set) -> bool:
    """An order-item / invoice-detail entry belongs to the caller when its
    user_id matches; entries without a user_id fall back to display name."""
    return is_same_person(entry_uid, entry_name, uid, names)


def _in_range(day: str, start: Optional[str], end: Optional[str]) -> bool:
    d = str(day or "")[:10]
    if not d:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


async def fetch_user_context(
    user_id: Any,
    username: str,
    full_name: str,
    start_date: Optional[str],
    end_date: Optional[str],
    range_label: str = "",
) -> Dict[str, Any]:
    """Everything the model may see. Orders, invoices and polls are
    group-shared (the bot posts them to the whole chat), so they include
    every member's lines — but ONLY for chats the caller belongs to. The
    only profile row fetched is the caller's own, so personal details of
    other members can never reach the prompt. Tabs consulted: user, order,
    invoice, poll.

    Every amount the answer could need is precomputed here — the caller's
    total, each person's total, and a per-day series — so the model never
    has to add anything up, including for a sub-range of the fetched period.
    """
    uid = str(user_id).strip()
    names = name_variants(username=username, full_name=full_name)

    context: Dict[str, Any] = {
        "date_range": {
            "label": range_label or _range_label(start_date, end_date),
            "from": start_date or "(all time)",
            "to": end_date or "(today)",
        },
        "profile": {},
        "exchange_rate": None,
        "caller_total_invoiced": 0.0,
        "caller_total_khr": None,
        "group_total_invoiced": 0.0,
        "group_total_khr": None,
        "totals_by_person": [],
        "by_day": [],
        "uninvoiced_order_days": [],
        "group_orders": [],
        "group_invoices": [],
        "group_polls": [],
        "truncated": {},
    }
    if not is_configured():
        return context

    # user — the caller's own row only.
    row = await repo.find_by_pk("user", user_id) or {}
    if row:
        context["profile"] = {
            "username": str(row.get("username", "") or ""),
            "full_name": str(row.get("full_name", "") or ""),
            "role": str(row.get("role", "") or ""),
            "language": str(row.get("language", "") or ""),
            "member_since": str(row.get("created_at", "") or "")[:10],
        }

    # Chats the caller belongs to — every block below is scoped to them.
    # Lazy import: bot.api.__init__ imports bot.api.ai which imports this
    # module — a top-level import of bot.api.members here would be circular.
    from .api.members import user_chats
    chats = await user_chats(uid)

    # Read both sources up front: the per-person totals need a first pass
    # over everything to work out which display names belong to which
    # user_id (see below), so the rows get walked twice.
    order_rows = [
        o for o in await sheets_orders.list_in_range(start_date, end_date)
        if str(o.get("chat_id", "") or "").strip() in chats
    ]
    invoice_rows = [
        inv for inv in await sheets_invoices.list_all()
        if str(inv.get("chat_id", "") or "").strip() in chats
        and _in_range(inv.get("order_date", ""), start_date, end_date)
    ]

    def _order_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            items = json.loads(order.get("item") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        return [it for it in (items or []) if isinstance(it, dict)]

    # Pass 1 — name → user_id. Guest and pre-user_id rows identify a person
    # by display name only; without this they'd form a SECOND bucket next to
    # that person's modern rows and split their spending in half.
    uid_index = build_uid_index(
        [(it.get("user_id"), it.get("name"))
         for o in order_rows for it in _order_items(o)]
        + [(d.get("user_id"), d.get("user_name"))
           for inv in invoice_rows for d in (inv.get("details") or [])]
    )

    # Per-person aggregates. These are the AUTHORITATIVE numbers the model
    # quotes for any amount/count question — LLM arithmetic over long lists
    # is unreliable.
    people: Dict[str, Dict[str, Any]] = {}

    def _person(entry_uid: Any, entry_name: Any, day: str = "") -> Dict[str, Any]:
        name = strip_invisible(entry_name) or "Guest"
        mine = _is_mine(entry_uid, entry_name, uid, names)
        # The caller is always exactly ONE bucket. uid_index can only merge a
        # legacy row when some other row pairs that exact name with a user_id
        # ("Dara" stays split from "Dara Kim"), but for the caller we know the
        # id — and "how much did I spend" must not be answered from a half.
        key = uid if (mine and uid) else person_key(entry_uid, entry_name, uid_index)
        slot = people.get(key)
        if not slot:
            slot = people[key] = {
                "name": name, "me": mine, "name_rank": (False, ""),
                "items_ordered": 0, "order_days": set(), "invoiced_days": set(),
                "invoiced_amount": 0.0,
            }
        # Which of a merged person's display names to show: the newest one
        # that came with a user_id, falling back to the newest legacy name.
        # Ranking it (rather than "last write wins") keeps the label stable
        # no matter what order the sheet returns rows in.
        rank = (bool(str(entry_uid or "").strip()), str(day or ""))
        if rank >= slot["name_rank"]:
            slot["name"] = name
            slot["name_rank"] = rank
        slot["me"] = slot["me"] or mine
        return slot

    # Per-day series — lets the model answer about any sub-range of the
    # fetched period (a single day, "Monday", "the first week") by reading
    # one row instead of summing invoice lines.
    days: Dict[str, Dict[str, Any]] = {}

    def _day(day: str) -> Dict[str, Any]:
        return days.setdefault(day, {
            "date": day, "my_amount": 0.0, "group_amount": 0.0,
            "invoiced": False, "my_items": [],
        })

    # order — every member's item lines in the caller's chats; the caller's
    # own lines are tagged "me" so "my orders" stays unambiguous.
    for o in order_rows:
        day = str(o.get("order_date", "") or "")
        for it in _order_items(o):
            try:
                qty = int(it.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            item_name = strip_invisible(it.get("item_name", ""))
            line: Dict[str, Any] = {
                "order_date": day,
                "name": strip_invisible(it.get("name", "")),
                "item_name": item_name,
                "qty": qty,
            }
            mine = _is_mine(it.get("user_id"), it.get("name"), uid, names)
            if mine:
                line["me"] = True
                if day:
                    _day(day)["my_items"].append(f"{item_name} x{qty}")
            context["group_orders"].append(line)

            slot = _person(it.get("user_id"), it.get("name"), day)
            slot["items_ordered"] += qty
            if day:
                slot["order_days"].add(day)

    # invoice — the full per-person breakdown, exactly as posted to the
    # group chat (shared knowledge within the group).
    for inv in invoice_rows:
        day = str(inv.get("order_date", "") or "")
        entries = []
        for d in inv.get("details") or []:
            subtotal = _money(d.get("subtotal"))
            person: Dict[str, Any] = {
                "name": strip_invisible(d.get("user_name", "")),
                "items": [
                    {
                        "item_name": strip_invisible(i.get("item_name", "")),
                        "qty": i.get("qty", 0),
                        "price": _money(i.get("price")),
                        "cost": _money(i.get("cost")),
                    }
                    for i in d.get("items") or []
                ],
                "subtotal": subtotal,
            }
            mine = _is_mine(d.get("user_id"), d.get("user_name"), uid, names)
            if mine:
                person["me"] = True
            entries.append(person)

            slot = _person(d.get("user_id"), d.get("user_name"), day)
            slot["invoiced_amount"] += subtotal
            if day:
                slot["invoiced_days"].add(day)

            if day:
                bucket = _day(day)
                bucket["invoiced"] = True
                bucket["group_amount"] = round(bucket["group_amount"] + subtotal, 2)
                if mine:
                    bucket["my_amount"] = round(bucket["my_amount"] + subtotal, 2)

        context["group_invoices"].append({
            "order_date": day,
            "people": entries,
            # Sum of this day's per-person subtotals. Deliberately NOT the
            # stored `total` column: that one is unrounded, so the two
            # disagree by cents and the model can't tell which is real.
            "day_total": round(sum(p["subtotal"] for p in entries), 2),
            "payer_name": strip_invisible(inv.get("payer_name", "")),
        })

    # poll — question/options/status of polls in the caller's own group(s).
    polls = []
    for p in await repo.list_all("poll"):
        if str(p.get("chat_id", "") or "").strip() not in chats:
            continue
        day = str(p.get("created_at", "") or "")[:10]
        if not _in_range(day, start_date, end_date):
            continue
        try:
            options = json.loads(p.get("options") or "[]")
        except (json.JSONDecodeError, TypeError):
            options = []
        polls.append({
            "date": day,
            "question": strip_invisible(p.get("question", "")),
            "options": options,
            "status": str(p.get("status", "") or ""),
        })
    polls.sort(key=lambda x: x["date"], reverse=True)
    context["group_polls"] = polls[:MAX_POLLS]

    # Bound prompt growth: newest first, capped. Aggregates above are
    # already computed over everything, so this can't move an amount — but
    # record what was dropped so the model doesn't call a cut list complete.
    context["group_orders"].sort(key=lambda x: x["order_date"], reverse=True)
    context["group_invoices"].sort(key=lambda x: x["order_date"], reverse=True)
    context["truncated"] = {
        "group_orders_hidden": max(0, len(context["group_orders"]) - MAX_ORDER_LINES),
        "group_invoices_hidden": max(0, len(context["group_invoices"]) - MAX_INVOICES),
    }
    context["group_orders"] = context["group_orders"][:MAX_ORDER_LINES]
    context["group_invoices"] = context["group_invoices"][:MAX_INVOICES]

    # Serialize the aggregates (day sets → counts), biggest spender first.
    context["totals_by_person"] = sorted(
        (
            {
                "name": s["name"],
                **({"me": True} if s["me"] else {}),
                "items_ordered": s["items_ordered"],
                "days_ordered": len(s["order_days"]),
                "days_invoiced": len(s["invoiced_days"]),
                "invoiced_amount": round(s["invoiced_amount"], 2),
            }
            for s in people.values()
        ),
        key=lambda x: x["invoiced_amount"],
        reverse=True,
    )
    context["caller_total_invoiced"] = round(
        sum(s["invoiced_amount"] for s in people.values() if s["me"]), 2
    )
    context["group_total_invoiced"] = round(
        sum(s["invoiced_amount"] for s in people.values()), 2
    )

    # Riel equivalents, precomputed at the current NBC rate. The model must
    # never multiply by the rate itself — 4047 × 12.25 is exactly the kind of
    # arithmetic it gets subtly wrong.
    rate_row = await exchange.current()
    if rate_row:
        usd_khr = rate_row["usd_khr"]
        context["exchange_rate"] = {
            "rate_date": rate_row["rate_date"],
            "usd_khr": usd_khr,
            "display": exchange.format_rate(usd_khr),
            "source": "National Bank of Cambodia (official)",
        }
        context["caller_total_khr"] = exchange.to_khr(
            context["caller_total_invoiced"], usd_khr)
        context["group_total_khr"] = exchange.to_khr(
            context["group_total_invoiced"], usd_khr)
        for person in context["totals_by_person"]:
            person["invoiced_amount_khr"] = exchange.to_khr(
                person["invoiced_amount"], usd_khr)
        for bucket in days.values():
            bucket["my_amount_khr"] = exchange.to_khr(bucket["my_amount"], usd_khr)

    context["by_day"] = sorted(
        days.values(), key=lambda d: d["date"], reverse=True
    )[:MAX_DAYS]
    context["uninvoiced_order_days"] = sorted(
        (d["date"] for d in days.values() if d["my_items"] and not d["invoiced"]),
        reverse=True,
    )

    return context


# ---------------------------------------------------------------------------
# Step 3 — generate the grounded, friendly answer.
# ---------------------------------------------------------------------------

def _system_prompt(today_str: str, caller_name: str,
                   context: Optional[Dict[str, Any]]) -> str:
    prompt = (
        "You are the friendly assistant inside the *Food Bot* — an internal "
        "Telegram lunch-ordering system for a Khmer/English-speaking team.\n"
        f"Today's date is {today_str}. You are talking to: {caller_name}.\n\n"
        "STRICT RULES\n"
        "1. INTERNAL ONLY: answer using nothing but the FEATURE GUIDE and the "
        "DATA below. Never answer general questions (recipes, news, coding, "
        "world facts) — instead say, warmly, what you CAN help with.\n"
        "2. NEVER DO ARITHMETIC. Every amount you might need is already "
        "computed in the DATA. Read the right field, copy it exactly, and do "
        "not add, subtract or re-total anything:\n"
        "   - what the person asking spent → `caller_total_invoiced`\n"
        "   - what the whole group spent → `group_total_invoiced`\n"
        "   - what one named member spent → their `invoiced_amount` in "
        "`totals_by_person`\n"
        "   - one day, or a stretch shorter than the period → the matching "
        "`by_day` entries (`my_amount` is the caller's, `group_amount` is "
        "everyone's). Adding a handful of by_day rows is the ONLY sum you may "
        "ever do; never sum `group_invoices` or `group_orders`.\n"
        "   - use `group_orders` / `group_invoices` only to say WHICH dishes "
        "or dates, never to work out a number.\n"
        "   - RIEL (KHR): use the ready-made `*_khr` fields — "
        "`caller_total_khr`, `group_total_khr`, `invoiced_amount_khr`, "
        "`my_amount_khr`. NEVER multiply dollars by the rate yourself. Write "
        "riel with thousands separators and the ៛ sign, e.g. 16,200៛. If a "
        "`*_khr` field is missing, give the dollar amount only.\n"
        "   - asked for the exchange rate itself → `exchange_rate.display` "
        "and `exchange_rate.rate_date`. It is the National Bank of Cambodia's "
        "official rate; say which date it was published on, since NBC doesn't "
        "publish at weekends or on public holidays.\n"
        "3. ALWAYS STATE THE PERIOD you used, in words — it is "
        "`date_range.label` (exact dates in `date_range.from`/`to`). If the "
        "question asked about a different period than the one in the DATA, "
        "say plainly which period your figure actually covers rather than "
        "pretending it matches.\n"
        "4. INCOMPLETE AMOUNTS: a day only has money once its invoice was "
        "generated. If `uninvoiced_order_days` is not empty, say the total "
        "excludes those day(s) and name them. If `days_invoiced` is lower "
        "than `days_ordered` for the person being asked about, mention it.\n"
        "5. PRIVACY: the data covers only this user's own group(s). Orders, "
        "invoices and amounts are group-shared (the bot posts invoices to the "
        "whole chat), so you may answer about any member who appears in the "
        "data. But NEVER share personal details — phone numbers, contact or "
        "profile info — of anyone except the caller's own profile below, and "
        "refuse questions about people or groups not present in the data.\n"
        "6. Never invent dishes, prices, dates or numbers that are not in the "
        "data. If the data has nothing for their question, say so kindly. If "
        "`truncated` shows hidden rows, don't present a list as complete.\n"
        "7. Reply in the same language as the user's MESSAGE — English message "
        "→ English answer, Khmer message → Khmer answer. Ignore the profile "
        "language setting for this.\n"
        "8. Be warm, friendly and concise — a light emoji is welcome 😋\n\n"
        "FORMATTING (Telegram Markdown v1)\n"
        "- *bold* with single asterisks (never **), _italic_ with underscores.\n"
        "- Money always with 2 decimals, e.g. $3.50.\n"
        "- No tables. Use bullet lines like \"- dish ×2   $3.50\".\n\n"
        f"FEATURE GUIDE (how this bot works)\n{FEATURE_GUIDE}\n"
    )
    if context is not None:
        def _j(key: str, default: Any) -> str:
            return json.dumps(context.get(key, default), ensure_ascii=False)

        prompt += (
            "\nDATA — live records from the caller's group(s), newest first. "
            "Entries tagged \"me\": true belong to the caller.\n"
            f"date_range (the period ALL numbers below cover): {_j('date_range', {})}\n"
            "-- AUTHORITATIVE NUMBERS (copy these; never recompute) --\n"
            f"exchange_rate (NBC official USD→KHR, used for every riel figure "
            f"below): {_j('exchange_rate', None)}\n"
            f"caller_total_invoiced (what {caller_name} spent in this period): "
            f"{context.get('caller_total_invoiced', 0)}  "
            f"= caller_total_khr: {context.get('caller_total_khr')}\n"
            f"group_total_invoiced (what the WHOLE group spent, not the caller): "
            f"{context.get('group_total_invoiced', 0)}  "
            f"= group_total_khr: {context.get('group_total_khr')}\n"
            f"totals_by_person: {_j('totals_by_person', [])}\n"
            f"by_day (per-date amounts): {_j('by_day', [])}\n"
            f"uninvoiced_order_days (caller ordered, no invoice yet → $0 counted): "
            f"{_j('uninvoiced_order_days', [])}\n"
            "-- DETAIL (for names, dishes and dates only) --\n"
            f"profile (the caller's own): {_j('profile', {})}\n"
            f"group_orders: {_j('group_orders', [])}\n"
            f"group_invoices: {_j('group_invoices', [])}\n"
            f"group_polls: {_j('group_polls', [])}\n"
            f"truncated (rows hidden from the lists above): {_j('truncated', {})}\n"
        )
    return prompt


def _external_system_prompt(today_str: str, caller_name: str,
                            results: List[Dict[str, str]]) -> str:
    """Prompt for questions the public web answers, not our spreadsheet."""
    prompt = (
        "You are the friendly assistant inside the *Food Bot*, a Telegram "
        "lunch-ordering system for a Khmer/English-speaking team in Cambodia. "
        "This question is about the outside world rather than the group's own "
        "orders, so answer it helpfully and briefly.\n"
        f"Today's date is {today_str}. You are talking to: {caller_name}.\n\n"
        "RULES\n"
        "1. Answer the question directly and concisely.\n"
        "2. If SEARCH RESULTS are given below, base the answer on them and "
        "cite the source with a bare URL on its own line at the end. If they "
        "don't actually cover the question, say what you do know and be clear "
        "about what you couldn't confirm.\n"
        "3. Without search results, answer from your own knowledge and say "
        "you couldn't check the web just now. Never present a guess as fact, "
        "and be explicit that anything time-sensitive (prices, news, weather) "
        "may be out of date.\n"
        "4. NEVER state figures about this group's orders, invoices, members "
        "or spending here — those come from our own records, not the web. If "
        "they want those, tell them to ask about their orders directly.\n"
        "5. For the USD/KHR exchange rate, do NOT use search results: the "
        "group's invoices are priced at the National Bank of Cambodia rate we "
        "store. Tell them to use /exchange_rate.\n"
        "6. Reply in the same language as the user's MESSAGE — English → "
        "English, Khmer → Khmer.\n"
        "7. Be warm and concise; a light emoji is welcome 😊\n\n"
        "FORMATTING (Telegram Markdown v1)\n"
        "- *bold* with single asterisks (never **), _italic_ with underscores.\n"
        "- No tables.\n"
    )
    if results:
        prompt += "\nSEARCH RESULTS (from the public web, newest search):\n"
        for i, r in enumerate(results, 1):
            prompt += (
                f"\n[{i}] {r['title']}\n{r['url']}\n{r['content']}\n"
            )
    else:
        prompt += "\nSEARCH RESULTS: none available — the web search did not run.\n"
    return prompt


async def generate_external_answer(
    user_query: str,
    today_str: str,
    caller_name: str,
    results: List[Dict[str, str]],
) -> str:
    """Answer an outside-world question, grounded on search results if any."""
    messages = [
        {"role": "system", "content": _external_system_prompt(today_str, caller_name, results)},
        {"role": "user", "content": user_query},
    ]
    response = await _call_ollama(messages, temperature=0.4)
    return response.replace("**", "*")


async def generate_answer(
    user_query: str,
    today_str: str,
    caller_name: str,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Grounded generation. context=None → help mode (feature guide only)."""
    messages = [
        {"role": "system", "content": _system_prompt(today_str, caller_name, context)},
        {"role": "user", "content": user_query},
    ]
    # Low temperature: this answer transcribes precomputed figures, and
    # sampling noise shows up as mistyped amounts.
    response = await _call_ollama(messages, temperature=0.2)
    # Ensure double asterisks from LLM are safe for Telegram Markdown v1
    return response.replace("**", "*")


# ---------------------------------------------------------------------------
# Entry point shared by /api/ai and the /ai Telegram command.
# ---------------------------------------------------------------------------

async def answer_query(user_query: str, user_info: Dict[str, Any]) -> Dict[str, str]:
    """Full pipeline. user_info needs: id, username, full_name.
    Returns {"response": <text>, "query_type": <route>}."""
    today = _today()
    today_str = today.isoformat()
    username = str(user_info.get("username") or "")
    full_name = str(user_info.get("full_name") or "") or f"User{user_info.get('id')}"
    caller_name = f"{full_name} (@{username})" if username else full_name

    intent = await classify_query(user_query, today_str, caller_name)
    q_type = intent["type"]

    if q_type == "privacy":
        return {"response": PRIVACY_REPLY, "query_type": q_type}

    if q_type == "external":
        # Outside the spreadsheet: search the public web and answer from that.
        # Group data is never mixed in here (see _external_system_prompt).
        results = await web_search(user_query)
        try:
            reply = await generate_external_answer(
                user_query, today_str, caller_name, results
            )
        except Exception as e:
            logger.error(f"External answer failed: {e}", exc_info=True)
            return {"response": OFF_TOPIC_REPLY, "query_type": q_type}
        return {"response": reply, "query_type": q_type}

    context = None
    if q_type == "data":
        start_date, end_date, label = resolve_range(user_query, intent, today)
        context = await fetch_user_context(
            user_info.get("id"), username, full_name,
            start_date, end_date, range_label=label,
        )
        logger.info(
            "AI data context: period=%r (%s..%s) — %d order lines, %d invoices, "
            "%d polls, %d people, %d days; caller_total=%s group_total=%s",
            label, start_date, end_date,
            len(context["group_orders"]), len(context["group_invoices"]),
            len(context["group_polls"]), len(context["totals_by_person"]),
            len(context["by_day"]),
            context["caller_total_invoiced"], context["group_total_invoiced"],
        )

    reply = await generate_answer(user_query, today_str, caller_name, context)
    return {"response": reply, "query_type": q_type}
