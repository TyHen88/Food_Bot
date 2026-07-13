# Food Bot — Upgrade Plan: Sheets-backed + Mini App

This plan covers upgrading the bot from in-memory state + hardcoded config to a Google Sheets-backed system with a plain HTML/CSS/JS Mini App for customization. Single Railway service, single Python process.

## Goal

Replace in-memory state and hardcoded config with Google Sheets as the persistence layer. Add a small HTML/CSS/JS Mini App for non-technical admins to customize polls, schedules, templates, and view history. Keep deployment as a single Railway service.

## Locked decisions

- **Storage:** Google Sheets via `gspread` (no traditional DB)
- **History semantics:** audit/event log (append-only)
- **Payment tracking:** out of scope for now
- **Sheet bootstrap:** bot auto-creates tabs + headers on first run
- **Migration:** full cutover (no dual-write period)
- **Frontend:** plain HTML/CSS/JS + Telegram WebApp SDK, no React, no build step
- **Deployment:** all-in-one — FastAPI serves webhook + API + static frontend in the same process; APScheduler runs in the same event loop; single Railway service; single replica
- **Bot mode:** switch from long polling to webhook
- **GCP:** user already has service account + JSON

## Architecture

```
                            Railway (single service)
   ┌─────────────────────────────────────────────────────────────────┐
   │  uvicorn → FastAPI app                                           │
   │                                                                  │
   │    POST /webhook         ← Telegram pushes updates here          │
   │    GET  /api/settings    ← Mini App reads                        │
   │    POST /api/settings    ← Mini App writes                       │
   │    GET  /api/templates   ← ...etc (CRUD per resource)            │
   │    GET  /health          ← Railway health check                  │
   │    GET  /  (static)      ← Mini App HTML/CSS/JS                  │
   │                                                                  │
   │  Inside the same event loop:                                     │
   │    • python-telegram-bot Application (webhook handler)           │
   │    • APScheduler (reminder jobs from `schedule` tab)             │
   │    • gspread client (Sheets CRUD)                                │
   │    • In-memory cache per tab, 60s refresh                        │
   └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                          Google Sheets (10 tabs)
```

## Tech stack additions

- `fastapi`, `uvicorn[standard]` — web framework
- `gspread`, `google-auth` — Sheets API
- `python-multipart` — only if file uploads needed later

## New env vars

```
BOT_TOKEN=<existing>
WEBHOOK_URL=https://<railway-app>.up.railway.app    # base URL for /webhook
GOOGLE_SHEET_ID=<sheet ID from URL>
GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}   # full JSON, one line
ADMIN_USER_IDS=123,456                              # comma-sep Telegram user IDs (bootstrap admins)
```

`LOG_LEVEL` and `LOG_FILE` stay as-is.

## Final repo layout

```
Food_Bot/
├── main.py                    # FastAPI app entry (rewritten)
├── bot/
│   ├── __init__.py
│   ├── bot.py                 # PTB Application factory (no run_polling)
│   ├── config.py              # env-var loading only; runtime settings come from Sheets
│   ├── handlers.py            # existing + new admin commands
│   ├── menu_processor.py      # now Sheets-backed (no module-level dicts)
│   ├── scheduler.py           # APScheduler driven by `schedule` tab
│   ├── utils.py
│   └── sheets/                # new package
│       ├── __init__.py
│       ├── client.py          # authenticated gspread singleton + retry
│       ├── schema.py          # tab → headers map (source of truth)
│       ├── bootstrap.py       # idempotent tab/header creation + seed
│       ├── cache.py           # per-tab in-memory cache + refresh loop
│       └── repo.py            # generic CRUD wrapper
├── frontend/                  # new
│   ├── index.html             # entry, lists screens
│   ├── settings.html
│   ├── templates.html
│   ├── schedule.html
│   ├── history.html
│   ├── style.css
│   └── app.js                 # fetch() helpers, Telegram WebApp init
├── assets/                    # existing QR images (unchanged)
├── requirements.txt
├── railway.toml               # new — start command
├── env.example                # updated
└── .gitignore                 # add frontend/dist if any later build, .env stays ignored
```

## Sheet schemas (10 tabs)

### 1. `common_code`
| group_code | code | label_en | label_kh | sort_order | is_active | description |

