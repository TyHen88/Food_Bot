"""
Script to seed / update member bank names from user list.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.sheets import repo
from bot.sheets.client import is_configured

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

async def seed():
    if not is_configured():
        print("Sheets not configured")
        return
    existing_users = await repo.list_all("user")
    for m in MEMBERS:
        uname = m["username"].lower()
        found = None
        for u in existing_users:
            if str(u.get("username", "")).lower().lstrip("@") == uname:
                found = u
                break
        if found:
            pk = found.get("user_id")
            await repo.update("user", pk, {
                "bank_name": m["bank_name"],
                "full_name": m["full_name"],
                "role": m["role"],
            })
            print(f"Updated user @{m['username']} with bank_name='{m['bank_name']}'")
        else:
            await repo.create("user", {
                "user_id": "",
                "username": m["username"],
                "full_name": m["full_name"],
                "bank_name": m["bank_name"],
                "role": m["role"],
                "language": "KH",
                "phone_number": "",
                "chat_id": "",
                "dietary_notes": "",
                "created_at": repo.now_iso(),
                "last_active_at": repo.now_iso(),
            })
            print(f"Created user @{m['username']} with bank_name='{m['bank_name']}'")

if __name__ == "__main__":
    asyncio.run(seed())
