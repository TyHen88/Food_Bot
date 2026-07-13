"""
/api/invoices — admin-only per-user invoice generation.

Phase 1 (this file, the spike) proves the riskiest unknown before we build
the rest: a Telegram bot CANNOT start a private chat with a user — it may
only DM someone who has messaged it privately first. So per-user invoice
DMs can only reach users whose `can_dm` flag is TRUE (set in
handlers._record_user the moment a user interacts in a private chat).

`GET /invoices/reachability` groups an order's items by user and reports,
per person, whether the bot can DM them — WITHOUT sending anything. It's the
go/no-go check that drives the whole invoice UX (who gets a DM vs who needs
a group-mention + deep-link fallback).
"""

import html
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden

from .. import exchange
from ..sheets import invoice_links
from ..sheets import payers as sheets_payers
from ..sheets import repo
from ..sheets.client import is_configured
from .auth import caller_user_id, require_admin
from .prices import price_index

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])

# assets/ lives at the repo root: bot/api/invoices.py -> bot/api -> bot -> root.
_ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"


def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _user_index() -> Dict[str, Dict[str, Any]]:
    """{user_id: {name, can_dm, dm_chat_id}} from the `user` tab."""
    out: Dict[str, Dict[str, Any]] = {}
    for u in await repo.list_all("user"):
        uid = str(u.get("user_id", "")).strip()
        if not uid:
            continue
        out[uid] = {
            "name": (
                str(u.get("username", "")).strip()
                or str(u.get("full_name", "")).strip()
                or f"User{uid}"
            ),
            "can_dm": str(u.get("can_dm", "")).strip().upper() == "TRUE",
            "dm_chat_id": str(u.get("dm_chat_id", "")).strip(),
        }
    return out


def _group_by_user(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """user_id -> {name, items: [{item_name, qty}], qty_total}.

    Items with no user_id (admin-added dishes with no Telegram user) can't be
    DM'd to anyone, so they're skipped here and surfaced separately by the
    caller if needed.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for it in items:
        uid = str(it.get("user_id", "")).strip()
        if not uid:
            continue
        g = grouped.setdefault(
            uid, {"name": str(it.get("name", "")).strip(), "items": [], "qty_total": 0}
        )
        try:
            qty = int(it.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        g["items"].append(
            {"item_name": str(it.get("item_name", "")).strip(), "qty": qty}
        )
        g["qty_total"] += qty
    return grouped


@router.get("/reachability")
async def reachability(
    order_id: str = Query(..., description="The order to invoice."),
    _: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Per-user DM reachability for one order. Sends nothing — this is the
    go/no-go check for the invoice feature. Admin-only.

    Returns each recipient with `can_dm`, plus reachable/unreachable counts so
    the Mini App can warn the admin before generating invoices.
    """
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sheets not configured",
        )

    row = await repo.find_by_pk("order", order_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )

    grouped = _group_by_user(_parse_items(row.get("item")))
    users = await _user_index()

    recipients: List[Dict[str, Any]] = []
    for uid, g in grouped.items():
        u = users.get(uid, {})
        recipients.append(
            {
                "user_id": uid,
                "name": u.get("name") or g["name"] or f"User{uid}",
                "can_dm": bool(u.get("can_dm")),
                "item_count": g["qty_total"],
                "items": g["items"],
            }
        )
    # Unreachable first so the admin sees who needs the fallback at a glance.
    recipients.sort(key=lambda r: (r["can_dm"], r["name"].lower()))

    reachable = sum(1 for r in recipients if r["can_dm"])
    return {
        "order_id": order_id,
        "recipients": recipients,
        "reachable_count": reachable,
        "unreachable_count": len(recipients) - reachable,
    }


# ---------------------------------------------------------------------------
# Invoice generation — compute per-user amounts and DM each ordering user.
# ---------------------------------------------------------------------------

def _round_khr(value: float) -> int:
    """Riel is rarely transacted below 100៛ — round totals to the nearest 100."""
    return int(round(value / 100.0) * 100)


