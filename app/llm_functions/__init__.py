"""
Package app.llm_functions.
Provides modular sub-packages for intent classification, reply generation,
dynamic payload mapping, and issue summarization.
Maintains 100% backwards compatibility with previous flat module.
"""

from app.llm_functions.intent import (
    detect_intent_llm,
    scan_history_for_ticket,
)
from app.llm_functions.replies import (
    generate_reply_llm,
    generate_issue_resolved_reply,
    generate_off_topic_reply,
)
from app.llm_functions.payloads import (
    design_payload,
)
from app.llm_functions.summaries import (
    extract_issue_description,
    generate_summary_llm,
)

__all__ = [
    # Intent & triage
    "detect_intent_llm",
    "scan_history_for_ticket",
    # Reply synthesis
    "generate_reply_llm",
    "generate_issue_resolved_reply",
    "generate_off_topic_reply",
    # Payloads
    "design_payload",
    # Summaries & extraction
    "extract_issue_description",
    "generate_summary_llm",
]
