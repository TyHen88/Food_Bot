"""
Single source of truth for Google Sheets layout.

Adding a column?  Update TABS here, then bump SCHEMA_VERSION.
On startup bootstrap.py reads this and idempotently adds missing
tabs/headers — no manual sheet editing required.

Conventions (see PLAN.md "Sheets design rules"):
    - First column of each tab is the primary key.
    - Boolean cells store "TRUE" / "FALSE" strings (Google Sheets native).
    - Timestamps are ISO 8601 with offset, e.g. 2026-05-20T08:00:00+07:00.
    - Array values are JSON-stringified (e.g. options: ["a","b"]).
    - Soft delete via is_active=FALSE; never hard-delete rows that have history.
"""

from typing import Dict, List

SCHEMA_VERSION = 10

# Tab name → ordered list of column headers.
# Order matters: first column is the PK; bootstrap.py writes headers in this order.
TABS: Dict[str, List[str]] = {
    "common_code": [
        "group_code", "code", "label_en", "label_kh",
        "sort_order", "is_active", "description",
    ],
    "setting": [
        "key", "value", "value_type", "description",
        "updated_at", "updated_by",
    ],
    "chat_setting": [
        # Per-chat overrides of `setting` keys (e.g. ORDER_SUMMARY_STYLE so each
        # group picks its own order-summary template). PK `id` = "<chat_id>:<key>".
        # Reads fall back to the global `setting` value when no row exists.
        "id", "chat_id", "key", "value", "updated_at", "updated_by",
    ],
    "user": [
        # chat_id records the most recent chat the user interacted in, so
        # members can be looked up by chat without deriving from votes/orders.
        # bank_name stores the ABA Bank Account name for automatic payment matching.
        "user_id", "username", "full_name", "bank_name", "phone_number", "chat_id",
        "role", "language",
        "dietary_notes", "created_at", "last_active_at",
    ],
    "chat": [
        "chat_id", "title", "type", "is_subscribed",
        "subscribed_at", "subscribed_by",
    ],
    "schedule": [
        # action_type/payload are legacy (TEXT/QR_PHOTO). New schedules use
        # message_text + image (a Telegram file_id from upload, or an assets/
        # filename) and send whichever is set. run_date set => one-time on that
        # date (DateTrigger); empty => recurring weekly via days_of_week.
        "schedule_id", "name", "action_type", "payload",
        "message_text", "image", "image_name", "run_date",
        "days_of_week", "time_of_day", "target_chat_ids",
        "is_active", "created_at",
    ],
    "poll": [
        "poll_id", "chat_id", "message_id", "button_message_id",
        "question", "options", "status",
        "created_at", "closed_at", "created_by",
    ],
    "vote": [
        "vote_id", "poll_id", "user_id", "user_name",
        "selected_options", "updated_at",
    ],
    "order": [
        # item: JSON array — [{"user_id": <id>, "name": "<user>", "item_name": "<food>", "qty": <n>}, ...]
        # user_id/username here record the *clicker* (payer); per-voter
        # identity lives in each item entry's user_id.
        "order_id", "poll_id", "chat_id", "user_id", "username",
        "item", "order_date", "created_at",
    ],
    "payer": [
        # One row per person who has tapped the Order button (the payer).
        # Upserted on each Order click; surfaced in the Settings "Paid list".
        "user_id", "username", "full_name", "qr_filename", "khqr_text",
        "times_paid", "last_paid_at", "created_at",
    ],
    "history": [
        "event_id", "event_type", "entity_type", "entity_id",
        "user_id", "chat_id", "payload", "created_at",
    ],
    "template": [
        "template_id", "name", "question", "options", "is_active",
        "created_at", "created_by"
    ],
    "invoice": [
        # One row per order (PK invoice_id = order_id); re-sending updates the
        # same row. `details` is a JSON array grouped per person:
        # [{"user_name": "...", "items": [{"item_name","qty","price","cost"}], "subtotal": 1.5}]
        # usd_khr_rate/rate_date PIN the NBC rate used when the invoice was
        # first sent. Riel amounts are always recomputed from these, never
        # from today's rate — otherwise every past invoice would silently
        # change value as the rate moves.
        # display_currencies: comma-separated "USD" / "KHR" — what the sent
        # message showed. Stored so a re-send reproduces the original invoice
        # instead of reverting to the default.
        "invoice_id", "order_id", "poll_id", "chat_id", "order_date",
        "details", "total", "payer_user_id", "payer_name",
        "usd_khr_rate", "rate_date", "display_currencies",
        "sent_count", "last_sent_at", "created_at", "created_by",
    ],
    "exchange_rate": [
        # One row per publication date of the National Bank of Cambodia's
        # official rate (nbc.gov.kh, published ~16:30 ICT on working days).
        # NBC does not publish at weekends or on holidays, so gaps are normal
        # and the newest row is carried forward — see bot/exchange.py.
        "rate_date", "usd_khr", "source", "fetched_at",
    ],
    "payment": [
        # One row per incoming payment transaction (e.g. from PayWay ABA @PayWayByABA_bot).
        # settled_orders: JSON array [{"order_id": "...", "date": "...", "amount": 1.5}, ...]
        # status: MATCHED, UNMATCHED, MANUAL
        "payment_id", "trx_id", "user_id", "sender_name", "amount",
        "currency", "amount_usd", "settled_orders", "status",
        "apv", "raw_text", "created_at",
    ],
}

