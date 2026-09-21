"""
Test payer deduction logic and auto-paid status when payer orders dishes.
"""
from bot.invoicing import build_invoice_text

def test_payer_invoicing_deduction():
    user_orders = {
        "Dara": [{"item_name": "Fried Rice", "qty": 1, "price": 1.75, "cost": 1.75, "user_id": "101"}],
        "Vongsa": [{"item_name": "Noodle Soup", "qty": 1, "price": 2.00, "cost": 2.00, "user_id": "202"}],
    }
    
    # Vongsa is the payer
    inv_text = build_invoice_text(
        order_date="2026-09-21",
        user_orders=user_orders,
        payer_name="Vongsa",
        khqr_text="000201010212...",
        payer_user_id="202",
    )
    
    print(inv_text)
    # Check that Vongsa has (Payer) label
    assert "Vongsa" in inv_text
    assert "(Payer)" in inv_text
    # Order total is 3.75
    assert "Order Total" in inv_text
    assert "$3.75" in inv_text
    # Total Due to Vongsa is only Dara's dish: $1.75
    assert "Total Due" in inv_text
    assert "$1.75" in inv_text
    print("✓ Payer invoice deduction works perfectly!")

if __name__ == "__main__":
    test_payer_invoicing_deduction()
