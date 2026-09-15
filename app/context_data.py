# app/context_data.py

from typing import TypedDict, Optional
import re


class ContextData(TypedDict, total=False):
    """
    Assembled once per incoming email, BEFORE any CRM/order-status API
    call. Shape must not vary by PATH A/B/C — templates must not need to
    know which trigger_type populated which field.

    Cheap fields (CHEAP_KEYS) are always computed via build_context_data_base.
    Expensive fields (EXPENSIVE_KEYS) require an LLM call and are only
    computed by the executor, on demand, via resolve_expensive_keys —
    called only for keys the selected template actually references.
    """
    client_id: str
    from_email: str
    subject: str
    body: str
    cleaned_body: str
    ticket_id: Optional[str]
    order_id: Optional[str]
    payment_id: Optional[str]
    reference_id: Optional[str]
    intent: str
    sentiment: str
    priority: str
    customer_name: str
    history_summary: str
    issue_description: str
    conversation_history: str



CONTEXT_DATA_KEYS = frozenset(ContextData.__annotations__.keys())

CHEAP_KEYS = frozenset({
    "client_id", "from_email", "subject", "body", "cleaned_body",
    "ticket_id", "order_id", "payment_id", "reference_id",
    "intent", "sentiment", "priority", "customer_name",
    "conversation_history",
})

EXPENSIVE_KEYS = frozenset({"history_summary", "issue_description"})

assert CHEAP_KEYS | EXPENSIVE_KEYS == CONTEXT_DATA_KEYS, \
    "CHEAP_KEYS/EXPENSIVE_KEYS drifted out of sync with ContextData — fix before shipping"


def _strip_boilerplate(text: str) -> str:
    """Strips repetitive support signatures and disclaimer separators from message lines."""
    if not text:
        return ""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        l_strip = line.strip().lower()
        if any(l_strip.startswith(p) for p in (
            "thanks & regards", "thanks and regards", "warm regards",
            "best regards", "department:", "company:", "customer support team",
            "-- ", "___", "==="
        )):
            break
        cleaned_lines.append(line)
    res = "\n".join(cleaned_lines).strip()
    return res if res else text.strip()


def format_conversation_thread(history: list, current_body: str, max_messages: int = 4) -> str:
    """
    Builds a clean, structured conversation thread for CRM ticket descriptions.
    - If history is empty, returns the latest customer message directly.
    - Limits history to the last max_messages to avoid huge transcripts in CRM tickets.
    - Strips repetitive sign-offs, email disclaimers, and boilerplate.
    - Formats with clear section headers: [LATEST CUSTOMER INQUIRY] and [RECENT THREAD CONTEXT].
    - Caps overall string length to 3,500 chars to avoid exceeding CRM description limits.
    """
    current_body_clean = (current_body or "").strip()
    if not history:
        return current_body_clean

    recent_history = history[-max_messages:] if len(history) > max_messages else history
    formatted_turns = []

    for entry in recent_history:
        role = entry.get("role", "")
        role_label = "Customer" if role == "customer" else "Support"
        ts = entry.get("timestamp", "")
        ts_label = f" [{ts[:16]}]" if ts else ""
        body_text = (entry.get("body") or "").strip()
        if not body_text:
            continue

        cleaned_msg = _strip_boilerplate(body_text)
        if cleaned_msg:
            formatted_turns.append(f"{role_label}{ts_label}:\n{cleaned_msg}")

    sections = []
    if current_body_clean:
        sections.append(f"=== LATEST CUSTOMER INQUIRY ===\n{_strip_boilerplate(current_body_clean)}")

    if formatted_turns:
        thread_body = "\n\n-----------------------------------\n\n".join(formatted_turns)
        sections.append(f"=== RECENT THREAD CONTEXT (Last {len(formatted_turns)} Messages) ===\n{thread_body}")

    result = "\n\n".join(sections)
    if len(result) > 3500:
        result = result[:3490] + "\n[...truncated]"
    return result



def build_context_data_base(
    client_id: str,
    from_email: str,
    subject: str,
    body: str,
    cleaned_body: str,
    ticket_id: Optional[str] = None,
    intent: str = "general_query",
    sentiment: str = "Neutral",
    priority: str = "Medium",
    history: Optional[list] = None,
    order_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    reference_id: Optional[str] = None,
) -> dict:
    """
    Cheap fields only — no LLM calls. Called once per email in
    worker/tasks.py, before PATH A/B/C branching, replacing today's
    ad-hoc inline variable assembly.
    """
    from app.llm import extract_name_from_email
    from app.utils import normalize_subject
    clean_sub = normalize_subject(subject, cleaned_body or body or "")

    ref_id = reference_id or order_id or payment_id or ticket_id
    ord_id = order_id or ref_id or ticket_id
    pay_id = payment_id or ref_id or ticket_id
    tkt_id = ticket_id or ref_id or ord_id

    return {
        "client_id": client_id or "",
        "from_email": from_email or "",
        "subject": clean_sub,
        "body": body if body is not None else "",
        "cleaned_body": cleaned_body if cleaned_body is not None else (body or ""),
        "ticket_id": tkt_id,
        "order_id": ord_id,
        "payment_id": pay_id,
        "reference_id": ref_id,
        "intent": intent or "general_query",
        "sentiment": sentiment or "Neutral",
        "priority": priority or "Medium",
        "customer_name": extract_name_from_email(from_email) if from_email else "Customer",
        "conversation_history": format_conversation_thread(history or [], body if body is not None else ""),
    }



def resolve_expensive_keys(needed_keys: set, body: str, history: list, old_summary: str = "") -> dict:
    """
    Called ONLY by the executor, after it has determined which
    placeholders the selected template actually references. Never call
    this from worker/tasks.py directly.
    """
    from app.llm import generate_summary_llm, extract_issue_description
    result = {}
    if "history_summary" in needed_keys:
        result["history_summary"] = generate_summary_llm(
            context="", customer_body=body, history=history, old_summary=old_summary
        )
    if "issue_description" in needed_keys:
        result["issue_description"] = extract_issue_description(body, history)
    return result