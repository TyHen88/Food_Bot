---
name: local-env-token-is-different-bot
description: The local .env BOT_TOKEN is NOT the production Food_Bot — don't trust local Telegram API checks
metadata:
  type: project
---

The `BOT_TOKEN` in the local `.env` belongs to a **different bot** (a writing/email assistant whose commands are `menu, idea, templates, email, reply, improve, rewrite, grammar, explain, tone, signature, clearsignature, help`), not the production Food_Bot. Confirmed by the user on 2026-05-26.

**Why:** Production Food_Bot uses a separate token set in Railway env vars; the local `.env` token is intentionally a different bot.

**How to apply:** Do NOT use the local `BOT_TOKEN` to verify production bot state (getWebhookInfo, getMyCommands, set_webhook, etc.) — it queries the wrong bot. The `GOOGLE_SHEET_ID` may also point to a non-production sheet, so live-data checks (user/poll/vote/order rows) may not reflect production either. Verify logic in code; ask the user to confirm runtime behavior.