# Primary key column per tab (first column by convention).
PRIMARY_KEYS: Dict[str, str] = {tab: cols[0] for tab, cols in TABS.items()}


# ---------------------------------------------------------------------------
# Seed data — written by bootstrap.py on first run if the tab is empty.
# Edit values in the spreadsheet directly afterwards; seeds only fire on
# an empty tab to avoid clobbering manual edits.
# ---------------------------------------------------------------------------

SEED_COMMON_CODE: List[Dict[str, str]] = [
    # Roles
    {"group_code": "ROLE", "code": "ADMIN", "label_en": "Admin", "label_kh": "អ្នកគ្រប់គ្រង",
     "sort_order": "1", "is_active": "TRUE", "description": "Full bot configuration access"},
    {"group_code": "ROLE", "code": "MEMBER", "label_en": "Member", "label_kh": "សមាជិក",
     "sort_order": "2", "is_active": "TRUE", "description": "Voting only"},

    # Languages
    {"group_code": "LANG", "code": "KH", "label_en": "Khmer", "label_kh": "ខ្មែរ",
     "sort_order": "1", "is_active": "TRUE", "description": ""},
    {"group_code": "LANG", "code": "EN", "label_en": "English", "label_kh": "អង់គ្លេស",
     "sort_order": "2", "is_active": "TRUE", "description": ""},

    # Poll status
    {"group_code": "POLL_STATUS", "code": "OPEN", "label_en": "Open", "label_kh": "បើក",
     "sort_order": "1", "is_active": "TRUE", "description": ""},
    {"group_code": "POLL_STATUS", "code": "CLOSED", "label_en": "Closed", "label_kh": "បិទ",
     "sort_order": "2", "is_active": "TRUE", "description": ""},

    # Schedule action types
    {"group_code": "SCHEDULE_ACTION", "code": "TEXT", "label_en": "Send text", "label_kh": "ផ្ញើអត្ថបទ",
     "sort_order": "1", "is_active": "TRUE",
     "description": "payload = literal text"},
    {"group_code": "SCHEDULE_ACTION", "code": "QR_PHOTO", "label_en": "Send QR photo", "label_kh": "ផ្ញើ QR",
     "sort_order": "2", "is_active": "TRUE",
     "description": "payload = filename in assets/ (e.g. payment_qr.png)"},

    # Days of week
    *[
        {"group_code": "DAY_OF_WEEK", "code": code, "label_en": en, "label_kh": kh,
         "sort_order": str(i), "is_active": "TRUE", "description": ""}
        for i, (code, en, kh) in enumerate([
            ("MON", "Monday",    "ច័ន្ទ"),
            ("TUE", "Tuesday",   "អង្គារ"),
            ("WED", "Wednesday", "ពុធ"),
            ("THU", "Thursday",  "ព្រហស្បតិ៍"),
            ("FRI", "Friday",    "សុក្រ"),
            ("SAT", "Saturday",  "សៅរ៍"),
            ("SUN", "Sunday",    "អាទិត្យ"),
        ], start=1)
    ],

    # Event types (extend as new events are emitted)
    *[
        {"group_code": "EVENT_TYPE", "code": code, "label_en": label,
         "label_kh": "", "sort_order": str(i), "is_active": "TRUE", "description": ""}
        for i, (code, label) in enumerate([
            ("POLL_CREATED",       "Poll created"),
            ("VOTE_CAST",          "Vote cast or changed"),
            ("ORDER_CLOSED",       "Order closed"),
            ("ORDER_SNAPSHOT",     "Order snapshot taken at cutoff"),
            ("CHAT_SUBSCRIBED",    "Chat subscribed to reminders"),
            ("CHAT_UNSUBSCRIBED",  "Chat unsubscribed from reminders"),
            ("SETTING_UPDATED",    "Setting updated"),
            ("SCHEDULE_UPDATED",   "Schedule created/updated/disabled"),
        ], start=1)
    ],
]


