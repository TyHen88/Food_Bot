"""
Bootstrap and seed production Google Spreadsheet.
Spreadsheet ID: 1PONK-CZKCRRVqZY8TR6uHgYu_1GrdYKPr5G32coWdP0
"""

import sys
import os
import time
from pathlib import Path
from typing import List

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gspread.utils import rowcol_to_a1
from bot.sheets.client import get_client
from bot.sheets.schema import (
    TABS,
    SEED_SETTING,
    SEED_SCHEDULE,
    SEED_COMMON_CODE,
    SEED_TEMPLATE,
    SCHEMA_VERSION,
)

PROD_SHEET_ID = "1PONK-CZKCRRVqZY8TR6uHgYu_1GrdYKPr5G32coWdP0"

MEMBERS = [
    {"username": "seyhaphan", "full_name": "Seyha PHAN", "bank_name": "PHAN SEYHA", "role": "ADMIN"},
    {"username": "soeuksophanit", "full_name": "Soeuk Sophanit", "bank_name": "SOEUK SOPHANIT", "role": "MEMBER"},
    {"username": "songchanmoni", "full_name": "Song Chanmoni", "bank_name": "SONG CHAN MONI", "role": "MEMBER"},
    {"username": "ahh_tiii", "full_name": "Tii ♏️", "bank_name": "HEN TY | TY HEN", "role": "ADMIN"},
    {"username": "vongsahuot", "full_name": "Vongsa HUOT", "bank_name": "HOURT VONGSA | VONGSA HUOT", "role": "ADMIN"},
    {"username": "sophann_ah", "full_name": "vun sophann - វុន សុផាន់", "bank_name": "VUN SOPHANN", "role": "MEMBER"},
    {"username": "YonYuos", "full_name": "Yon Yuos", "bank_name": "YON YUOS", "role": "MEMBER"},
    {"username": "vakhimnuon", "full_name": "Nuon Vakhim", "bank_name": "NUON VAKHIM", "role": "MEMBER"},
    {"username": "somnangpho", "full_name": "សំណាង", "bank_name": "PHO SOMNANG", "role": "MEMBER"},
]


