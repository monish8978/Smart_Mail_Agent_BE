# app/context_data.py

from typing import TypedDict, Optional


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
    intent: str
    sentiment: str
    priority: str
    customer_name: str
    history_summary: str
    issue_description: str


CONTEXT_DATA_KEYS = frozenset(ContextData.__annotations__.keys())

CHEAP_KEYS = frozenset({
    "client_id", "from_email", "subject", "body", "cleaned_body",
    "ticket_id", "intent", "sentiment", "priority", "customer_name",
})

EXPENSIVE_KEYS = frozenset({"history_summary", "issue_description"})

assert CHEAP_KEYS | EXPENSIVE_KEYS == CONTEXT_DATA_KEYS, \
    "CHEAP_KEYS/EXPENSIVE_KEYS drifted out of sync with ContextData — fix before shipping"


def build_context_data_base(
    client_id: str,
    from_email: str,
    subject: str,
    body: str,
    cleaned_body: str,
    ticket_id: Optional[str],
    intent: str,
    sentiment: str,
    priority: str,
) -> dict:
    """
    Cheap fields only — no LLM calls. Called once per email in
    worker/tasks.py, before PATH A/B/C branching, replacing today's
    ad-hoc inline variable assembly.
    """
    from app.llm import extract_name_from_email
    return {
        "client_id": client_id or "",
        "from_email": from_email or "",
        "subject": subject or "",
        "body": body if body is not None else "",
        "cleaned_body": cleaned_body if cleaned_body is not None else (body or ""),
        "ticket_id": ticket_id,
        "intent": intent or "general_query",
        "sentiment": sentiment or "Neutral",
        "priority": priority or "Medium",
        "customer_name": extract_name_from_email(from_email) if from_email else "Customer",
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