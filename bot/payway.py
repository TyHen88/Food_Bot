"""
Parser for ABA PayWay / KHQR transaction notifications.

Handles messages sent by @PayWayByABA_bot or payment forward messages.
Example format:
    "$0.10 paid by HEN TY (*859) on Aug 24, 11:37 AM via ABA KHQR (ACLEDA Bank Plc.) at HEN TY. Trx. ID: 178754626218875, APV: 273833."
    "4,000 KHR paid by VUN SOPHANN (*123) on Aug 24, 12:00 PM via ABA KHQR at VONGSA. Trx. ID: 987654321, APV: 123456"
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class PayWayTransaction:
    amount: float
    currency: str  # "USD" or "KHR"
    amount_usd: float  # Equivalent in USD (if KHR, can be converted using exchange rate)
    sender_name: str  # Cleaned name, e.g. "HEN TY"
    account_mask: str  # e.g. "*859" or ""
    date_str: str  # e.g. "Aug 24, 11:37 AM"
    payment_method: str  # e.g. "ABA KHQR (ACLEDA Bank Plc.)"
    merchant: str  # e.g. "HEN TY" or "VONGSA"
    trx_id: str  # e.g. "178754626218875"
    apv: str  # e.g. "273833"
    raw_text: str


_KHMER_DIGITS_MAP = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


def _clean_text(text: str) -> str:
    """Strip zero-width spaces, normalize non-breaking spaces and line breaks."""
    if not text:
        return ""
    t = (
        text.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\u00a0", " ")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )
    return t


# Regex for USD: $0.10 or $1,234.50
_USD_PATTERN = re.compile(
    r"^\$(?P<amount>[\d\u17e0-\u17e9]+(?:,[\d\u17e0-\u17e9]{3})*(?:\.[\d\u17e0-\u17e9]{1,2})?)\s+"
    r"paid\s+by\s+(?P<sender>.+?)(?:\s*\((?P<mask>\*[0-9A-Za-z]+|\d+)\))?\s+"
    r"on\s+(?P<date>.+?)\s+"
    r"via\s+(?P<method>.+?)\s+"
    r"at\s+(?P<merchant>.+?)\.\s*"
    r"(?:Trx\.?\s*ID|Transaction\s*ID):\s*(?P<trx_id>\d+)"
    r"(?:,?\s*(?:APV|Approval\s*Code):\s*(?P<apv>[\w\d]+))?",
    re.IGNORECASE | re.DOTALL,
)

# Regex for KHR: 4,000 KHR or KHR 4,000 or ៛4,000 or ៛100 or 100៛
_KHR_PATTERN = re.compile(
    r"^(?:(?:KHR|៛|riel|riels)\s*)?(?P<amount>[\d\u17e0-\u17e9]+(?:,[\d\u17e0-\u17e9]{3})*(?:\.[\d\u17e0-\u17e9]{1,2})?)(?:\s*(?:KHR|៛|riel|riels))?\s+"
    r"paid\s+by\s+(?P<sender>.+?)(?:\s*\((?P<mask>\*[0-9A-Za-z]+|\d+)\))?\s+"
    r"on\s+(?P<date>.+?)\s+"
    r"via\s+(?P<method>.+?)\s+"
    r"at\s+(?P<merchant>.+?)\.\s*"
    r"(?:Trx\.?\s*ID|Transaction\s*ID):\s*(?P<trx_id>\d+)"
    r"(?:,?\s*(?:APV|Approval\s*Code):\s*(?P<apv>[\w\d]+))?",
    re.IGNORECASE | re.DOTALL,
)

# Generic fallback pattern if structure has minor punctuation differences
_GENERIC_PATTERN = re.compile(
    r"(?P<amount_raw>[\$\d\u17e0-\u17e9\.,\s]+(?:USD|KHR|៛|riel|riels)?)\s+"
    r"paid\s+by\s+(?P<sender>[^\n\r]+?)(?:\s*\((?P<mask>\*[0-9A-Za-z]+|\d+)\))?\s+"
    r"on\s+(?P<date>[^\n\r]+?)\s+"
    r"via\s+(?P<method>[^\n\r]+?)\s+"
    r"at\s+(?P<merchant>[^\n\r]+?)\.\s*"
    r"(?:Trx\.?\s*ID|Transaction\s*ID):\s*(?P<trx_id>\d+)",
    re.IGNORECASE,
)


def is_payway_text(text: str) -> bool:
    """Check if message is an ABA PayWay payment notification."""
    if not text:
        return False
    t = _clean_text(text).lower()
    return "paid by" in t and ("trx. id" in t or "trx id" in t or "transaction id" in t or "trx.id" in t)


def parse_payway_transaction(text: str, usd_khr_rate: float = 4000.0) -> Optional[PayWayTransaction]:
    """
    Parse an ABA PayWay notification string into PayWayTransaction.
    Returns None if text does not match the PayWay transaction format.
    """
    if not is_payway_text(text):
        return None

    cleaned_text = _clean_text(text)

    # Try USD match
    m = _USD_PATTERN.search(cleaned_text)
    if m:
        amount_str = m.group("amount").replace(",", "")
        amount = float(amount_str)
        sender = m.group("sender").strip()
        mask = m.group("mask") or ""
        date_str = m.group("date").strip()
        method = m.group("method").strip()
        merchant = m.group("merchant").strip()
        trx_id = m.group("trx_id").strip()
        apv = (m.group("apv") or "").strip().rstrip(".")

        return PayWayTransaction(
            amount=amount,
            currency="USD",
            amount_usd=round(amount, 2),
            sender_name=sender,
            account_mask=mask,
            date_str=date_str,
            payment_method=method,
            merchant=merchant,
            trx_id=trx_id,
            apv=apv,
            raw_text=text.strip(),
        )

    # Try KHR match
    m = _KHR_PATTERN.search(cleaned_text)
    if m:
        amount_str = m.group("amount").replace(",", "")
        amount = float(amount_str)
        sender = m.group("sender").strip()
        mask = m.group("mask") or ""
        date_str = m.group("date").strip()
        method = m.group("method").strip()
        merchant = m.group("merchant").strip()
        trx_id = m.group("trx_id").strip()
        apv = (m.group("apv") or "").strip().rstrip(".")

        # Convert KHR to USD
        rate = usd_khr_rate if usd_khr_rate > 0 else 4000.0
        amount_usd = round(amount / rate, 2)

        return PayWayTransaction(
            amount=amount,
            currency="KHR",
            amount_usd=amount_usd,
            sender_name=sender,
            account_mask=mask,
            date_str=date_str,
            payment_method=method,
            merchant=merchant,
            trx_id=trx_id,
            apv=apv,
            raw_text=text.strip(),
        )

    # Try generic fallback
    m = _GENERIC_PATTERN.search(cleaned_text)
    if m:
        raw_amt = m.group("amount_raw").strip()
        currency = "KHR" if ("khr" in raw_amt.lower() or "៛" in raw_amt) else "USD"
        digits_only = re.sub(r"[^\d\.]", "", raw_amt)
        try:
            amount = float(digits_only)
        except ValueError:
            return None

        rate = usd_khr_rate if usd_khr_rate > 0 else 4000.0
        amount_usd = amount if currency == "USD" else round(amount / rate, 2)

        sender = m.group("sender").strip()
        mask = m.group("mask") or ""
        date_str = m.group("date").strip()
        method = m.group("method").strip()
        merchant = m.group("merchant").strip()
        trx_id = m.group("trx_id").strip()

        return PayWayTransaction(
            amount=amount,
            currency=currency,
            amount_usd=amount_usd,
            sender_name=sender,
            account_mask=mask,
            date_str=date_str,
            payment_method=method,
            merchant=merchant,
            trx_id=trx_id,
            apv="",
            raw_text=text.strip(),
        )

    return None
