"""
Settlement Engine for ABA PayWay transactions.

Matches incoming bank transactions to group members and settles single
or multi-date lunch debts using FIFO (First-In, First-Out).
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .people import is_same_person, name_variants, norm_name, strip_invisible
from .payway import PayWayTransaction
from .sheets import invoices as sheets_invoices
from .sheets import payments as sheets_payments
from .sheets import repo
from .sheets.client import is_configured

logger = logging.getLogger(__name__)


def _tokens(s: str) -> Set[str]:
    """Set of lowercase words in string."""
    return set(norm_name(s).split())


async def match_member(sender_name: str, account_mask: str = "") -> Optional[Dict[str, Any]]:
    """
    Match bank sender name with a user in the `user` tab.
    Matches against:
      1. Bank Name aliases (e.g. "HEN TY | TY HEN")
      2. Full Name / Username
      3. Reversed name tokens (e.g. "PHAN SEYHA" <-> "Seyha PHAN")
    """
    if not is_configured() or not sender_name:
        return None

    users = await repo.list_all("user")
    norm_sender = norm_name(sender_name)
    sender_toks = _tokens(sender_name)

    # Pass 1: Check explicit bank_name aliases
    for u in users:
        raw_bank = str(u.get("bank_name") or "")
        if not raw_bank:
            continue
        aliases = [a.strip() for a in raw_bank.replace(",", "|").split("|") if a.strip()]
        for a in aliases:
            norm_a = norm_name(a)
            if norm_sender == norm_a:
                return u
            # Also check if token sets match (e.g. "HEN TY" vs "TY HEN")
            if sender_toks and sender_toks == _tokens(a):
                return u

    # Pass 2: Check full_name / username and name_variants
    for u in users:
        variants = name_variants(
            username=u.get("username") or "",
            full_name=u.get("full_name") or "",
        )
        if norm_sender in variants:
            return u
        # Token match on full_name
        full_toks = _tokens(u.get("full_name") or "")
        if full_toks and full_toks == sender_toks:
            return u

    # Pass 3: Substring / loose token match if >= 2 words match
    if len(sender_toks) >= 2:
        for u in users:
            cand = f"{u.get('full_name', '')} {u.get('bank_name', '')}"
            cand_toks = _tokens(cand)
            if sender_toks.issubset(cand_toks) or cand_toks.issubset(sender_toks):
                return u

    return None


async def get_unpaid_invoices_for_user(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Find all unpaid invoices containing items for `user`, ordered oldest first.
    Returns list of:
      {
        "invoice_id": str,
        "order_date": str,
        "subtotal": float,
        "paid_amount": float,
        "due": float,
        "detail": dict,
      }
    """
    if not is_configured():
        return []

    uid = str(user.get("user_id") or "")
    names = name_variants(
        username=user.get("username") or "",
        full_name=user.get("full_name") or "",
    )

    all_invoices = await sheets_invoices.list_all()
    # Sort chronologically (oldest order_date first)
    all_invoices.sort(key=lambda x: (x.get("order_date") or "", x.get("created_at") or ""))

    unpaid = []
    for inv in all_invoices:
        details = inv.get("details") or []
        for d in details:
            if is_same_person(d.get("user_id"), d.get("user_name"), uid, names):
                subtotal = float(d.get("subtotal") or 0.0)
                is_paid = bool(d.get("paid"))
                paid_amount = float(d.get("paid_amount") or 0.0)
                due = round(subtotal - (subtotal if is_paid else paid_amount), 2)
                if due > 0.009:  # More than 1 cent due
                    unpaid.append({
                        "invoice_id": inv["invoice_id"],
                        "order_date": inv.get("order_date") or "Unknown Date",
                        "subtotal": subtotal,
                        "paid_amount": paid_amount,
                        "due": due,
                        "detail": d,
                    })
    return unpaid


def format_receipt_message(
    user: Dict[str, Any],
    tx: PayWayTransaction,
    settled: List[Dict[str, Any]],
    remaining_balance: float,
) -> str:
    """Format Telegram receipt message to post in group chat."""
    display_name = user.get("full_name") or user.get("username") or tx.sender_name
    username = f" (@{user['username']})" if user.get("username") else ""
    mask_str = f" ({tx.account_mask})" if tx.account_mask else ""

    paid_formatted = (
        f"{int(tx.amount):,} ៛ (≈ ${tx.amount_usd:.2f})"
        if tx.currency == "KHR"
        else f"${tx.amount_usd:.2f}"
    )

    lines = [
        "✅ <b>Payment Received!</b> (ABA PayWay)",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Member:</b> {display_name}{username}",
        f"🏦 <b>Sender:</b> {tx.sender_name}{mask_str}",
        f"💵 <b>Paid:</b> {paid_formatted} via {tx.payment_method}",
        f"🔢 <b>Trx ID:</b> <code>{tx.trx_id}</code>",
    ]

    if settled:
        lines.append("")
        lines.append("📋 <b>Settled Lunch Orders:</b>")
        for s in settled:
            status_icon = "✓" if s.get("status") == "PAID" else "⏳"
            lines.append(f"  {status_icon} <b>{s['date']}:</b> ${s['amount']:.2f} ({s['status']})")
    else:
        lines.append("")
        lines.append("ℹ️ <i>Payment recorded.</i>")

    return "\n".join(lines)