def _compute_lines(
    items: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    rate: float,
) -> Dict[str, Any]:
    """Turn a user's [{item_name, qty}] into priced lines + USD/KHR totals.

    Each price row carries its own currency; we convert to the other currency
    using `rate` (riel per USD). Items with no price row are returned in
    `unpriced` so the caller can warn the admin before sending."""
    lines: List[Dict[str, Any]] = []
    total_usd = 0.0
    total_khr = 0.0
    unpriced: List[str] = []
    for it in items:
        name = it["item_name"]
        qty = int(it.get("qty", 1) or 1)
        p = prices.get(name)
        if not p:
            unpriced.append(name)
            lines.append({
                "item_name": name, "qty": qty, "unit_price": 0.0,
                "currency": "", "line_usd": 0.0, "line_khr": 0.0,
            })
            continue
        unit = float(p["price"])
        currency = p["currency"]
        if currency == "KHR":
            line_khr = qty * unit
            line_usd = (line_khr / rate) if rate else 0.0
        else:  # USD
            line_usd = qty * unit
            line_khr = line_usd * rate
        total_usd += line_usd
        total_khr += line_khr
        lines.append({
            "item_name": name, "qty": qty, "unit_price": unit,
            "currency": currency,
            "line_usd": round(line_usd, 2), "line_khr": _round_khr(line_khr),
        })
    return {
        "lines": lines,
        "total_usd": round(total_usd, 2),
        "total_khr": _round_khr(total_khr),
        "unpriced": unpriced,
    }


def _format_invoice_text(
    *, name: str, order_date: str, lines: List[Dict[str, Any]],
    total_usd: float, total_khr: int, rate: float,
    payer_name: str, intro: Optional[str],
) -> str:
    """The friendly per-user invoice message body (also used as the QR caption)."""
    greeting = f"Hello {name} 👋"
    body = intro or "Here's your lunch order — thanks for ordering! 🍱"
    out = [greeting, "", body, ""]
    if order_date:
        out.append(f"🧾 Order — {order_date}")
    for ln in lines:
        if ln["currency"]:
            out.append(f" • {ln['item_name']} × {ln['qty']}   ${ln['line_usd']:.2f}")
        else:
            out.append(f" • {ln['item_name']} × {ln['qty']}   (no price set)")
    out.append("")
    out.append(f"💵 Total: ${total_usd:.2f}  ≈  {total_khr:,}៛")
    out.append(f"   (rate: 1 USD = {rate:,.0f}៛)")
    if payer_name:
        out.append("")
        out.append(f"Please pay {payer_name} via the KHQR below 🙏")
    return "\n".join(out)


def _payer_qr_path(payer_row: Optional[Dict[str, Any]]) -> Optional[Path]:
    """Resolve the payer's QR image to an assets/ file, or None."""
    if not payer_row:
        return None
    fname = str(payer_row.get("qr_filename", "")).strip()
    if not fname:
        return None
    p = _ASSETS_DIR / fname
    return p if p.exists() else None


