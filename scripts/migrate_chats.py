#!/usr/bin/env python3
"""
One-shot migration: data/scheduled_chats.json -> `chat` tab.

Run once after setting GOOGLE_SHEET_ID + GOOGLE_CREDENTIALS_JSON.
Safe to re-run: idempotent on chat_id.

    python scripts/migrate_chats.py             # apply
    python scripts/migrate_chats.py --dry-run   # show what would change

The JSON file is NOT deleted automatically. After verifying the `chat` tab
looks right in the spreadsheet, remove it manually:
    Remove-Item data/scheduled_chats.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make the bot package importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.sheets import chats as sheets_chats   # noqa: E402
from bot.sheets.bootstrap import bootstrap     # noqa: E402
from bot.sheets.client import is_configured    # noqa: E402

JSON_FILE = Path(__file__).resolve().parent.parent / "data" / "scheduled_chats.json"


async def main(dry_run: bool) -> int:
    if not is_configured():
        print("ERROR: GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS_JSON must be set.")
        return 2

    if not JSON_FILE.exists():
        print(f"Nothing to migrate: {JSON_FILE} does not exist.")
        return 0

    try:
        raw = json.loads(JSON_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not read {JSON_FILE}: {e}")
        return 1

    if not isinstance(raw, list):
        print(f"ERROR: {JSON_FILE} is not a JSON array.")
        return 1

    chat_ids = [int(x) for x in raw]
    print(f"Found {len(chat_ids)} chat(s) in {JSON_FILE.name}: {chat_ids}")

    if dry_run:
        print("--dry-run: not writing anything to Sheets.")
        return 0

    # Make sure tabs/headers exist before writing.
    await bootstrap()

    existing = set(await sheets_chats.list_subscribed())
    written = 0
    for cid in chat_ids:
        if cid in existing:
            print(f"  skip  {cid} (already subscribed)")
            continue
        await sheets_chats.subscribe(cid, title="(migrated)", chat_type="unknown")
        print(f"  add   {cid}")
        written += 1

    # Let background writes drain before exit.
    await asyncio.sleep(2)

    print(f"\nDone. {written} new chat(s) added to `chat` tab.")
    print("Verify in the spreadsheet, then delete data/scheduled_chats.json manually.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
