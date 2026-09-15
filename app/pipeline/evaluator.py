import re
import logging
from typing import Tuple, Dict, Any
from app.scoring import llm_score
from app.decision import decision_engine
from worker.credential_service import get_email_score_threshold

logger = logging.getLogger(__name__)

AI_DISCLAIMER_PATTERNS = [
    r"as an ai\b",
    r"i am an ai\b",
    r"as a language model\b",
    r"i don't have access to\b",
    r"i do not have access to real-time\b",
]


def check_ai_self_reference(text: str) -> bool:
    """Returns True if the generated draft contains forbidden AI self-referential language."""
    if not text:
        return False
    text_lower = text.lower()
    for pat in AI_DISCLAIMER_PATTERNS:
        if re.search(pat, text_lower):
            return True
    return False


def evaluate_draft_and_decide(
    client_id: str,
    reply: str,
    query: str,
    context_succeeded: bool = True,
    is_resolved: bool = False,
    troubleshooting_step: int = 0
) -> Tuple[int, str]:
    """
    Evaluates reply quality against customer query and determines action.
    Returns (score: int, decision: 'auto_send' | 'create_ticket').
    """
    # 0. If issue is confirmed resolved by customer, confirm closure without escalating
    if is_resolved:
        logger.info(f"✅ Issue resolved for client {client_id} — auto-sending resolution confirmation")
        return 95, "auto_send"

    # 1. Hard floor: If context retrieval failed, immediate ticket escalation
    if not context_succeeded:
        logger.warning(f"⚠️ Context retrieval failed for client {client_id} — triggering hard floor ticket creation")
        return 0, "create_ticket"

    # 2. Check for AI self-referential hallucinations
    if check_ai_self_reference(reply):
        logger.warning(f"⚠️ Draft contains AI self-reference for client {client_id} — forcing ticket escalation")
        return 40, "create_ticket"

    # 3. LLM-based Scoring + Rule penalties
    score = llm_score(reply, query)

    # 4. Fetch Client Threshold
    threshold = get_email_score_threshold(client_id)
    if threshold is None:
        threshold = 80

    # 5. During active troubleshooting (Turns 1, 2, and 3), allow diagnostic steps to reach the customer
    # so they can troubleshoot before a formal ticket is created.
    if troubleshooting_step in (1, 2, 3) and context_succeeded:
        if score >= 50:
            logger.info(f"🔧 [Client {client_id}] Active troubleshooting step {troubleshooting_step} (score: {score}) -> delivering diagnostic to customer")
            return score, "auto_send"

    # 6. Turn 4+: Troubleshooting attempts exhausted without resolution -> force ticket escalation
    if troubleshooting_step >= 4 and not is_resolved:
        logger.info(f"🎫 [Client {client_id}] Troubleshooting step limit reached ({troubleshooting_step}) without resolution -> escalating to ticket")
        return score, "create_ticket"

    decision = decision_engine(score, threshold=threshold)
    logger.info(f"📊 [Client {client_id}] Score: {score}, Threshold: {threshold} -> Decision: {decision}")

    return score, decision
