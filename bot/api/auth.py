"""
Telegram WebApp init-data verification + admin gate.

The Mini App attaches Telegram's signed `initData` blob to every request
(via the `X-Telegram-Init-Data` header). We verify the HMAC against
BOT_TOKEN, decode the user payload, and check that the user is an admin.

See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status

from ..auth import is_admin
from ..config import ADMIN_USER_IDS, BOT_TOKEN, DEV_BYPASS_AUTH, WEBHOOK_URL

logger = logging.getLogger(__name__)


def _dev_bypass_active() -> bool:
    """The bypass is honoured ONLY when WEBHOOK_URL is empty (local dev)."""
    return DEV_BYPASS_AUTH and not WEBHOOK_URL


if _dev_bypass_active():
    logger.warning(
        "DEV_BYPASS_AUTH is active — Mini App requests will skip Telegram "
        "init-data verification. This is local-only because WEBHOOK_URL is "
        "empty. Never set both DEV_BYPASS_AUTH and WEBHOOK_URL in production."
    )

# Reject initData older than this many seconds (prevents replay attacks).
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60


def _verify_signature(init_data: str) -> Optional[dict]:
    """Verify the HMAC on Telegram's initData; return parsed dict or None."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=False))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    # Replay-attack guard: initData becomes stale.
    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            if int(time.time()) - int(auth_date) > INIT_DATA_MAX_AGE_SECONDS:
                return None
        except ValueError:
            return None

    # Decode the user JSON payload for convenience.
    user_raw = parsed.get("user")
    if user_raw:
        try:
            parsed["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            return None

    return parsed


def _dev_bypass_user_id() -> int:
    """Identity used by DEV_BYPASS_AUTH in local dev — never an admin signal."""
    dev_user_id = next(iter(ADMIN_USER_IDS), 0)
    if not dev_user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DEV_BYPASS_AUTH set but ADMIN_USER_IDS is empty.",
        )
    return int(dev_user_id)


async def require_admin(
    x_telegram_init_data: Optional[str] = Header(default=None),
) -> dict:
    """
    FastAPI dependency: verify initData and that the user has ADMIN role.
    Returns the parsed initData (with `user` already decoded).

    Local dev shortcut: if DEV_BYPASS_AUTH is set AND WEBHOOK_URL is empty,
    the first ADMIN_USER_IDS entry is treated as the caller (no HMAC check)
    — but their admin role is still checked against the `user` tab.
    """
    if _dev_bypass_active():
        dev_user_id = _dev_bypass_user_id()
        if not await is_admin(dev_user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required",
            )
        return {"user": {"id": dev_user_id, "first_name": "dev"}, "auth_date": "0"}

    parsed = _verify_signature(x_telegram_init_data or "")
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Telegram init data",
        )
    user = parsed.get("user") or {}
    user_id = user.get("id")
    if not user_id or not await is_admin(int(user_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return parsed


async def require_member(
    x_telegram_init_data: Optional[str] = Header(default=None),
) -> dict:
    """
    FastAPI dependency for endpoints that any verified Telegram user may
    call (admin OR regular member). Returns the parsed initData with an
    `is_admin` flag derived from the `user` tab role.

    Local-dev DEV_BYPASS_AUTH still applies — but `is_admin` reflects the
    real role on the user row, never a hardcoded True.
    """
    if _dev_bypass_active():
        dev_user_id = _dev_bypass_user_id()
        return {
            "user": {"id": dev_user_id, "first_name": "dev"},
            "auth_date": "0",
            "is_admin": await is_admin(dev_user_id),
        }

    parsed = _verify_signature(x_telegram_init_data or "")
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Telegram init data",
        )
    user = parsed.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing user in init data",
        )
    parsed["is_admin"] = await is_admin(int(user_id))
    return parsed
