#!/usr/bin/env python3
"""
Simple test script to verify the bot structure works correctly.
"""

import sys
import os

def test_imports():
    """Test that all modules can be imported correctly."""
    try:
        # Test bot package import
        from bot import build_application
        print("✓ build_application imported successfully")
        
        # Test individual modules
        from bot.config import BOT_TOKEN, setup_logging
        print("✓ Config module imported successfully")
        
        from bot.utils import extract_menu_options, is_food_menu_text
        print("✓ Utils module imported successfully")
        
        from bot.menu_processor import process_food_menu
        print("✓ Menu processor imported successfully")
        
        from bot.handlers import setup_handlers
        print("✓ Handlers module imported successfully")
        
        from bot.scheduler import setup_scheduler
        print("✓ Scheduler module imported successfully")
        
        return True
        
    except ImportError as e:
        print(f"✗ Import error: {e}")
        return False
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        return False

def test_config():
    """Test configuration loading."""
    try:
        from bot.config import setup_logging
        setup_logging()
        print("✓ Logging setup successful")
        return True
    except Exception as e:
        print(f"✗ Config test failed: {e}")
        return False

def test_utils():
    """Test utility functions."""
    try:
        from bot.utils import extract_menu_options, is_food_menu_text
        
        # Test menu text detection (Khmer 1-10)
        test_menu_kh = """ម្ហូបថ្ងៃ
១. បបរសាច់គោ
២. សម្លកកូរ
៣. អាម៉ុក
៤. ឆាក្តៅ
៥. សម្លម្ជូរ
៦. ត្រីចៀន
៧. ពងទាចៀន
៨. ស៊ុបមាន់
៩. ឆាត្រកួន
១០. បាយឆា"""
        
        assert is_food_menu_text(test_menu_kh), "Menu text detection failed (Khmer)"
        options_kh = extract_menu_options(test_menu_kh)
        assert len(options_kh) == 10, f"Expected 10 Khmer options, got {len(options_kh)}"
        assert options_kh[9] == "បាយឆា", f"Expected last item 'បាយឆា', got '{options_kh[9]}'"
        print("✓ Menu text detection and 1-10 option extraction works for Khmer (១-១០)")

        # Test Arabic 1-10
        test_menu_en = """Today Menu:
1. Fried Rice
2. Chicken Soup
3. Beef Lok Lak
4. Fish Amok
5. Stir-fried Pork
6. Green Curry
7. Pad Thai
8. Spring Rolls
9. Mango Salad
10. Tom Yum"""
        assert is_food_menu_text(test_menu_en), "Menu text detection failed (Arabic numbers)"
        options_en = extract_menu_options(test_menu_en)
        assert len(options_en) == 10, f"Expected 10 Arabic options, got {len(options_en)}"
        assert options_en[9] == "Tom Yum", f"Expected last item 'Tom Yum', got '{options_en[9]}'"
        print("✓ Menu text detection and 1-10 option extraction works for Arabic (1-10)")

        # Test rejection of order summary lines (e.g. "1) ...")
        order_summary = """1) Fried Rice x 2
2) Chicken Soup x 1"""
        assert not is_food_menu_text(order_summary), "Order summary should not be detected as menu"
        assert len(extract_menu_options(order_summary)) == 0, "Order summary lines should not be extracted as options"
        print("✓ Order summary lines correctly ignored")
        
        return True
        
    except Exception as e:
        print(f"✗ Utils test failed: {e}")
        return False

def test_bot_setup():
    """Test bot setup without running."""
    try:
        from bot import build_application
        
        # Build application instance
        app = build_application()
        print("✓ Application instance created successfully")
        return True
        
    except Exception as e:
        print(f"✗ Bot setup test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("Testing Telegram Food Poll Bot structure...")
    print("=" * 50)
    
    tests = [
        ("Import Test", test_imports),
        ("Config Test", test_config),
        ("Utils Test", test_utils),
        ("Bot Setup Test", test_bot_setup),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\nRunning {test_name}...")
        if test_func():
            passed += 1
        else:
            print(f"❌ {test_name} failed")
    
    print("\n" + "=" * 50)
    print(f"Tests passed: {passed}/{total}")
    
    if passed == total:
        print("🎉 All tests passed! The bot structure is working correctly.")
        print("\nTo run the bot:")
        print("1. Copy env.example to .env")
        print("2. Add your BOT_TOKEN to .env")
        print("3. Run: python main.py")
        return 0
    else:
        print("❌ Some tests failed. Please check the errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main()) 