Examples: `ROLE/ADMIN`, `LANG/KH`, `POLL_STATUS/OPEN`, `SCHEDULE_ACTION/TEXT`, `DAY_OF_WEEK/MON`, `EVENT_TYPE/POLL_CREATED`

### 2. `setting`
| key | value | value_type | description | updated_at | updated_by |

Keys: `POLL_QUESTION`, `ORDER_NAME`, `ORDER_CUTOFF_TIME`, `TIMEZONE`, `DAILY_MESSAGE`, `WELCOME_MESSAGE`, etc.

### 3. `user`
| user_id | username | full_name | role | language | dietary_notes | created_at | last_active_at |

PK: `user_id` (Telegram).

### 4. `chat`
| chat_id | title | type | is_subscribed | subscribed_at | subscribed_by |

PK: `chat_id`. Replaces `data/scheduled_chats.json`.

### 5. `template`
| template_id | name | question | options | created_by | created_at | is_active |

`options` stored as JSON array string.

### 6. `schedule`
| schedule_id | name | action_type | payload | days_of_week | time_of_day | target_chat_ids | is_active | created_at |

Replaces hardcoded `WEEKDAY_REMINDER_MESSAGE_TIME` / `WEEKDAY_VONGSA_QR_TIME`. `target_chat_ids` is CSV or `ALL`.

### 7. `poll`
| poll_id | chat_id | message_id | button_message_id | template_id | question | options | status | created_at | closed_at | created_by |

PK: `poll_id` (from Telegram). `options` stored as JSON array string.

### 8. `vote`
| vote_id | poll_id | user_id | user_name | selected_options | updated_at |

PK: `vote_id = {poll_id}_{user_id}`. One row per (poll, user), updated in place.

### 9. `order`
| order_id | poll_id | chat_id | user_id | user_name | item | quantity | order_date | notes | created_at |

PK: `order_id = {poll_id}_{user_id}_{seq}`. Created at cutoff time from `vote` rows.

### 10. `history`
| event_id | event_type | entity_type | entity_id | user_id | chat_id | payload | created_at |

Append-only audit log. `payload` is JSON.

## CRUD matrix

| Tab | C | R | U | D | Triggered by |
|---|---|---|---|---|---|
| `common_code` | manual seed | startup + 60s | manual | manual | Edit sheet directly |
| `setting` | seed | startup + 60s | admin command/UI | rare | `/set` command, Mini App |
| `user` | first interaction | every handler | role change | rare | Any message |
| `chat` | `/start`, `/subscribe` | scheduler reads list | rare | `/unsubscribe` | Subscribe commands |
| `template` | admin | menu post, Mini App | admin | admin (soft) | Commands, Mini App |
| `schedule` | admin | startup + 60s | admin | admin (soft) | Commands, Mini App |
| `poll` | menu detected | callback handlers | close_order | never | Menu detection, buttons |
| `vote` | first poll answer | summary, cutoff | every poll-answer change | unvote all | `PollAnswerHandler` |
| `order` | cutoff snapshot | history view | rare | rare | Cutoff job, admin |
| `history` | every write op | admin view, debug | never | trim job (optional) | All writes |

## Sheets design rules (apply everywhere)

| Rule | Why |
|---|---|
| Access by header name (`get_all_records`) | Survives column reordering in the sheet |
| In-memory cache per tab, 60s TTL | Sheets ≈300 ms/call — can't be in hot path |
| Writes via `asyncio.create_task` (fire-and-forget) | Don't make Telegram users wait |
| Batch multi-row writes (`batch_update`) | One API call instead of N |
| Natural keys where Telegram provides them | No ID generation needed |
| Timestamps as ISO 8601 with offset | Sortable, human-readable |
| Arrays stored as JSON strings | Avoids row explosion; under 50k char limit |
| Soft delete (`is_active=FALSE`) | Preserves audit trail |
| Retry on 429/5xx with exponential backoff (3 attempts) | Google rate-limits at 60 writes/min/user |
| Frozen header row | Prevents accidental edits |
| `_meta` tab with `schema_version` cell | Future migrations |

---

# Implementation phases

Each phase is shippable on its own. Stop after any phase if scope feels right.

## Phase 0 — FastAPI + webhook scaffolding (~½ day)

