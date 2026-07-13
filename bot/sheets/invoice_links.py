"""
Pending invoice deep-links — the `invoice_link` tab.

When an invoice can't be DM'd (the recipient never started the bot privately),
the generate endpoint stores the prebuilt invoice here under a short token and
posts a group button `t.me/<bot>?start=<token>`. The user taps it, /start
redeems the token, and the bot delivers the stored invoice privately.

Redemption is identity-checked in the handler: only the row's user_id may open
its token. In-memory fallback mirrors the other sheet helpers for local dev.
"""

import logging
from typing import Any, Dict, Optional

from . import repo
from .client import is_configured

logger = logging.getLogger(__name__)

# Fallback for local dev: {token: row}
_mem: Dict[str, Dict[str, Any]] = {}


def new_token() -> str:
    """A short deep-link-safe token (Telegram start payloads cap at 64 chars,
    [A-Za-z0-9_-])."""
    return "inv_" + repo.new_uuid()[:12]


async def create(
    token: str,
    *,
    order_id: str,
    user_id: Any,
    text: str,
    qr_filename: str = "",
    khqr_text: str = "",
) -> Dict[str, Any]:
    row = {
        "token": token,
        "order_id": order_id,
        "user_id": str(user_id),
        "text": text,
        "qr_filename": qr_filename,
        "khqr_text": khqr_text,
        "status": "PENDING",
        "created_at": repo.now_iso(),
        "delivered_at": "",
    }
    if is_configured():
        await repo.upsert("invoice_link", row)
    else:
        _mem[token] = row
    return row


async def get(token: str) -> Optional[Dict[str, Any]]:
    if is_configured():
        return await repo.find_by_pk("invoice_link", token)
    return _mem.get(token)


async def mark_delivered(token: str) -> None:
    fields = {"status": "DELIVERED", "delivered_at": repo.now_iso()}
    if is_configured():
        await repo.update("invoice_link", token, fields)
    elif token in _mem:
        _mem[token].update(fields)
