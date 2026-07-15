# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See the repo-root [../CLAUDE.md](../CLAUDE.md) for the two-app (backend + Next.js Mini App) big picture, the frontend↔backend auth chain, and the **live-production-spreadsheet warning** — this checkout's `.env` has real Google credentials, so never invoke Sheets write paths from ad-hoc scripts.

## Commands

```powershell
# Install dependencies (a .venv/ already exists in this directory)
pip install -r requirements.txt

# Run everything (FastAPI on :8000 + bot; requires BOT_TOKEN in .env — see env.example)
python main.py

# Smoke-test the package structure (imports, menu parsing, bot construction)
python test_structure.py
```

There is no formal test suite, linter, or formatter configured. `test_structure.py` is an import/sanity check, not unit tests.

## Architecture

One Python process runs three things in a single event loop, all wired up in [main.py](main.py)'s FastAPI lifespan handler:

1. **python-telegram-bot 20.1 Application** (built by `build_application()` in [bot/bot.py](bot/bot.py)) — turns Khmer/English numbered food menus into polls, aggregates votes into orders.
2. **FastAPI** — `POST /webhook` (Telegram updates), `/api/*` REST routers for the Mini App, `/health`.
3. **APScheduler** — reminder/cutoff jobs built from the `schedule` sheet tab.

**Update delivery mode is chosen by `WEBHOOK_URL`:** set → webhook mode with a watchdog task that re-registers the webhook if Telegram reports it drifted (rolling-deploy protection — this is also why the lifespan handler deliberately does NOT delete the webhook on shutdown); empty → long polling inside the same process (local dev, no ngrok needed). Because FastAPI owns the lifecycle (no `run_polling()`/`run_webhook()`), `main.py` must call `application.post_init(...)` manually — without it the command menu and scheduler jobs never get set up.

**PTB's built-in JobQueue is explicitly disabled** (`.job_queue(None)` in bot.py) because PTB 20.1 has a weakref crash on Python 3.14. All scheduled work must go through APScheduler in [bot/scheduler.py](bot/scheduler.py), never PTB jobs.

**Persistence is Google Sheets** ([bot/sheets/](bot/sheets/)) — the old module-level in-memory dicts are gone:
- [bot/sheets/schema.py](bot/sheets/schema.py) — **single source of truth** for tabs/columns (`common_code`, `setting`, `chat_setting`, `user`, `chat`, `schedule`, `poll`, `vote`, `order`, `payer`, `history`, …). Adding a column = edit `TABS` + bump `SCHEMA_VERSION`; [bot/sheets/bootstrap.py](bot/sheets/bootstrap.py) idempotently creates missing tabs/headers on startup.
- [bot/sheets/repo.py](bot/sheets/repo.py) — generic CRUD; [bot/sheets/cache.py](bot/sheets/cache.py) — per-tab cache with 60s refresh loop (reads must not hit the Sheets API in hot paths; a role edit in the sheet takes effect on the next tick).
- Per-domain modules (`polls.py`, `votes.py`, `orders.py`, `chats.py`, `settings.py`, `payers.py`, `events.py` = append-only `history` audit log).
- **Fallback:** when credentials are missing (`is_configured()` False) the bot still runs — chat subscriptions fall back to [data/scheduled_chats.json](data/scheduled_chats.json), poll state is in-memory-only. Code that touches persistence usually branches on `is_configured()` (see [bot/scheduler.py](bot/scheduler.py) for the pattern).

**Mini App API** ([bot/api/](bot/api/)) — one router module per resource (`settings`, `schedules`, `history`, `orders`, `payers`, `polls`, `members`, `me`, `templates`), aggregated in [bot/api/\_\_init\_\_.py](bot/api/__init__.py) under `/api`. Auth is via `require_admin` / `require_member` dependencies in [bot/api/auth.py](bot/api/auth.py) (HMAC-verified Telegram `initData`; `DEV_BYPASS_AUTH` shortcut works only when `WEBHOOK_URL` is empty). Member-vs-admin scoping of order data is server-side in `bot/api/orders.py::_shape_order`.

**Two admin gates, one truth:** both chat commands ([bot/auth.py](bot/auth.py) `@admin_only`) and API routes check `role='ADMIN'` on the `user` tab row. `ADMIN_USER_IDS` env var does **not** grant admin — it only selects the dev-bypass identity.

**Both order-writing paths share `orders.snapshot_from_poll`** — the Order button (handlers.py) and the scheduled cutoff job (scheduler.py) — so `order` row shapes never diverge. `order.item` is a JSON array carrying per-voter `user_id`, which is what makes per-member filtering possible.

**Other module roles:**
- [bot/handlers.py](bot/handlers.py) — all command/message/callback/poll handlers, registered via `setup_handlers(app)`. Commands: `/start`, `/subscribe`, `/unsubscribe`, `/debug_send`, `/debug_qr`, `/vongsa`, `/ty`, `/app`, plus admin-only `/admin`, `/set`, `/schedule_list`, `/schedule_enable`, `/schedule_disable`.
- [bot/menu_processor.py](bot/menu_processor.py) — menu text → poll creation, writes `poll` rows.
- [bot/utils.py](bot/utils.py) — `is_food_menu_text` / `extract_menu_options` (regex over Khmer digits ១–៦ and Arabic 1–6), `with_retry`, `format_order_summary`.
- [bot/config.py](bot/config.py) — env-var loading only (`load_dotenv()` at import; raises if `BOT_TOKEN` missing). Runtime-tunable values live in the `setting` tab (global) with per-chat overrides in `chat_setting`.
- [simple_bot.py](simple_bot.py) — self-contained ancient version, **not wired into main.py**. Don't edit it expecting changes to take effect.

**Menu detection rule** ([bot/utils.py](bot/utils.py)): a message is a menu if it starts with `ម្ហូបថ្ងៃ` OR contains ≥2 lines beginning with a Khmer/Arabic numeral 1–6. The numeral regex is capped at 6 — supporting more items means widening `_NUMERAL_PATTERN`.

**Asset paths** resolve relative to the package, not CWD: QR images in [assets/](assets/), referenced via `Path(__file__).parent.parent / "assets"`.

## Notes for changes

- New command: handler in [bot/handlers.py](bot/handlers.py) + register in `setup_handlers` + `BotCommand(...)` entry in `post_init` in [bot/bot.py](bot/bot.py) so it shows in Telegram's menu. Admin commands get the `@admin_only` decorator.
- New scheduled behavior: schedules are data, not code — rows in the `schedule` tab (managed from the Mini App or `/schedule_*` commands), turned into APScheduler jobs by `setup_scheduler` / rebuilt by the refresh path in [bot/scheduler.py](bot/scheduler.py). Do **not** use PTB's JobQueue.
- New API endpoint: new module in [bot/api/](bot/api/), include it in `api_router`, choose `require_admin` vs `require_member`, and emit a `history` event via `events.emit` for writes.
- Sheets writes are rate-limited (~60/min) — batch them and keep them out of user-facing latency (fire-and-forget), matching the existing repo patterns.
- [PLAN.md](PLAN.md) is the historical migration plan: phases 0–5 are essentially implemented, but its "plain HTML/CSS/JS frontend" was superseded by the Next.js app in [../frontend/](../frontend/), and `main.py`'s static mount of a local `frontend/` dir is a leftover (the dir doesn't exist). README.md / PROJECT_SUMMARY.md predate all of this.