**Goal:** switch from `run_polling` to webhook mode and stand up the FastAPI shell. No Sheets yet. No behavior change for end users.

| Step | File | Action |
|---|---|---|
| 0.1 | `requirements.txt` | Add `fastapi`, `uvicorn[standard]` |
| 0.2 | `env.example` + `bot/config.py` | Add `WEBHOOK_URL`, `ADMIN_USER_IDS` env vars |
| 0.3 | `bot/bot.py` | Refactor: remove `run_polling`. Expose `build_application()` returning a configured `Application` without starting it |
| 0.4 | `main.py` | Rewrite as FastAPI app: lifespan handler builds the PTB Application, sets webhook on startup, deletes webhook on shutdown. Wire `POST /webhook` → `application.process_update(...)`. Add `GET /health` |
| 0.5 | `main.py` | Mount `frontend/` as static files at `/`. Create placeholder `frontend/index.html` ("Hello from Mini App") |
| 0.6 | `railway.toml` | Add `[deploy] startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"` |
| 0.7 | Smoke test | Deploy to Railway, set `WEBHOOK_URL`, verify bot still answers `/start` and sends scheduled reminders |

**Rollback:** revert the commit; old `main.py` works as before.

## Phase 1 — Sheets foundation (~1 day)

**Goal:** wire up the Sheets client, schema bootstrap, and cache. No behavior change yet.

| Step | File | Action |
|---|---|---|
| 1.1 | `requirements.txt` | Add `gspread`, `google-auth` |
| 1.2 | `env.example` + `bot/config.py` | Add `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_JSON` |
| 1.3 | `bot/sheets/client.py` | `gspread` client singleton, retry decorator on 429/5xx |
| 1.4 | `bot/sheets/schema.py` | Tab name → list of column headers. Source of truth |
| 1.5 | `bot/sheets/bootstrap.py` | Idempotent: check each tab, create missing, append missing headers. Run from FastAPI startup before bot starts |
| 1.6 | `bot/sheets/cache.py` | Per-tab cache, 60s TTL, async refresh loop |
| 1.7 | `bot/sheets/repo.py` | Generic CRUD: `create(tab, row)`, `read(tab, pk)`, `update(tab, pk, fields)`, `delete(tab, pk, soft=True)`, `list(tab, filter=...)`. Writes via `asyncio.create_task` |
| 1.8 | `bot/sheets/bootstrap.py` | Seed `common_code` defaults and `setting` defaults (from current `config.py` constants) if tabs are empty |

**Rollback:** revert; nothing depends on the new package yet.

## Phase 2 — Migrate persistent state (~1 day)

**Goal:** replace `scheduled_chats.json` and hardcoded config constants with Sheets-backed equivalents.

| Step | File | Action |
|---|---|---|
| 2.1 | One-time migration script | Read existing `data/scheduled_chats.json` → write rows into `chat` tab |
| 2.2 | `bot/scheduler.py` | Replace `chat_ids_for_scheduled_messages` set + JSON I/O with `chat` repo calls |
| 2.3 | `bot/config.py` | Keep env-var loading; runtime values (`POLL_QUESTION`, `ORDER_NAME`, message templates) move to `setting` tab + cache lookup |
| 2.4 | `bot/scheduler.py` | Build APScheduler jobs from `schedule` tab on startup. Seed default `schedule` rows for the existing two reminders if tab is empty |
| 2.5 | `bot/handlers.py` | `/start`, `/subscribe`, `/unsubscribe` → write to `chat` + `user` tabs, emit `history` event |
| 2.6 | Verification | After deploy, confirm `chat` tab has all subscribers; then delete `data/scheduled_chats.json` |

**Rollback:** redeploy previous commit; JSON file still has the original data until you delete it.

## Phase 3 — Migrate poll + vote state (~1–2 days)

**Goal:** kill the last in-memory dicts. Polls survive restarts.

