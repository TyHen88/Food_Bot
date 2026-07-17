"""
/api/ai — the Mini App's AI assistant endpoint.

Thin wrapper around bot.ai.answer_query, which enforces the assistant's
boundaries: internal-only answers, grounded on the caller's OWN data from
the user / order / invoice / poll tabs, with canned refusals for
off-topic and privacy-crossing questions.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import ai
from .auth import require_member

router = APIRouter(prefix="/ai", tags=["ai"])
logger = logging.getLogger(__name__)


class AIQueryRequest(BaseModel):
    query: str


@router.post("")
async def ask_ai(
    body: AIQueryRequest,
    auth: dict = Depends(require_member)
) -> Dict[str, Any]:
    """Answer a question about the caller's own orders/invoices/polls,
    or about how the bot works."""
    user = auth.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")

    user_query = body.query.strip()
    if not user_query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    user_info = {
        "id": user_id,
        "username": user.get("username", ""),
        "full_name": f"{first_name} {last_name}".strip() or f"User{user_id}",
    }

    try:
        result = await ai.answer_query(user_query, user_info)
        return {
            "success": True,
            "response": result["response"],
            "query_type": result["query_type"],
        }
    except Exception as e:
        logger.error(f"API AI: Error processing query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
