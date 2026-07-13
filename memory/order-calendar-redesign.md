---
name: order-calendar-redesign
description: The Mini App calendar shows the order tab (not polls); admins see all orders, members see only their own
metadata:
  type: project
---

On branch `feature/miniapp-calendar`, the Mini App calendar was reworked to be
**order-driven**, not poll-driven:

- `order` rows carry per-voter `user_id` inside the `item` JSON
  (`[{user_id,name,item_name,qty}]`) so orders can be filtered per member.
- `GET /api/orders?from=&to=` is the calendar source (was `/api/polls`), gated by
  `require_member`: **admins** get every order with full items; **members** get
  only orders containing their `user_id`, with items trimmed to theirs.
- Both order-writing paths now share `orders.snapshot_from_poll` (Order button in
  handlers.py AND the cutoff job in scheduler.py) so row shapes never diverge.
- New `payer` tab: upserted on each Order-button click; surfaced in the
  redesigned Settings "Paid list" (`/api/payers`).
- Templates tab is admin-only; Members tab is open to all members; Settings has
  full CRUD for schedules (`/api/schedules` POST/PUT/DELETE) and common messages
  (`/api/settings` POST/DELETE).

**How to apply:** When touching the calendar, remember member-vs-admin scoping
lives server-side in `bot/api/orders.py::_shape_order`. See [[live-sheets-in-dev]].
