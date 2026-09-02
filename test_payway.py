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
    assert "Remaining:" in receipt
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
def test_parser_with_forward_and_emoji_prefix():
    text_with_emoji = "💸 $1.75 paid by SOK DARA (*111) on Aug 25, 12:30 PM via ABA KHQR at HEN TY. Trx. ID: 123456789, APV: 999999."
    assert is_payway_text(text_with_emoji)
    tx = parse_payway_transaction(text_with_emoji)
    assert tx is not None
    assert tx.amount == 1.75
    assert tx.sender_name == "SOK DARA"
    assert tx.trx_id == "123456789"
    print("✓ PayWay text with emoji prefix parsed successfully")

    forwarded_text = "Forwarded message from @PayWayByABA_bot:\n$3.50 paid by CHANTHY PHAN (*555) on Aug 25, 01:15 PM via ABA KHQR at HEN TY. Trx. ID: 987654321, APV: 888888."
    assert is_payway_text(forwarded_text)
    tx_fwd = parse_payway_transaction(forwarded_text)
    assert tx_fwd is not None
    assert tx_fwd.amount == 3.50
    assert tx_fwd.sender_name == "CHANTHY PHAN"
    print("✓ Forwarded PayWay message parsed successfully")


def test_auto_invoicing_helpers():
    from bot.invoicing import build_invoice_text, clean_item_name

    assert clean_item_name("1. បាយសាច់ជ្រូក") == "បាយសាច់ជ្រូក"
    assert clean_item_name("• Fried Rice") == "Fried Rice"
    assert clean_item_name("- Soup") == "Soup"

    user_orders = {
        "Dara": [{"item_name": "Fried Rice", "qty": 1, "price": 1.75, "cost": 1.75}],
        "Seyha": [{"item_name": "Soup", "qty": 2, "price": 1.75, "cost": 3.50}],
    }
    inv_text = build_invoice_text(
        order_date="2026-09-02",
        user_orders=user_orders,
        payer_name="HEN TY",
        khqr_text="000201010212...",
        rate={"usd_khr": 4000.0, "rate_date": "2026-09-02"},
        currencies=["USD", "KHR"],
    )
    assert "2026-09-02" in inv_text
    assert "Dara" in inv_text
    assert "Seyha" in inv_text
    assert "$1.75" in inv_text
    assert "$3.50" in inv_text
    assert "$5.25" in inv_text
    assert "HEN TY" in inv_text
    print("✓ Invoicing helpers and text rendering work")


def test_auto_invoice_time_and_price_parser():
    from bot.handlers import _parse_time_arg, _parse_price_arg

    # Test time extraction with full sentence
    t1 = _parse_time_arg("/auto-invoice set to 11:59 AM in cambodia time")
    assert t1 is not None
    assert t1[0] == 11 and t1[1] == 59
    assert t1[2] == "11:59"
    assert t1[3] == "11:59 AM"

    # Test 12-hour PM
    t2 = _parse_time_arg("/auto-invoice set 1:30 PM")
    assert t2 is not None
    assert t2[0] == 13 and t2[1] == 30
    assert t2[2] == "13:30"
    assert t2[3] == "01:30 PM"

    # Test 24-hour
    t3 = _parse_time_arg("/auto-invoice 11:59")
    assert t3 is not None
    assert t3[0] == 11 and t3[1] == 59

    # Test price parsing
    p1 = _parse_price_arg("/auto-invoice 1.75")
    assert p1 == 1.75

    p2 = _parse_price_arg("/auto-invoice set 11:59 AM $2.50")
    assert p2 == 2.50
    print("✓ Auto-invoice time and price argument parser works perfectly")


if __name__ == "__main__":
    print("Running ABA PayWay & Invoicing feature tests...")
    test_parser_exact_sample()
    test_parser_khr()
    test_parser_with_forward_and_emoji_prefix()
    test_auto_invoicing_helpers()
    test_auto_invoice_time_and_price_parser()
    test_name_tokens_matching()
    test_receipt_formatting()
    test_non_member_handling()
    print("🎉 All PayWay & Invoicing tests passed!")