def run_bootstrap_prod():
    print(f"Connecting to Google Sheets client...")
    client = get_client()
    if not client:
        print("ERROR: Could not initialise Google Sheets client (check credentials.json)")
        return

    print(f"Opening production spreadsheet: {PROD_SHEET_ID}...")
    ss = client.open_by_key(PROD_SHEET_ID)
    print(f"Successfully opened: '{ss.title}'")

    existing = {ws.title: ws for ws in ss.worksheets()}
    created: List[str] = []
    extended: List[str] = []

    print("\n--- 1. Ensuring Schema (Tabs & Headers) ---")
    for tab, headers in TABS.items():
        time.sleep(0.5)  # Avoid rate limit
        if tab not in existing:
            ws = ss.add_worksheet(title=tab, rows=200, cols=max(len(headers), 10))
            ws.update(values=[headers], range_name=f"A1:{rowcol_to_a1(1, len(headers))}")
            ws.freeze(rows=1)
            created.append(tab)
            print(f"  + Created tab '{tab}' ({len(headers)} cols)")
            continue

        ws = existing[tab]
        first_row = ws.row_values(1)
        missing = [h for h in headers if h not in first_row]
        if missing:
            start_col = len(first_row) + 1
            end_col = start_col + len(missing) - 1
            current_cols = ws.col_count
            if current_cols < end_col:
                ws.add_cols(end_col - current_cols)
            ws.update(
                values=[missing],
                range_name=f"{rowcol_to_a1(1, start_col)}:{rowcol_to_a1(1, end_col)}",
            )
            extended.append(tab)
            print(f"  * Appended missing columns to '{tab}': {missing}")
        else:
            print(f"  ✓ Tab '{tab}' is up to date")
        ws.freeze(rows=1)

    print(f"\nSchema status: created={created or 'none'}, extended={extended or 'none'}")

    print("\n--- 2. Seeding Default Settings / Schedules ---")
    for tab_name, rows in [
        ("common_code", SEED_COMMON_CODE),
        ("setting", SEED_SETTING),
        ("schedule", SEED_SCHEDULE),
        ("template", SEED_TEMPLATE),
    ]:
        time.sleep(0.5)
        ws = ss.worksheet(tab_name)
        existing_rows = ws.get_all_values()
        if len(existing_rows) <= 1:
            headers = ws.row_values(1)
            values = [[r.get(c, "") for c in headers] for r in rows]
            if values:
                ws.append_rows(values, value_input_option="USER_ENTERED")
                print(f"  + Seeded {len(values)} rows to '{tab_name}'")
        else:
            print(f"  ✓ '{tab_name}' already contains {len(existing_rows)-1} data rows")

    print("\n--- 3. Updating / Seeding Member Bank Names in 'user' Tab ---")
    user_ws = ss.worksheet("user")
    user_records = user_ws.get_all_records()
    user_headers = user_ws.row_values(1)
    
    # Ensure bank_name is in headers
    if "bank_name" not in user_headers:
        user_ws.add_cols(1)
        user_ws.update(values=[["bank_name"]], range_name=f"{rowcol_to_a1(1, len(user_headers)+1)}:A1")
        user_headers.append("bank_name")

    bank_name_col_idx = user_headers.index("bank_name") + 1
    role_col_idx = user_headers.index("role") + 1 if "role" in user_headers else None
    full_name_col_idx = user_headers.index("full_name") + 1 if "full_name" in user_headers else None

    for m in MEMBERS:
        time.sleep(0.3)
        uname = m["username"].lower()
        matched_row_idx = None
        
        for idx, u in enumerate(user_records, start=2):
            if str(u.get("username", "")).lower().lstrip("@") == uname:
                matched_row_idx = idx
                break

        if matched_row_idx:
            user_ws.update_cell(matched_row_idx, bank_name_col_idx, m["bank_name"])
            if full_name_col_idx:
                user_ws.update_cell(matched_row_idx, full_name_col_idx, m["full_name"])
            if role_col_idx:
                user_ws.update_cell(matched_row_idx, role_col_idx, m["role"])
            print(f"  * Updated row {matched_row_idx}: @{m['username']} -> bank_name='{m['bank_name']}'")
        else:
            new_row = [
                f"usr_{m['username']}",  # user_id
                m["username"],           # username
                m["full_name"],          # full_name
                m["role"],               # role
                "KH",                    # language
                "",                      # phone_number
                "",                      # chat_id
                "",                      # dietary_notes
                "2026-08-24T00:00:00Z",  # created_at
                "2026-08-24T00:00:00Z",  # last_active_at
            ]
            # Map according to actual headers
            row_dict = {
                "user_id": f"usr_{m['username']}",
                "username": m["username"],
                "full_name": m["full_name"],
                "bank_name": m["bank_name"],
                "role": m["role"],
                "language": "KH",
                "phone_number": "",
                "chat_id": "",
                "dietary_notes": "",
                "created_at": "2026-08-24T00:00:00Z",
                "last_active_at": "2026-08-24T00:00:00Z",
            }
            row_values = [row_dict.get(c, "") for c in user_headers]
            user_ws.append_row(row_values, value_input_option="USER_ENTERED")
            print(f"  + Added member @{m['username']} ({m['full_name']}) -> bank_name='{m['bank_name']}'")

    print("\n--- 4. Setting Metadata Schema Version ---")
    meta_ws = ss.worksheet("_meta") if "_meta" in ss.worksheets() else None
    if meta_ws:
        meta_ws.update(values=[["schema_version", SCHEMA_VERSION], ["updated_at", "2026-08-24T00:00:00Z"]], range_name="A1:B2")
        print(f"  ✓ _meta schema_version set to {SCHEMA_VERSION}")

    print("\n🎉 Production spreadsheet bootstrap and member seeding completed successfully!")


if __name__ == "__main__":
    run_bootstrap_prod()
