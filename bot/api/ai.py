"""
/api/ai — API endpoint to interact with Ollama for the Mini App Web UI.
"""

from datetime import datetime
import json
import logging
from typing import Any, Dict
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import ai
from ..config import TIMEZONE
from ..sheets import orders as sheets_orders
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
    """Execute AI query about orders or general questions."""
    user = auth.get("user") or {}
    user_id = user.get("id")
    username = user.get("username", "")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() or f"User{user_id}"

    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")

    user_query = body.query.strip()
    if not user_query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    try:
        try:
            tz = ZoneInfo(TIMEZONE)
            today_str = datetime.now(tz).strftime("%Y-%m-%d")
        except Exception:
            today_str = datetime.now().strftime("%Y-%m-%d")

        # Step 1: Parse intent and dates
        intent_info = await ai.parse_query_intent(user_query, today_str)
        q_type = intent_info.get("type", "external")
        start_date = intent_info.get("start_date")
        end_date = intent_info.get("end_date")

        order_data = []
        user_info = {
            "id": user_id,
            "username": username,
            "full_name": full_name
        }

        # Step 2: Fetch database if internal
        if q_type == "internal":
            if not start_date:
                start_date = "2026-05-01"
            if not end_date:
                end_date = today_str

            logger.info(f"API AI: Querying order sheet for range: {start_date} to {end_date}")
            all_orders = await sheets_orders.list_in_range(start_date, end_date)
            
            # Step 3: Filter strictly to user's orders
            for o in all_orders:
                items = []
                try:
                    items = json.loads(o.get("item", "[]"))
                except Exception:
                    pass
                
                for it in items:
                    it_uid = it.get("user_id")
                    it_name = it.get("name")
                    is_match = False
                    if it_uid and str(it_uid) == str(user_id):
                        is_match = True
                    elif username and it_name == username:
                        is_match = True
                    elif it_name == full_name:
                        is_match = True

                    if is_match:
                        order_data.append({
                            "order_date": o.get("order_date"),
                            "item_name": it.get("item_name"),
                            "qty": it.get("qty", 1)
                        })

        # Step 4: Generate response
        reply_text = await ai.generate_chat_response(
            user_query=user_query,
            query_type=q_type,
            order_data=order_data,
            user_info=user_info
        )

        return {
            "success": True,
            "response": reply_text,
            "query_type": q_type
        }

    except Exception as e:
        logger.error(f"API AI: Error processing query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