def format_unmatched_alert(tx: PayWayTransaction) -> str:
    """Alert message when a transaction doesn't match any known member."""
    mask_str = f" ({tx.account_mask})" if tx.account_mask else ""
    return (
        "⚠️ <b>Unmatched Payment Received (ABA PayWay)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 <b>Sender:</b> {tx.sender_name}{mask_str}\n"
        f"💵 <b>Amount:</b> ${tx.amount_usd:.2f} ({tx.amount} {tx.currency})\n"
        f"🔢 <b>Trx ID:</b> <code>{tx.trx_id}</code>\n"
        f"🕒 <b>Date:</b> {tx.date_str}\n\n"
        "<i>Admin can assign this transaction to a member in the Mini App Members/Payments tab.</i>"
    )


async def process_transaction_settlement(tx: PayWayTransaction) -> Dict[str, Any]:
    """
    Execute end-to-end settlement for an incoming PayWay transaction.
    """
    # 1. Deduplication check
    existing = await sheets_payments.find_by_trx_id(tx.trx_id)
    if existing:
        logger.info(f"Duplicate PayWay transaction ignored: Trx ID {tx.trx_id}")
        return {"status": "DUPLICATE", "payment": existing}

    # 2. Member matching
    matched_user = await match_member(tx.sender_name, tx.account_mask)

    if not matched_user:
        logger.warning(f"Unmatched PayWay transaction: {tx.sender_name} - ${tx.amount_usd}")
        payment = await sheets_payments.create_payment(
            payment_id=f"pay_{tx.trx_id}",
            trx_id=tx.trx_id,
            user_id="",
            sender_name=tx.sender_name,
            amount=tx.amount,
            currency=tx.currency,
            amount_usd=tx.amount_usd,
            settled_orders=[],
            status="UNMATCHED",
            apv=tx.apv,
            raw_text=tx.raw_text,
        )
        return {
            "status": "UNMATCHED",
            "payment": payment,
            "receipt_text": format_unmatched_alert(tx),
        }

    # 3. User matched -> FIFO settlement across unpaid invoices
    uid = str(matched_user.get("user_id") or "")
    names = name_variants(
        username=matched_user.get("username") or "",
        full_name=matched_user.get("full_name") or "",
    )

    unpaid_orders = await get_unpaid_invoices_for_user(matched_user)
    remaining_funds = tx.amount_usd
    settled_list = []

    tolerance = 0.05 if tx.currency == "KHR" else 0.01

    for item in unpaid_orders:
        if remaining_funds <= 0.001:
            break
        due = item["due"]
        is_fully_paid = (remaining_funds + tolerance) >= due
        pay_amount = min(remaining_funds, due)

        if is_fully_paid and remaining_funds < due:
            pay_amount = due

        # Mark as paid in Google Sheets invoice tab
        await sheets_invoices.mark_member_paid(
            item["invoice_id"],
            uid,
            names,
            payment_id=tx.trx_id,
            paid_amount=pay_amount,
            is_fully_paid=is_fully_paid,
        )

        settled_list.append({
            "order_id": item["invoice_id"],
            "date": item["order_date"],
            "amount": pay_amount,
            "status": "PAID" if is_fully_paid else "PARTIAL",
        })
        remaining_funds = max(0.0, remaining_funds - pay_amount)

    # 4. Save payment record
    payment = await sheets_payments.create_payment(
        payment_id=f"pay_{tx.trx_id}",
        trx_id=tx.trx_id,
        user_id=uid,
        sender_name=tx.sender_name,
        amount=tx.amount,
        currency=tx.currency,
        amount_usd=tx.amount_usd,
        settled_orders=settled_list,
        status="MATCHED",
        apv=tx.apv,
        raw_text=tx.raw_text,
    )

    # 5. Calculate remaining unpaid balance
    remaining_invoices = await get_unpaid_invoices_for_user(matched_user)
    total_remaining = sum(r["due"] for r in remaining_invoices)

    receipt = format_receipt_message(matched_user, tx, settled_list, total_remaining)

    return {
        "status": "MATCHED",
        "user": matched_user,
        "payment": payment,
        "settled": settled_list,
        "remaining_balance": total_remaining,
        "receipt_text": receipt,
    }
