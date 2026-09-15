import re
import logging
from typing import Tuple, List, Optional
from app.pipeline.context import PipelineContext
from app.llm import detect_intent_llm, extract_ticket_and_order_ids, scan_history_for_ticket

logger = logging.getLogger(__name__)


def route_intent_and_entities(ctx: PipelineContext, email_query: str) -> bool:
    """
    Extracts ticket/order entities and detects customer intent.
    Returns True if an immediate terminal branch was taken (e.g. clarification needed),
    False if normal pipeline routing should continue.
    """
    # 1. Detect Intent via LLM
    classification = detect_intent_llm(email_query)
    ctx.intent = classification.get("intent", "general_query")
    ctx.sentiment = classification.get("sentiment", "Neutral")
    ctx.priority = classification.get("priority", "Medium")
    ctx.log_step(f"Intent_Detection:{ctx.intent}")

    ticket_ids = classification.get("ticket_ids", []) or []
    used_fallback = classification.get("used_fallback", False)

    # 2. Fallback Keyword Escalation (only when LLM failed)
    if used_fallback:
        q_lower = email_query.lower()
        if any(w in q_lower for w in ["sue", "legal", "lawyer", "court", "scam"]):
            ctx.priority = "Critical"
            ctx.sentiment = "Angry"
        elif any(w in q_lower for w in ["refund", "cancel", "urgent", "wrong", "fake", "bad", "worst"]):
            if ctx.priority not in ["Critical", "High"]:
                ctx.priority = "High"
            if ctx.sentiment == "Neutral":
                ctx.sentiment = "Angry"

    # 3. Regex Extraction if LLM missed ticket IDs
    if not ticket_ids:
        ticket_ids = extract_ticket_and_order_ids(email_query)

    # Deduplicate IDs
    ticket_ids = list(dict.fromkeys(ticket_ids)) if ticket_ids else []
    ctx.extracted_tickets = ticket_ids

    if ticket_ids:
        ctx.ticket_id = ticket_ids[0]

    return False