# Default values for the `setting` tab. These mirror the current hardcoded
# constants in bot/config.py — Phase 2 will switch reads to the cache.
SEED_SETTING: List[Dict[str, str]] = [
    {"key": "POLL_QUESTION",
     "value": "តើថ្ងៃនេះចង់ញ៉ាំអ្វី?😋🍴",
     "value_type": "string",
     "description": "Question shown on every food poll",
     "updated_at": "", "updated_by": ""},

    {"key": "ORDER_NAME",
     "value": "Seyha",
     "value_type": "string",
     "description": "Name printed on every order summary (the person collecting orders)",
     "updated_at": "", "updated_by": ""},

    {"key": "ORDER_BUTTON_TEXT",
     "value": "Order",
     "value_type": "string",
     "description": "Label on the Order inline button",
     "updated_at": "", "updated_by": ""},

    {"key": "CLOSE_ORDER_BUTTON_TEXT",
     "value": "Close Order",
     "value_type": "string",
     "description": "Label on the Close Order inline button",
     "updated_at": "", "updated_by": ""},

    {"key": "ORDER_INSTRUCTION_TEXT",
     "value": "Please vote first, then press Order to show the summary.",
     "value_type": "string",
     "description": "Caption on the message that carries the Order/Close buttons",
     "updated_at": "", "updated_by": ""},

    {"key": "DAILY_MESSAGE",
     "value": "តើថ្ងៃនេះបានម្ហូបអ្វី?😋🍴",
     "value_type": "string",
     "description": "Text sent by the weekday morning reminder",
     "updated_at": "", "updated_by": ""},

    {"key": "WELCOME_MESSAGE",
     "value": ("សួស្តី! ខ្ញុំជា Food Poll Bot។\n\n"
               "របៀបប្រើ៖\n"
               "- ផ្ញើម៉ឺនុយដែលមានលេខរៀង\n"
               "- Bot នឹងបង្កើត poll អោយ\n"
               "- ចុច Order ដើម្បីមើលសរុបការកុម្ម៉ង់"),
     "value_type": "string",
     "description": "Sent on /start",
     "updated_at": "", "updated_by": ""},

    {"key": "TIMEZONE",
     "value": "Asia/Phnom_Penh",
     "value_type": "string",
     "description": "IANA timezone for all schedule jobs",
     "updated_at": "", "updated_by": ""},

    {"key": "ORDER_CUTOFF_TIME",
     "value": "10:30",
     "value_type": "time",
     "description": "HH:MM — after this, daily cutoff job snapshots votes into orders",
     "updated_at": "", "updated_by": ""},

    {"key": "ORDER_SUMMARY_STYLE",
     "value": "1",
     "value_type": "string",
     "description": ("Template for the bot's order-summary message when the "
                     "Order button is tapped. 1=classic receipt, 2=compact "
                     "single list, 3=boxed card per item."),
     "updated_at": "", "updated_by": ""},
]


# Default schedule rows seeded if `schedule` tab is empty.
# Matches today's hardcoded WEEKDAY_REMINDER_MESSAGE_TIME / WEEKDAY_VONGSA_QR_TIME.
SEED_SCHEDULE: List[Dict[str, str]] = [
    {"schedule_id": "weekday_text_reminder",
     "name": "Weekday morning text reminder",
     "action_type": "TEXT",
     "payload": "",  # empty payload → fall back to setting DAILY_MESSAGE
     "days_of_week": "MON,TUE,WED,THU,FRI",
     "time_of_day": "08:00",
     "target_chat_ids": "ALL",
     "is_active": "TRUE",
     "created_at": ""},

    {"schedule_id": "weekday_vongsa_qr",
     "name": "Weekday lunchtime Vongsa QR",
     "action_type": "QR_PHOTO",
     "payload": "payment_qr.png",
     "days_of_week": "MON,TUE,WED,THU,FRI",
     "time_of_day": "12:00",
     "target_chat_ids": "ALL",
     "is_active": "TRUE",
     "created_at": ""},
]


# Default templates seeded if `template` tab is empty.
SEED_TEMPLATE: List[Dict[str, str]] = [
    {"template_id": "lunch_menu_tpl",
     "name": "Lunch Menu",
     "question": "តើបងៗចង់ញ៉ាំអ្វីសម្រាប់ថ្ងៃត្រង់?😋🍴",
     "options": '["បាយសាច់ជ្រូកចៀន","បាយទាសាសាវ","បាយមាន់ដុត","មីទាខ្វៃ"]',
     "is_active": "TRUE",
     "created_at": "2026-07-15T00:00:00+07:00"},

    {"template_id": "drinks_menu_tpl",
     "name": "Drinks",
     "question": "ចង់កុម្ម៉ង់ភេសជ្ជៈអ្វីខ្លះបងៗ?🥤☕️",
     "options": '["កាហ្វេទឹកដោះគោទឹកកក","តែបៃតងទឹកដោះគោ","តែក្រូចឆ្មា","ទឹកក្រូចច្របាច់"]',
     "is_active": "TRUE",
     "created_at": "2026-07-15T00:00:00+07:00"},
]

