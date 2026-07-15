---
name: live-sheets-in-dev
description: This dev environment has a LIVE Google Sheets backend configured — test scripts write to real data
metadata:
  type: project
---

In this checkout, `bot.sheets.client.is_configured()` returns **True** — Google
service-account creds are present and `repo`/`votes`/`orders` calls hit a **real
spreadsheet**, not the in-memory fallback. `DEV_BYPASS_AUTH` is also active
(WEBHOOK_URL empty), which can make it look like a throwaway local setup — it is not.

**Why:** Running an ad-hoc async script that calls `votes.record` /
`orders.snapshot_from_poll` appended live test rows (had to delete them via
`run_sync(repo._delete_sync, tab, pk)`).

**How to apply:** To exercise data-layer logic, test the pure functions
(`orders._build_item_json`, `orders._shape_order`, `_parse_items`) with
hand-built dict rows — do NOT call the `record`/`snapshot`/`upsert` paths unless
you intend to mutate the real sheet. The repo has a full Sheets backend
(`bot/sheets/`, `bot/api/`) and a Mini App (`../frontend/`, Next.js); CLAUDE.md
(root + backend) was updated 2026-07-15 to describe this. See [[order-calendar-redesign]].
