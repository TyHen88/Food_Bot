"""
Google Sheets persistence layer for the Food Bot.

Layer responsibilities:
    schema     — single source of truth for tab names, columns, seed data
    client     — authenticated gspread connection + retry on 429/5xx
    bootstrap  — idempotent tab/header creation + default seeds
    cache      — per-tab in-memory cache with TTL + async refresh loop
    repo       — generic CRUD operations used by handlers/scheduler

Only `repo` and `cache` should be imported by application code. The rest
are infrastructure.
"""

from .schema import TABS, SEED_COMMON_CODE, SEED_SETTING
from .client import is_configured

__all__ = ["TABS", "SEED_COMMON_CODE", "SEED_SETTING", "is_configured"]
