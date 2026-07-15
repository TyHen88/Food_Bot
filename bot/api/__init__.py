"""
FastAPI routers exposed under /api for the Mini App.

Each module owns one resource:
    auth       — Telegram WebApp init-data verification + admin gate
    settings   — GET/PUT for the `setting` tab
    schedules  — list + enable/disable for the `schedule` tab
    history    — paginated audit log
    orders     — read-only listing of the `order` tab (by date)
    polls      — read-only listing of polls + per-poll votes
    members    — read-only list of users with name/phone/status
"""

from fastapi import APIRouter

from . import (
    ai, history, invoices, me, members, orders, payers, polls, schedules,
    settings, templates,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(settings.router)
api_router.include_router(schedules.router)
api_router.include_router(history.router)
api_router.include_router(orders.router)
api_router.include_router(invoices.router)
api_router.include_router(payers.router)
api_router.include_router(polls.router)
api_router.include_router(members.router)
api_router.include_router(me.router)
api_router.include_router(templates.router)
api_router.include_router(ai.router)

__all__ = ["api_router"]
