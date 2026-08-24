"""
Unit tests for ABA PayWay transaction parser, member matching, and settlement formatting.
"""

import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.payway import is_payway_text, parse_payway_transaction
from bot.settlement import _tokens, format_receipt_message


def test_parser_exact_sample():
    text = (
        "$0.10 paid by HEN TY (*859) on Aug 24, 11:37 AM via ABA KHQR "
        "(ACLEDA Bank Plc.) at HEN TY. Trx. ID: 178754626218875, APV: 273833."
    )
    assert is_payway_text(text), "Should detect payway text"
    tx = parse_payway_transaction(text)
    assert tx is not None, "Failed to parse payway transaction"
    assert tx.amount == 0.10
    assert tx.currency == "USD"
    assert tx.amount_usd == 0.10
    assert tx.sender_name == "HEN TY"
    assert tx.account_mask == "*859"
    assert tx.date_str == "Aug 24, 11:37 AM"
    assert tx.payment_method == "ABA KHQR (ACLEDA Bank Plc.)"
    assert tx.merchant == "HEN TY"
    assert tx.trx_id == "178754626218875"
    assert tx.apv == "273833"
    print("✓ Exact sample parsed successfully")


def test_parser_khr():
    text = (
        "4,000 KHR paid by VUN SOPHANN (*123) on Aug 24, 12:00 PM via ABA KHQR "
        "at VONGSA. Trx. ID: 987654321, APV: 123456"
    )
    assert is_payway_text(text)
    tx = parse_payway_transaction(text, usd_khr_rate=4000.0)
    assert tx is not None
    assert tx.amount == 4000.0
    assert tx.currency == "KHR"
    assert tx.amount_usd == 1.00
    assert tx.sender_name == "VUN SOPHANN"
    assert tx.account_mask == "*123"
    assert tx.trx_id == "987654321"
    print("✓ KHR sample parsed successfully")

    # Test user's exact Riel sample:
    text_riel = "៛100 paid by HEN TY (*329) on Aug 24, 02:26 PM via ABA PAY at HOURT VONGSA. Trx. ID: 178755637145232, APV: 942001."
    assert is_payway_text(text_riel)
    tx_riel = parse_payway_transaction(text_riel, usd_khr_rate=4000.0)
    print("tx_riel result:", tx_riel)
    assert tx_riel is not None
    assert tx_riel.amount == 100.0
    assert tx_riel.currency == "KHR"
    assert tx_riel.sender_name == "HEN TY"
    assert tx_riel.trx_id == "178755637145232"
    assert tx_riel.apv == "942001"
    print("✓ Exact ៛100 sample parsed successfully")



def test_name_tokens_matching():
    # Test token matching for reversed names
    assert _tokens("HEN TY") == _tokens("TY HEN")
    assert _tokens("PHAN SEYHA") == _tokens("SEYHA PHAN")
    assert _tokens("HOURT VONGSA") == _tokens("VONGSA HOURT")
    print("✓ Name tokens matching works")


def test_receipt_formatting():
    from bot.payway import PayWayTransaction

    tx = PayWayTransaction(
        amount=5.00,
        currency="USD",
        amount_usd=5.00,
        sender_name="HEN TY",
        account_mask="*859",
        date_str="Aug 24, 11:37 AM",
        payment_method="ABA KHQR",
        merchant="HEN TY",
        trx_id="178754626218875",
        apv="273833",
        raw_text="",
    )
    user = {
        "user_id": "1921226603",
        "username": "ahh_tiii",
        "full_name": "Tii ♏️",
    }
    settled = [
        {"order_id": "ord1", "date": "2026-08-15", "amount": 1.50, "status": "PAID"},
        {"order_id": "ord2", "date": "2026-08-18", "amount": 2.00, "status": "PAID"},
        {"order_id": "ord3", "date": "2026-08-24", "amount": 1.50, "status": "PAID"},
    ]
    receipt = format_receipt_message(user, tx, settled, remaining_balance=0.0)
    assert "HEN TY" in receipt
    assert "Tii ♏️" in receipt
    assert "178754626218875" in receipt
    assert "2026-08-15" in receipt
    assert "Settled Lunch Orders" in receipt
    print("✓ Receipt formatting works")


def test_non_member_handling():
    from bot.payway import parse_payway_transaction

    text = (
        "$25.00 paid by STRANGER UNKNOWN (*999) on Aug 24, 02:15 PM via ABA KHQR "
        "at HEN TY. Trx. ID: 8888888888, APV: 111111."
    )
    tx = parse_payway_transaction(text)
    assert tx is not None
    assert tx.sender_name == "STRANGER UNKNOWN"
    assert tx.amount_usd == 25.00
    print("✓ Non-member transaction parsed cleanly")


if __name__ == "__main__":
    print("Running ABA PayWay feature tests...")
    test_parser_exact_sample()
    test_parser_khr()
    test_name_tokens_matching()
    test_receipt_formatting()
    test_non_member_handling()
    print("🎉 All PayWay tests passed!")

