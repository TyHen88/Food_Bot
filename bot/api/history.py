"""
/api/history — paginated read of the audit log.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ..sheets import repo
from ..sheets.client import is_configured
from .auth import require_admin

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def list_history(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: Optional[str] = Query(default=None),
    _: dict = Depends(require_admin),
) -> Dict[str, Any]:
    if not is_configured():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    rows = await repo.list_all("history")
    if event_type:
        rows = [r for r in rows if str(r.get("event_type", "")) == event_type]
    # Newest first by created_at (ISO 8601 is sortable as string).
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    total = len(rows)
    page = rows[offset : offset + limit]
    return {"items": page, "total": total, "limit": limit, "offset": offset}
