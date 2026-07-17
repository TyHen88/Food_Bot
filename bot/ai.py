"""
AI assistant for the Food Bot — internal-only, data-grounded.

One pipeline shared by the Mini App endpoint (bot/api/ai.py) and the
Telegram /ai command (bot/handlers.py): classify the question, fetch the
caller's OWN records from the four allowed tabs (user, order, invoice,
poll), then generate a friendly grounded answer via Ollama.

Hard boundaries, enforced here rather than trusted to the model:
    - Internal only: general questions (recipes, news, ...) get a canned
      friendly redirect — the LLM is never asked to answer them.
    - Privacy: orders/invoices/polls are GROUP-shared (invoices are posted
      to the whole chat, so every member already sees who ordered what) —
      but only for chats the caller belongs to; other groups' rows never
      reach the prompt. Personal profile details (phone numbers etc.) are
      never fetched for anyone but the caller, and questions about another
      member's personal info get a canned refusal.
    - Data sources are limited to user / order / invoice / poll. No
      settings, schedules, history or payer data reaches the model.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import httpx

from .config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, TIMEZONE
from .sheets import invoices as sheets_invoices
from .sheets import orders as sheets_orders
from .sheets import repo
from .sheets.client import is_configured

logger = logging.getLogger(__name__)


async def _call_ollama(messages: list) -> str:
    """Make an async call to the Ollama /api/chat endpoint."""
    if not OLLAMA_API_URL:
        raise ValueError("OLLAMA_API_URL is not configured.")

    headers = {}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }

    logger.info(f"Calling Ollama at {OLLAMA_API_URL} with model {OLLAMA_MODEL}...")
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(OLLAMA_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        # Ollama API structure for chat has messages:
        # {"message": {"role": "assistant", "content": "..."}}
        content = data.get("message", {}).get("content", "")
        return content.strip()


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


def _today_str() -> str:
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Static knowledge: what this bot can do (the "help" grounding).
# ---------------------------------------------------------------------------

FEATURE_GUIDE = """HOW ORDERING WORKS
1. Post a numbered food menu in the group (lines starting 1-6 or ១-៦, or a message beginning with "ម្ហូបថ្ងៃ") — the bot instantly turns it into a poll.
2. Members vote for the dishes they want (several choices allowed; voting again updates your picks).
3. Tap the "Order" button under the poll to post the live order summary; "Close Order" locks it. At the daily cutoff time the votes are snapshotted into that day's order automatically.
4. From the Orders page in the Mini App, an admin enters prices and sends the invoice to the group — it lists each person's dishes, their share, the grand total, and the payer's KHQR to scan.

COMMANDS (everyone)
/start — welcome & instructions · /app — open the Mini App · /subscribe and /unsubscribe — turn the daily reminder on/off for the chat · /vongsa — Vongsa's payment KHQR · /ty — Ty's payment KHQR · /ai <question> — ask this assistant directly in chat
COMMANDS (admins)
/admin — open the admin panel · /set — update a setting · /schedule_list, /schedule_enable, /schedule_disable — manage scheduled reminders

MINI APP PAGES
Dashboard (calendar of order days + quick actions, incl. this AI) · Orders (per-day orders with date picker, search, generate/view invoice) · Invoices (history with date-range filter and Total / My Amount cards) · Members · Admin only: Templates (menu presets), Schedule (reminders), Settings, History (audit log)"""


# Canned replies — deterministic, bilingual, never sent through the LLM.
OFF_TOPIC_REPLY = (
    "😊 I'm the Food Bot assistant, so I can only help with things inside this "
    "system — your orders, your invoices, the food polls, and how to use the bot.\n\n"
    "Try asking me:\n"
    "- What did I order last week?\n"
    "- How much did I spend this month?\n"
    "- How do I create a food poll?\n\n"
    "ខ្ញុំអាចជួយបានតែរឿងក្នុងប្រព័ន្ធ Food Bot ប៉ុណ្ណោះ — ការកុម្ម៉ង់ វិក្កយបត្រ "
    "poll និងរបៀបប្រើ bot 😊"
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
# Step 1 — classify the question and extract a date range.
# ---------------------------------------------------------------------------

async def classify_query(user_query: str, today_str: str, caller_name: str) -> Dict[str, Any]:
    """Route the question. Returns:
    {"type": "data"|"help"|"privacy"|"off_topic",
     "start_date": "YYYY-MM-DD"|None, "end_date": "YYYY-MM-DD"|None}
    Falls back to "data" with no range when the model output can't be parsed
    (safe: the data fetch is caller-scoped regardless).
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
        '- "off_topic": anything else — recipes, cooking advice, general '
        "knowledge, news, coding, chit-chat unrelated to this system.\n\n"
        "Also extract an inclusive date range if the message mentions one, "
        "resolving relative dates (today, yesterday, last week, this month, "
        "may-01, ...) against today's date. Use null when no date is mentioned.\n\n"
        "Return ONLY a raw JSON object, no markdown, no explanation:\n"
        '{"type": "data", "start_date": "YYYY-MM-DD" or null, "end_date": "YYYY-MM-DD" or null}'
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]

    try:
        raw = await _call_ollama(messages)
        logger.info(f"AI router raw response: {raw}")
        result = json.loads(_clean_json_response(raw))
        q_type = str(result.get("type") or "").strip().lower()
        if q_type not in ("data", "help", "privacy", "off_topic"):
            q_type = "data"
        return {
            "type": q_type,
            "start_date": result.get("start_date") or None,
            "end_date": result.get("end_date") or None,
        }
    except Exception as e:
        logger.error(f"AI router failed, defaulting to 'data': {e}", exc_info=True)
        return {"type": "data", "start_date": None, "end_date": None}


