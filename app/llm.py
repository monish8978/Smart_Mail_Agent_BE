"""
Backward-compatible re-export barrel for the LLM subsystem.
All existing `from app.llm import X` statements continue to work seamlessly.
"""

from app.llm_utils import *
from app.llm_pricing import *
from app.llm_config import *
from app.llm_prompts import *
from app.llm_functions import *

# Explicitly re-export key runtime symbols
from app.llm_config import client, current_client_id, telemetry_create, resolve_model, resolve_langchain_model
from app.llm_utils import strip_reasoning_and_think_tags, extract_name_from_email, extract_ticket_and_order_ids
from app.llm_pricing import calculate_llm_cost, log_llm_metrics_db
from app.llm_prompts import AgentType, AGENT_PROMPTS, TONE_INSTRUCTIONS, get_agent_prompt
from app.llm_functions import (
    detect_intent_llm,
    generate_reply_llm,
    generate_issue_resolved_reply,
    generate_off_topic_reply,
    design_payload,
    scan_history_for_ticket,
    extract_issue_description,
    generate_summary_llm,
)