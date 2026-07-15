"""
AI utility module to interact with Ollama service.
"""

import json
import logging
from datetime import datetime
import httpx

from .config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL

logger = logging.getLogger(__name__)


async def _call_ollama(messages: list) -> str:
    """Make an async call to the Ollama /api/chat endpoint."""
    if not OLLAMA_API_URL:
        raise ValueError("OLLAMA_API_URL is not configured.")

    headers = {}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }

    logger.info(f"Calling Ollama at {OLLAMA_API_URL} with model {OLLAMA_MODEL}...")
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(OLLAMA_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        # Ollama API structure for chat has messages:
        # {"message": {"role": "assistant", "content": "..."}}
        content = data.get("message", {}).get("content", "")
        return content.strip()


def _clean_json_response(text: str) -> str:
    """Remove markdown json code block fences if present in Ollama output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def parse_query_intent(user_query: str, today_str: str) -> dict:
    """
    Parse the user request using Ollama to extract the intent and date range.
    Returns a dict like:
    {
      "type": "internal" | "external",
      "start_date": "YYYY-MM-DD" | None,
      "end_date": "YYYY-MM-DD" | None
    }
    """
    system_prompt = (
        "You are a query classifier and date extraction assistant.\n"
        f"Today's date is {today_str}.\n"
        "Analyze the user's query and classify it as:\n"
        "1. 'internal': if the user is asking to count, list, or check their own food orders/history.\n"
        "2. 'external': if the user is asking a general question, recipes, details about food, or anything else.\n\n"
        "Also, extract the start and end dates for the order search range if 'internal'. "
        "Resolve relative dates (like 'today', 'yesterday', 'last week', 'may-01', etc.) into YYYY-MM-DD format.\n"
        "If a date is not specified, default start_date to None and end_date to None (or the resolved date if mentioned).\n\n"
        "Return ONLY a raw JSON object with keys: 'type', 'start_date', 'end_date'. No explanation, no markdown formatting. "
        "Example response:\n"
        '{"type": "internal", "start_date": "2026-05-01", "end_date": "2026-07-15"}'
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query}
    ]

    try:
        response_text = await _call_ollama(messages)
        cleaned = _clean_json_response(response_text)
        logger.info(f"Ollama intent extraction raw response: {response_text}")
        result = json.loads(cleaned)
        return {
            "type": result.get("type", "external"),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date")
        }
    except Exception as e:
        logger.error(f"Failed to parse query intent using Ollama: {e}", exc_info=True)
        # Safe fallback: check keywords
        query_lower = user_query.lower()
        if "order" in query_lower or "count" in query_lower or "list" in query_lower or "history" in query_lower:
            # Fall back to start of month to today
            return {
                "type": "internal",
                "start_date": None,
                "end_date": None
            }
        return {
            "type": "external",
            "start_date": None,
            "end_date": None
        }


async def generate_chat_response(
    user_query: str,
    query_type: str,
    order_data: list = None,
    user_info: dict = None
) -> str:
    """Generate the final friendly response to the user."""
    if query_type == "internal":
        username = (user_info or {}).get("username", "user")
        full_name = (user_info or {}).get("full_name", "user")
        
        system_prompt = (
            "You are a friendly Food Bot assistant.\n"
            f"The user requesting information is: {full_name} (@{username}).\n"
            "We have fetched their personal food order records from Google Sheets.\n"
            f"Fetched Order Data:\n{json.dumps(order_data or [], ensure_ascii=False, indent=2)}\n\n"
            "Analyze the data and answer the user's question accurately. "
            "Count and list their orders, or summarize as requested. If there are no orders, kindly tell them.\n\n"
            "Formatting Rules:\n"
            "1. Use Telegram Markdown (v1) format.\n"
            "2. Use *bold* (single asterisk) for bold text, NOT **bold**.\n"
            "3. Use _italic_ (single underscore) for italics.\n"
            "4. Do NOT use markdown tables (Telegram does not support them). Instead, format lists or counts using bullet points (e.g. - Item: qty) or text spacing.\n"
            "5. Do not make up any orders not listed in the data. Keep the reply friendly and concise."
        )
    else:
        system_prompt = (
            "You are a friendly Food Bot assistant.\n"
            "Answer the user's query directly and helpfully.\n\n"
            "Formatting Rules:\n"
            "1. Use Telegram Markdown (v1) format.\n"
            "2. Use *bold* (single asterisk) for bold text, NOT **bold**.\n"
            "3. Use _italic_ (single underscore) for italics.\n"
            "4. Do NOT use markdown tables. Format text using standard paragraphs and lists.\n"
            "5. Keep it concise, friendly, and helpful."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query}
    ]

    response = await _call_ollama(messages)
    # Ensure double asterisks from LLM are safe for Telegram Markdown v1
    return response.replace("**", "*")