async def _send_one_invoice(
    bot, *, dm_chat_id: str, text: str, qr_path: Optional[Path],
    khqr_text: str,
) -> None:
    """Send a single user their invoice DM (photo+caption when a QR exists).

    Telegram photo captions cap at 1024 chars; our invoices are short, but if
    one ever exceeds that we send the text first, then the QR separately."""
    chat_id = int(dm_chat_id)
    if qr_path is not None:
        if len(text) <= 1024:
            with open(qr_path, "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
            with open(qr_path, "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo)
        return
    # No QR image — send the text, append a KHQR payload string if we have one.
    if khqr_text:
        text = f"{text}\n\nKHQR: {khqr_text}"
    await bot.send_message(chat_id=chat_id, text=text)


_FALLBACK_MODES = {"none", "group", "deeplink"}


class GenerateBody(BaseModel):
    order_id: str
    message: Optional[str] = None   # optional friendly intro override
    dry_run: bool = False           # compute + preview without sending
    force: bool = False             # resend even if already invoiced
    # How to reach users who can't be DM'd (no private chat with the bot yet):
    #   "none"     — skip them (default)
    #   "group"    — post their bill in the group, pinging them
    #   "deeplink" — post a group button that delivers their bill privately
    fallback: str = "none"


@router.post("/generate")
async def generate_invoices(
    body: GenerateBody,
    request: Request,
    auth: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Generate per-user invoices for an order and DM each ordering user.

    Admin-only. With ``dry_run=true`` it computes every recipient's amount and
    reachability but sends nothing — this is what the invoice page calls to
    preview. Otherwise it DMs each reachable user their items + total (USD &
    KHR) + the payer's KHQR, and stamps the order as invoiced.
    """
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sheets not configured",
        )

    order = await repo.find_by_pk("order", body.order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )

    already = str(order.get("invoiced_at", "")).strip()
    if already and not body.force and not body.dry_run:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already invoiced at {already}. Resend with force=true.",
        )

    grouped = _group_by_user(_parse_items(order.get("item")))
    users = await _user_index()
    prices = await price_index()
    rate_info = await exchange.get_rate_info()
    rate = float(rate_info["rate"])

    # Payer (order owner) + their QR, shared across every recipient's message.
    payer_id = str(order.get("user_id", "")).strip()
    payer_row = await sheets_payers.get(payer_id) if payer_id else None
    payer_name = (
        (payer_row or {}).get("username")
        or (payer_row or {}).get("full_name")
        or users.get(payer_id, {}).get("name", "")
        or str(order.get("username", "")).strip()
    )
    qr_path = _payer_qr_path(payer_row)
    payer_qr_filename = str((payer_row or {}).get("qr_filename", "")).strip()
    khqr_text = str((payer_row or {}).get("khqr_text", "")).strip()
    order_date = str(order.get("order_date", "")).strip()

    bot = request.app.state.application.bot

    results: List[Dict[str, Any]] = []
    sent = failed = unreachable = 0
    group_posted = deeplink_sent = 0

    for uid, g in grouped.items():
        u = users.get(uid, {})
        name = u.get("name") or g["name"] or f"User{uid}"
        amounts = _compute_lines(g["items"], prices, rate)
        text = _format_invoice_text(
            name=name, order_date=order_date, lines=amounts["lines"],
            total_usd=amounts["total_usd"], total_khr=amounts["total_khr"],
            rate=rate, payer_name=payer_name, intro=body.message,
        )
        rec: Dict[str, Any] = {
            "user_id": uid,
            "name": name,
            "can_dm": bool(u.get("can_dm")),
            "lines": amounts["lines"],
            "total_usd": amounts["total_usd"],
            "total_khr": amounts["total_khr"],
            "unpriced": amounts["unpriced"],
            "preview_text": text,
            "status": "preview",
        }

        if body.dry_run:
            results.append(rec)
            continue

        dm_chat_id = u.get("dm_chat_id") or uid
        if not u.get("can_dm"):
            rec["status"] = "skipped_unreachable"
            unreachable += 1
            results.append(rec)
            continue

        try:
            await _send_one_invoice(
                bot, dm_chat_id=str(dm_chat_id), text=text,
                qr_path=qr_path, khqr_text=khqr_text,
            )
            rec["status"] = "sent"
            sent += 1
        except Forbidden:
            # The user blocked the bot or never started it — self-heal the
            # reachability flag so future previews show them as unreachable.
            rec["status"] = "skipped_unreachable"
            rec["error"] = "bot blocked or chat never started"
            unreachable += 1
            try:
                await repo.update("user", uid, {"can_dm": "FALSE"})
            except Exception:
                pass
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = str(e)
            failed += 1
            logger.error(f"Invoice DM to {uid} failed: {e}")
        results.append(rec)

    # Reach the un-DM-able via the admin's chosen fallback.
    fallback_mode = (body.fallback or "none").strip().lower()
    if fallback_mode not in _FALLBACK_MODES:
        fallback_mode = "none"
    chat_id_raw = str(order.get("chat_id", "")).strip()
    if (not body.dry_run and fallback_mode in ("group", "deeplink")
            and chat_id_raw):
        unreachable_recs = [r for r in results if r["status"] == "skipped_unreachable"]
        try:
            group_chat_id = int(chat_id_raw)
        except ValueError:
            group_chat_id = None

        if group_chat_id is not None and unreachable_recs:
            if fallback_mode == "group":
                for r in unreachable_recs:
                    try:
                        nm = html.escape(r["name"])
                        mention = f'<a href="tg://user?id={r["user_id"]}">{nm}</a>'
                        msg = f"{mention}\n\n{html.escape(r['preview_text'])}"
                        await bot.send_message(
                            chat_id=group_chat_id, text=msg, parse_mode="HTML",
                        )
                        r["status"] = "group_posted"
                        group_posted += 1
                        unreachable -= 1
                    except Exception as e:
                        r["error"] = f"group post failed: {e}"
                        logger.error(f"Group invoice post for {r['user_id']} failed: {e}")
                # Post the payer QR once so the group can pay.
                if qr_path is not None and group_posted:
                    try:
                        cap = f"KHQR — pay {payer_name}" if payer_name else "KHQR"
                        with open(qr_path, "rb") as photo:
                            await bot.send_photo(chat_id=group_chat_id, photo=photo, caption=cap)
                    except Exception:
                        pass

            elif fallback_mode == "deeplink":
                bot_username = bot.username
                if not bot_username:
                    try:
                        bot_username = (await bot.get_me()).username
                    except Exception:
                        bot_username = None
                buttons: List[List[InlineKeyboardButton]] = []
                for r in unreachable_recs:
                    try:
                        token = invoice_links.new_token()
                        await invoice_links.create(
                            token, order_id=body.order_id, user_id=r["user_id"],
                            text=r["preview_text"], qr_filename=payer_qr_filename,
                            khqr_text=khqr_text,
                        )
                        if bot_username:
                            url = f"https://t.me/{bot_username}?start={token}"
                            buttons.append([InlineKeyboardButton(f"🧾 {r['name']}", url=url)])
                        r["status"] = "deeplink_sent"
                        deeplink_sent += 1
                        unreachable -= 1
                    except Exception as e:
                        r["error"] = f"deeplink failed: {e}"
                        logger.error(f"Deep-link invoice for {r['user_id']} failed: {e}")
                if buttons and bot_username:
                    try:
                        await bot.send_message(
                            chat_id=group_chat_id,
                            text="🧾 Your invoice is ready — tap your name to get it privately:",
                            reply_markup=InlineKeyboardMarkup(buttons),
                        )
                    except Exception as e:
                        logger.error(f"Deep-link group message failed: {e}")

    # Problems first, then fallbacks, then sent, then preview.
    _order = {
        "failed": 0, "skipped_unreachable": 1,
        "group_posted": 2, "deeplink_sent": 2, "sent": 3, "preview": 4,
    }
    results.sort(key=lambda r: (_order.get(r["status"], 9), r["name"].lower()))

    response = {
        "order_id": body.order_id,
        "dry_run": body.dry_run,
        "fallback": fallback_mode,
        "rate": rate,
        "rate_source": rate_info.get("source", ""),
        "rate_updated_at": rate_info.get("updated_at", ""),
        "payer": {"user_id": payer_id, "name": payer_name, "has_qr": qr_path is not None},
        "recipients": results,
        "sent_count": sent,
        "unreachable_count": unreachable,
        "failed_count": failed,
        "group_posted_count": group_posted,
        "deeplink_count": deeplink_sent,
        "recipient_count": len(results),
    }

    if not body.dry_run:
        stamp = repo.now_iso()
        await repo.update("order", body.order_id, {"invoiced_at": stamp})
        response["invoiced_at"] = stamp
        from ..sheets import events as sheets_events
        await sheets_events.emit(
            "INVOICE_GENERATED",
            entity_type="order", entity_id=body.order_id,
            user_id=int(caller_user_id(auth)) if caller_user_id(auth).isdigit() else None,
            chat_id=int(order["chat_id"]) if str(order.get("chat_id", "")).lstrip("-").isdigit() else None,
            payload={
                "sent": sent, "unreachable": unreachable, "failed": failed,
                "group_posted": group_posted, "deeplink": deeplink_sent,
                "fallback": fallback_mode,
            },
        )

    return response
