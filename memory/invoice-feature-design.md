---
name: invoice-feature-design
description: Per-user invoice feature — why the exchange rate isn't scraped from ABA, and the DM-reachability constraint
metadata:
  type: project
---

Admin-only feature (in progress on branch feature/miniapp-calendar): generate a
per-user invoice from an order and DM each person their bill + payer KHQR.

Two non-obvious decisions baked into the design:

1. **Exchange rate is NOT scraped from ABA.** ababank.com/en/forex-exchange is
   behind a Cloudflare challenge ("Just a moment…") — both httpx and WebFetch
   get HTTP 403. So the rate auto-fetches from `open.er-api.com/v6/latest/USD`
   (free, no key, ~4024 KHR/USD) via a daily scheduler job, cached in the
   `setting` tab (`USD_KHR_RATE`). To use ABA's exact "ABA Buys" figure, an admin
   sets `USD_KHR_RATE_AUTO=FALSE` and pins `USD_KHR_RATE`. Don't re-attempt a
   plain HTTP scrape of ABA — it needs a headless browser, deliberately avoided.

2. **A bot can't DM a user who hasn't messaged it privately first.** This is the
   core UX constraint. `can_dm`/`dm_chat_id` columns on the `user` tab are set in
   handlers._record_user the moment a user interacts in a PRIVATE chat. Invoices
   only reach users with `can_dm=TRUE`. Unreachable users have two admin-chosen
   fallbacks (POST /invoices/generate `fallback` = group|deeplink|none): "group"
   posts their bill in the group with a tg://user ping; "deeplink" stores the bill
   in the `invoice_link` tab under a short token and posts a group button
   t.me/<bot>?start=inv_<token> — /start redeems it privately (identity-checked:
   only the row's user_id may open it) and flips can_dm TRUE. `can_dm` only
   accumulates from deploy onward — there's no Telegram API to query reachability.

Code: [[live-sheets-in-dev]] (this checkout hits a REAL sheet — never call
write/snapshot in test scripts). Schema is at version 10. Key files:
bot/exchange.py, bot/api/invoices.py, bot/api/prices.py, bot/sheets/invoice_links.py,
frontend/invoice.html; deep-link redemption in bot/handlers.py handle_start_command.