# ---------------------------------------------------------------------------
# Step 2 — fetch the caller's own records (user / order / invoice / poll).
# ---------------------------------------------------------------------------

def _is_mine(entry_uid: Any, entry_name: Any, uid: str, names: set) -> bool:
    """An order-item / invoice-detail entry belongs to the caller when its
    user_id matches; entries without a user_id fall back to display name."""
    euid = str(entry_uid or "").strip()
    if euid:
        return euid == uid
    return str(entry_name or "").strip() in names


def _in_range(day: str, start: Optional[str], end: Optional[str]) -> bool:
    d = str(day or "")[:10]
    if not d:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


async def fetch_user_context(
    user_id: Any,
    username: str,
    full_name: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, Any]:
    """Everything the model may see. Orders, invoices and polls are
    group-shared (the bot posts them to the whole chat), so they include
    every member's lines — but ONLY for chats the caller belongs to. The
    only profile row fetched is the caller's own, so personal details of
    other members can never reach the prompt. Tabs consulted: user, order,
    invoice, poll."""
    uid = str(user_id).strip()
    names = {n.strip() for n in (username, full_name) if n and n.strip()}

    context: Dict[str, Any] = {
        "profile": {},
        "group_orders": [],
        "group_invoices": [],
        "group_polls": [],
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

    # Per-person aggregates, keyed by user_id (fallback: display name).
    # These are the AUTHORITATIVE numbers the model quotes for any
    # amount/count question — LLM arithmetic over long lists is unreliable.
    people: Dict[str, Dict[str, Any]] = {}

    def _person(entry_uid: Any, entry_name: Any) -> Dict[str, Any]:
        name = str(entry_name or "Guest").strip() or "Guest"
        key = str(entry_uid or "").strip() or name
        slot = people.get(key)
        if not slot:
            slot = people[key] = {
                "name": name, "me": _is_mine(entry_uid, entry_name, uid, names),
                "items_ordered": 0, "order_days": set(), "invoiced_amount": 0.0,
            }
        slot["name"] = name  # newest display name wins
        return slot

    # order — every member's item lines in the caller's chats; the caller's
    # own lines are tagged "me" so "my orders" stays unambiguous.
    for o in await sheets_orders.list_in_range(start_date, end_date):
        if str(o.get("chat_id", "") or "").strip() not in chats:
            continue
        try:
            items = json.loads(o.get("item") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        day = str(o.get("order_date", "") or "")
        for it in items or []:
            it = it or {}
            try:
                qty = int(it.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            line: Dict[str, Any] = {
                "order_date": day,
                "name": str(it.get("name", "") or ""),
                "item_name": str(it.get("item_name", "") or ""),
                "qty": qty,
            }
            if _is_mine(it.get("user_id"), it.get("name"), uid, names):
                line["me"] = True
            context["group_orders"].append(line)

            slot = _person(it.get("user_id"), it.get("name"))
            slot["items_ordered"] += qty
            slot["order_days"].add(day)

    # invoice — the full per-person breakdown, exactly as posted to the
    # group chat (shared knowledge within the group).
    for inv in await sheets_invoices.list_all():
        if str(inv.get("chat_id", "") or "").strip() not in chats:
            continue
        if not _in_range(inv.get("order_date", ""), start_date, end_date):
            continue
        entries = []
        for d in inv.get("details") or []:
            try:
                subtotal = float(d.get("subtotal") or 0)
            except (TypeError, ValueError):
                subtotal = 0.0
            person: Dict[str, Any] = {
                "name": str(d.get("user_name", "") or ""),
                "items": [
                    {
                        "item_name": i.get("item_name", ""),
                        "qty": i.get("qty", 0),
                        "price": i.get("price", 0),
                        "cost": i.get("cost", 0),
                    }
                    for i in d.get("items") or []
                ],
                "subtotal": subtotal,
            }
            if _is_mine(d.get("user_id"), d.get("user_name"), uid, names):
                person["me"] = True
            entries.append(person)

            slot = _person(d.get("user_id"), d.get("user_name"))
            slot["invoiced_amount"] += subtotal
        context["group_invoices"].append({
            "order_date": inv.get("order_date", ""),
            "people": entries,
            "invoice_total": inv.get("total", 0),
            "payer_name": inv.get("payer_name", ""),
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
            "question": str(p.get("question", "") or ""),
            "options": options,
            "status": str(p.get("status", "") or ""),
        })
    polls.sort(key=lambda x: x["date"], reverse=True)
    context["group_polls"] = polls[:60]

    # Bound prompt growth: newest first, capped.
    context["group_orders"].sort(key=lambda x: x["order_date"], reverse=True)
    context["group_orders"] = context["group_orders"][:400]
    context["group_invoices"].sort(key=lambda x: x["order_date"], reverse=True)
    context["group_invoices"] = context["group_invoices"][:100]

    # Serialize the aggregates (order_days set → count), biggest spender first.
    context["totals_by_person"] = sorted(
        (
            {
                "name": s["name"],
                **({"me": True} if s["me"] else {}),
                "items_ordered": s["items_ordered"],
                "days_ordered": len(s["order_days"]),
                "invoiced_amount": round(s["invoiced_amount"], 2),
            }
            for s in people.values()
        ),
        key=lambda x: x["invoiced_amount"],
        reverse=True,
    )
    context["grand_total_invoiced"] = round(
        sum(s["invoiced_amount"] for s in people.values()), 2
    )
    context["date_range"] = {
        "from": start_date or "(all time)",
        "to": end_date or "(today)",
    }

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
        "2. ACCURACY: for ANY question about amounts, totals, spending or "
        "counts, quote the precomputed numbers in `totals_by_person` and "
        "`grand_total_invoiced` — never add up list rows yourself. Use the "
        "detailed lists only to say WHICH dishes/dates, not to compute sums.\n"
        "3. PRIVACY: the data covers only this user's own group(s). Orders, "
        "invoices and amounts are group-shared (the bot posts invoices to the "
        "whole chat), so you may answer about any member who appears in the "
        "data. But NEVER share personal details — phone numbers, contact or "
        "profile info — of anyone except the caller's own profile below, and "
        "refuse questions about people or groups not present in the data.\n"
        "4. Never invent dishes, prices, dates or numbers that are not in the "
        "data. If the data has nothing for their question, say so kindly.\n"
        "5. Reply in the same language as the user's MESSAGE — English message "
        "→ English answer, Khmer message → Khmer answer. Ignore the profile "
        "language setting for this.\n"
        "6. Be warm, friendly and concise — a light emoji is welcome 😋\n\n"
        "FORMATTING (Telegram Markdown v1)\n"
        "- *bold* with single asterisks (never **), _italic_ with underscores.\n"
        "- No tables. Use bullet lines like \"- dish ×2   $3.50\".\n\n"
        f"FEATURE GUIDE (how this bot works)\n{FEATURE_GUIDE}\n"
    )
    if context is not None:
        prompt += (
            "\nDATA — live records from the caller's group(s), newest first. "
            "Entries tagged \"me\": true belong to the caller.\n"
            f"date_range: {json.dumps(context.get('date_range') or {}, ensure_ascii=False)}\n"
            "totals_by_person (AUTHORITATIVE for amounts & counts): "
            f"{json.dumps(context.get('totals_by_person') or [], ensure_ascii=False)}\n"
            f"grand_total_invoiced: {context.get('grand_total_invoiced', 0)}\n"
            f"profile (the caller's own): {json.dumps(context.get('profile') or {}, ensure_ascii=False)}\n"
            f"group_orders: {json.dumps(context.get('group_orders') or [], ensure_ascii=False)}\n"
            f"group_invoices: {json.dumps(context.get('group_invoices') or [], ensure_ascii=False)}\n"
            f"group_polls: {json.dumps(context.get('group_polls') or [], ensure_ascii=False)}\n"
        )
    return prompt


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
    response = await _call_ollama(messages)
    # Ensure double asterisks from LLM are safe for Telegram Markdown v1
    return response.replace("**", "*")


# ---------------------------------------------------------------------------
# Entry point shared by /api/ai and the /ai Telegram command.
# ---------------------------------------------------------------------------

async def answer_query(user_query: str, user_info: Dict[str, Any]) -> Dict[str, str]:
    """Full pipeline. user_info needs: id, username, full_name.
    Returns {"response": <text>, "query_type": <route>}."""
    today_str = _today_str()
    username = str(user_info.get("username") or "")
    full_name = str(user_info.get("full_name") or "") or f"User{user_info.get('id')}"
    caller_name = f"{full_name} (@{username})" if username else full_name

    intent = await classify_query(user_query, today_str, caller_name)
    q_type = intent["type"]

    if q_type == "off_topic":
        return {"response": OFF_TOPIC_REPLY, "query_type": q_type}
    if q_type == "privacy":
        return {"response": PRIVACY_REPLY, "query_type": q_type}

    context = None
    if q_type == "data":
        context = await fetch_user_context(
            user_info.get("id"), username, full_name,
            intent.get("start_date"), intent.get("end_date"),
        )
        logger.info(
            "AI data context: %d order lines, %d invoices, %d polls, %d people (range %s..%s)",
            len(context["group_orders"]), len(context["group_invoices"]),
            len(context["group_polls"]), len(context["totals_by_person"]),
            intent.get("start_date"), intent.get("end_date"),
        )

    reply = await generate_answer(user_query, today_str, caller_name, context)
    return {"response": reply, "query_type": q_type}
