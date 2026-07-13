# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
# Install dependencies (a .venv/ already exists in the repo root)
pip install -r requirements.txt

# Run the bot (requires BOT_TOKEN in .env — see env.example)
python main.py

# Smoke-test the package structure (imports, menu parsing, bot construction)
python test_structure.py
```

There is no formal test suite, linter, or formatter configured. `test_structure.py` is an import/sanity check, not unit tests.

## Architecture

The bot is a Telegram bot (python-telegram-bot 20.1) that turns Khmer/English numbered food menus into interactive polls, then aggregates votes into order summaries. Entry point: [main.py](main.py) → [bot/bot.py](bot/bot.py) `FoodPollBot.setup()` + `.run()`.

**Two entry points exist — use the package, not the monolith:**
- [main.py](main.py) → [bot/](bot/) package (canonical)
- [simple_bot.py](simple_bot.py) is a self-contained earlier version kept around but **not** wired into `main.py`. Don't edit it expecting changes to take effect; modify the `bot/` package instead.

**Module roles** (everything under [bot/](bot/)):
- [bot/bot.py](bot/bot.py) — Builds the PTB `Application`, registers `BotCommand`s, wires handlers, and starts the APScheduler in `post_init`. **PTB's built-in JobQueue is explicitly disabled** (`.job_queue(None)`) because PTB 20.1 has a weakref crash on Python 3.14; all scheduled work must go through APScheduler in `scheduler.py`, not PTB jobs.
- [bot/handlers.py](bot/handlers.py) — All command/message/callback/poll handlers, registered via `setup_handlers(app)`. Commands: `/start`, `/subscribe`, `/unsubscribe`, `/debug_send`, `/debug_qr`, `/vongsa`, `/ty`.
- [bot/menu_processor.py](bot/menu_processor.py) — Menu → poll creation, plus **all live poll state is held in module-level dicts** (`poll_data`, `global_orders`, `user_selections`, `order_button_used`). This state is **in-memory only and lost on restart** — there is no DB. Each poll gets a follow-up message with `Order` / `Close Order` inline buttons whose IDs are stored alongside the poll so they can be edited later.
- [bot/scheduler.py](bot/scheduler.py) — APScheduler cron jobs for two weekday reminders (Mon–Fri, Asia/Phnom_Penh): a text reminder and a Vongsa KHQR image. Subscribed chat IDs are **persisted** to [data/scheduled_chats.json](data/scheduled_chats.json) (only persisted state in the project); they're loaded lazily via `_load_scheduled_chats()` on first use.
- [bot/utils.py](bot/utils.py) — `is_food_menu_text` / `extract_menu_options` (regex over Khmer digits ១–៦ and Arabic 1–6), `with_retry` wrapper for `NetworkError`/`TimedOut`, and `format_order_summary`.
- [bot/config.py](bot/config.py) — `load_dotenv()` at import time; raises if `BOT_TOKEN` is missing. Holds timezone, reminder times, Khmer message templates, and `ORDER_NAME` (currently hardcoded to `"Seyha"` — the name printed on every order summary).

**Menu detection rule** ([bot/utils.py:47](bot/utils.py#L47)): a message is treated as a menu if it starts with `ម្ហូបថ្ងៃ` OR contains ≥2 lines beginning with a Khmer/Arabic numeral 1–6. The numeral regex is capped at 6 — adding support for more items means widening `_NUMERAL_PATTERN` in [bot/utils.py:28](bot/utils.py#L28).

**Asset paths** are resolved relative to the package, not CWD: QR images live in [assets/](assets/) (`payment_qr.png` for Vongsa, `ty_qr.png` for Ty), referenced via `Path(__file__).parent.parent / "assets"` in handlers and the scheduler.

## Notes for changes

- Adding a new command: add the handler in [bot/handlers.py](bot/handlers.py), register it in `setup_handlers`, and add the `BotCommand(...)` entry in `post_init` inside [bot/bot.py](bot/bot.py) so it appears in Telegram's command menu.
- Adding a new scheduled job: add it in `setup_scheduler` in [bot/scheduler.py](bot/scheduler.py) using `_scheduler.add_job(..., replace_existing=True)`. Do **not** use PTB's JobQueue (disabled — see above).
- Poll state changes survive only until the process restarts. If persistence is needed, it must be added (no migration path currently exists).