| Step | File | Action |
|---|---|---|
| 3.1 | `bot/menu_processor.py` | Delete module-level `poll_data` / `global_orders` / `user_selections` / `order_button_used` dicts |
| 3.2 | `bot/menu_processor.py` | `process_food_menu` writes a `poll` row, emits `POLL_CREATED` history |
| 3.3 | `bot/handlers.py` | `handle_poll_answer` upserts `vote` row, emits `VOTE_CAST` |
| 3.4 | `bot/handlers.py` | `handle_callback_query` close_order → updates `poll.status='CLOSED'`, emits `ORDER_CLOSED` |
| 3.5 | `bot/menu_processor.py` | `hide_order_buttons` reads `button_message_id` from `poll` tab |
| 3.6 | `bot/scheduler.py` | New cutoff job (time from `setting.ORDER_CUTOFF_TIME`): snapshot current `vote` rows for OPEN polls into `order` rows |

**Risk:** in-flight polls at the moment of deploy lose their state. Recommend deploying outside meal hours (avoid 7am–1pm Phnom Penh time).

## Phase 4 — Admin commands (~1 day)

**Goal:** the customization features you want, accessible from chat without a Mini App.

| Step | File | Action |
|---|---|---|
| 4.1 | `bot/handlers.py` | Role-check decorator: only `ADMIN` in `user` tab can run config commands |
| 4.2 | `bot/handlers.py` | `/template_save <name>` (reply to a menu message), `/template_list`, `/template_post <name>`, `/template_delete <name>` |
| 4.3 | `bot/handlers.py` | `/set <key> <value>` for `setting` tab |
| 4.4 | `bot/handlers.py` | `/schedule_list`, `/schedule_disable <id>`, `/schedule_enable <id>` |
| 4.5 | `bot/bot.py` | Register the new commands in `post_init` so they appear in Telegram's command menu |

After this phase, you have a fully customizable bot without any frontend. Mini App becomes optional polish.

## Phase 5 — Mini App UI (~2–3 days)

**Goal:** friendlier interface for non-technical admins. Plain HTML/CSS/JS, no build step.

| Step | File | Action |
|---|---|---|
| 5.1 | `main.py` | Add API routes per resource: `/api/settings`, `/api/templates`, `/api/schedule`, `/api/history`. Use the same `bot.sheets.repo` |
| 5.2 | `main.py` | Add `verify_init_data(init_data)` dependency: HMAC-check Telegram WebApp `initData` against `BOT_TOKEN`. Reject if user not in admin role |
| 5.3 | `frontend/app.js` | Telegram WebApp init: read theme vars, set up `fetch()` helper that auto-attaches `initData` header |
| 5.4 | `frontend/settings.html` + JS | Form bound to `setting` tab keys. Save → POST → optimistic UI update |
| 5.5 | `frontend/templates.html` + JS | List + create + delete templates |
| 5.6 | `frontend/schedule.html` + JS | Day/time picker + enable toggle |
| 5.7 | `frontend/history.html` + JS | Paginated event log, filter by event_type |
| 5.8 | `bot/bot.py` | Add a `/admin` command that opens the Mini App via `WebAppInfo` button |

Add Alpine.js (single `<script>`) only if a screen gets too reactive to manage by hand. Skip if not needed.

---

## Migration safety summary

| Phase | Risk | Mitigation |
|---|---|---|
| 0 | Webhook URL wrong → bot silent | Test `/health` after deploy; verify `getMe` and Telegram's `getWebhookInfo` |
| 1 | None (no behavior change) | — |
| 2 | Lose subscriber list if JSON deleted before Sheets verified | Verify `chat` tab populated first; keep JSON file for 1 week |
| 3 | In-flight polls lose state at cutover | Deploy outside meal hours |
| 4 | None (additive) | — |
| 5 | Auth misconfigured → unauthorized users edit settings | Test `initData` verification with a non-admin account |

## Open items / future work

- **Schema versioning:** if any schema changes after first ship, write a migration script that reads/rewrites the affected tab
- **History pruning:** if `history` grows large, add a weekly job to delete rows older than 90 days
- **Backup:** Google Sheets has built-in version history, but consider a weekly export to a separate spreadsheet as a snapshot
- **Multi-language UI:** if needed later, use `common_code.label_kh` already in schema; add language toggle in Mini App
- **Order cutoff edge cases:** what if no votes? what if poll is in a chat with no cutoff configured? — decide during Phase 3
- **Webhook secret:** Telegram supports a `secret_token` on webhooks to prevent spoofed POSTs. Add in Phase 0 or as a follow-up
