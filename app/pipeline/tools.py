import json
import logging
import re
from typing import Dict, Any, List, Optional
from app.pipeline.context import PipelineContext
from app.pipeline.enricher import (
    fetch_crm_ticket_status,
    fetch_crm_order_status,
    fetch_payment_status,
    fetch_rag_context,
)
from app.pipeline.dispatcher import create_ticket_and_reply
from app.connector_executor import format_mapped_data_for_prompt

logger = logging.getLogger(__name__)


def clean_rag_query(raw_text: str) -> str:
    """
    Sanitizes raw email bodies or queries for RAG dense embedding search.
    Strips email headers, greetings, signatures, quoted history, and legal footers.
    """
    if not raw_text:
        return ""
    text = str(raw_text).strip()

    # Remove email quoted history: "On ... wrote:" or lines starting with ">"
    text = re.split(r"(?i)\n\s*on\s+.*?\s+wrote\s*:\s*\n", text)[0]
    text = re.split(r"(?i)\n\s*---+\s*original message\s*---+\s*", text)[0]
    lines = [line for line in text.splitlines() if not line.strip().startswith(">")]
    text = "\n".join(lines)

    # Remove common greeting lines at the start
    greeting_pattern = r"(?i)^(hi|hello|dear|hey|good\s+(morning|afternoon|evening))\s*[^,\n]*[,!:]*\s*\n*"
    text = re.sub(greeting_pattern, "", text.strip())

    # Remove signatures at the bottom
    sig_patterns = [
        r"(?i)\n\s*(thanks|thank you|regards|best regards|warm regards|sincerely|cheers|yours truly)[\s,].*",
        r"(?i)\n\s*sent from my (iphone|android|galaxy|ipad|device).*",
        r"(?i)\n\s*disclaimer:?.*",
        r"(?i)\n\s*this email and any files transmitted.*"
    ]
    for sp in sig_patterns:
        text = re.sub(sp, "", text, flags=re.DOTALL)

    cleaned = " ".join(text.split()).strip()
    # Keep query focused: if it's over 400 characters, extract first 2-3 substantive sentences
    if len(cleaned) > 400:
        sentences = re.split(r"(?<=[.?!])\s+", cleaned)
        shortened = ""
        for s in sentences:
            if len(shortened) + len(s) < 350:
                shortened = f"{shortened} {s}".strip()
            else:
                break
        if shortened:
            cleaned = shortened

    return cleaned or raw_text[:300].strip()

# ==========================================
# 🛠️ DECLARATIVE OPENAI FUNCTION SCHEMAS
# ==========================================

SUPPORT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order_status",
            "description": "Look up real-time order status, fulfillment tracking, line items, and delivery details from the client store or ERP system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID or order number mentioned by the customer (e.g., '#1001', 'ORD10294', '10294')."
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_payment_status",
            "description": "Look up payment transaction status, refund status, or invoice payment details from payment gateways (e.g. Stripe, Razorpay, Zoho Books).",
            "parameters": {
                "type": "object",
                "properties": {
                    "payment_id_or_order_id": {
                        "type": "string",
                        "description": "The transaction ID, payment reference, invoice ID, or order reference (e.g., 'pi_3MtwBwLkdCwBgZr20e', 'pay_29ashd81', 'INV-1092', 'ORD10294')."
                    }
                },
                "required": ["payment_id_or_order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_ticket_status",
            "description": "Look up the real-time status, agent assignment, and latest comments of an existing support ticket from the client CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The exact ticket reference or support docket number mentioned by the customer (e.g., 'T-260526-00431', '275424000000446001')."
                    }
                },
                "required": ["ticket_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_ticket_or_order_status",
            "description": "Legacy combined tool: Look up status of an existing ticket or order docket number from CRM or store.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The exact ticket reference or order ID mentioned by the customer (e.g., 'T-260526-00431', 'ORD10294', '#120', '275424000000446001')."
                    }
                },
                "required": ["ticket_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search company knowledge base, policies, FAQs, documentation, product manuals, and troubleshooting guides for answers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string to look up in the vector knowledge base."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_and_create_ticket",
            "description": "Escalate an unresolved issue, technical bug, complaint, refund request, or human support request to generate a new formal support ticket in the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_summary": {
                        "type": "string",
                        "description": "A concise, factual description of the issue being escalated."
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["Low", "Medium", "High", "Critical"],
                        "description": "The urgency level of the request."
                    },
                    "remarks": {
                        "type": "string",
                        "description": "Optional notes or technical context for the support agent."
                    }
                },
                "required": ["issue_summary"]
            }
        }
    }
]


# ==========================================
# ⚙️ TOOL DISPATCHER & EXECUTOR
# ==========================================

def execute_tool_call(
    tool_name: str,
    arguments: Dict[str, Any],
    ctx: PipelineContext,
    cursor: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Executes the specified tool with arguments against underlying CRM/RAG backends.
    Returns a dictionary result to feed back into the agent conversation sequence.
    Catches errors and provides structured diagnostics so the agent can adapt gracefully.
    """
    logger.info(f"⚡ [Agent Tool Call] Executing '{tool_name}' with args: {arguments}")
    ctx.log_step(f"Tool_Call:{tool_name}")

    try:
        if tool_name == "lookup_order_status":
            raw_order_id = arguments.get("order_id", "")
            clean_order_id = str(raw_order_id).strip().lstrip("#")

            res = fetch_crm_order_status(
                client_id=ctx.client_id,
                order_id=clean_order_id,
                body=ctx.body,
                history=ctx.history,
                subject=ctx.subject,
                from_email=ctx.from_email
            )
            if res.get("success"):
                order_data = res.get("data") or {}
                formatted_text = format_mapped_data_for_prompt(order_data)
                ctx.context_data = order_data
                ctx.log_step("CRM_Order_Found")
                return {
                    "status": "found",
                    "order_id": clean_order_id,
                    "details": order_data,
                    "summary": formatted_text
                }
            else:
                err_msg = res.get("error")
                ctx.log_step("CRM_Order_Not_Found")
                return {
                    "status": "lookup_failed" if err_msg else "not_found",
                    "order_id": clean_order_id,
                    "message": err_msg or f"No active order record found for reference '{clean_order_id}'",
                    "hint": "If record is not found, ask customer to verify the order number. If an error occurred, apologize for the technical delay."
                }

        elif tool_name == "lookup_payment_status":
            raw_ref = arguments.get("payment_id_or_order_id", "")
            clean_ref = str(raw_ref).strip()

            res = fetch_payment_status(
                client_id=ctx.client_id,
                payment_id_or_order_id=clean_ref,
                body=ctx.body,
                history=ctx.history,
                subject=ctx.subject,
                from_email=ctx.from_email
            )
            if res.get("success"):
                payment_data = res.get("data") or {}
                formatted_text = format_mapped_data_for_prompt(payment_data)
                ctx.context_data = payment_data
                ctx.log_step("Payment_Status_Found")
                return {
                    "status": "found",
                    "payment_reference": clean_ref,
                    "details": payment_data,
                    "summary": formatted_text
                }
            else:
                err_msg = res.get("error")
                ctx.log_step("Payment_Status_Not_Found")
                return {
                    "status": "lookup_failed" if err_msg else "not_found",
                    "payment_reference": clean_ref,
                    "message": err_msg or f"No active payment record found for reference '{clean_ref}'",
                    "hint": "If record is not found, ask customer to confirm the payment reference or transaction ID."
                }

        elif tool_name == "lookup_ticket_status":
            raw_ticket_id = arguments.get("ticket_id", "")
            clean_ticket_id = str(raw_ticket_id).strip().lstrip("#")

            res = fetch_crm_ticket_status(
                client_id=ctx.client_id,
                ticket_id=clean_ticket_id,
                body=ctx.body,
                history=ctx.history,
                subject=ctx.subject,
                from_email=ctx.from_email
            )
            if res.get("success"):
                ticket_data = res.get("data") or {}
                formatted_text = format_mapped_data_for_prompt(ticket_data)
                ctx.ticket_id = clean_ticket_id
                ctx.context_data = ticket_data
                ctx.log_step("CRM_Status_Found")
                return {
                    "status": "found",
                    "ticket_id": clean_ticket_id,
                    "details": ticket_data,
                    "summary": formatted_text
                }
            else:
                err_msg = res.get("error")
                ctx.log_step("CRM_Status_Not_Found")
                return {
                    "status": "lookup_failed" if err_msg else "not_found",
                    "ticket_id": clean_ticket_id,
                    "message": err_msg or f"No active record found in CRM for reference '{clean_ticket_id}'",
                    "hint": "If record is not found, ask customer to verify the reference number or provide more context. If an error occurred, apologize for the technical delay and assure them support is investigating."
                }

        elif tool_name == "lookup_ticket_or_order_status":
            raw_ticket_id = arguments.get("ticket_id", "")
            clean_ticket_id = str(raw_ticket_id).strip().lstrip("#")

            # Try order status first if prefixed with ORD or numeric-short, otherwise ticket status
            res = fetch_crm_ticket_status(
                client_id=ctx.client_id,
                ticket_id=clean_ticket_id,
                body=ctx.body,
                history=ctx.history,
                subject=ctx.subject,
                from_email=ctx.from_email
            )
            if not res.get("success"):
                # Secondary attempt with order status lookup
                res = fetch_crm_order_status(
                    client_id=ctx.client_id,
                    order_id=clean_ticket_id,
                    body=ctx.body,
                    history=ctx.history,
                    subject=ctx.subject,
                    from_email=ctx.from_email
                )

            if res.get("success"):
                ticket_data = res.get("data") or {}
                formatted_text = format_mapped_data_for_prompt(ticket_data)
                ctx.ticket_id = clean_ticket_id
                ctx.context_data = ticket_data
                ctx.log_step("CRM_Status_Found")
                return {
                    "status": "found",
                    "ticket_id": clean_ticket_id,
                    "details": ticket_data,
                    "summary": formatted_text
                }
            else:
                err_msg = res.get("error")
                ctx.log_step("CRM_Status_Not_Found")
                return {
                    "status": "lookup_failed" if err_msg else "not_found",
                    "ticket_id": clean_ticket_id,
                    "message": err_msg or f"No active record found in CRM for reference '{clean_ticket_id}'",
                    "hint": "If record is not found, ask customer to verify the reference number or provide more context. If an error occurred, apologize for the technical delay and assure them support is investigating."
                }

        elif tool_name == "search_knowledge_base":
            raw_query = (arguments.get("query") or "").strip()
            if not raw_query:
                raw_query = ctx.body or ctx.subject
            query = clean_rag_query(raw_query)
            context_text, succeeded, rag_id = fetch_rag_context(ctx.client_id, query)
            ctx.rag_id = rag_id
            ctx.context_text = context_text

            if succeeded and context_text:
                ctx.log_step("Knowledge_Search_Success")
                return {
                    "status": "success",
                    "results_found": True,
                    "content": context_text
                }
            else:
                ctx.log_step("Knowledge_Search_Empty")
                return {
                    "status": "empty",
                    "results_found": False,
                    "content": "No relevant policy or documentation found in knowledge base for this inquiry.",
                    "hint": "If the knowledge base does not cover this inquiry, escalate and create a ticket or inform the customer that their issue has been noted for support review."
                }

        elif tool_name == "escalate_and_create_ticket":
            issue_summary = (arguments.get("issue_summary") or "").strip() or ctx.subject
            priority = arguments.get("priority", ctx.priority or "Medium")
            remarks = (arguments.get("remarks") or "").strip()

            troubleshooting_notes = ""
            if ctx.history:
                troubleshooting_notes = "\n\nTroubleshooting History:\n" + "\n".join(
                    f"- {h.get('role', 'msg').title()}: {h.get('body', '')[:120]}" for h in ctx.history[-4:]
                )
            context_to_send = f"{issue_summary}"
            if remarks:
                context_to_send += f" | Remarks: {remarks}"
            if troubleshooting_notes:
                context_to_send += troubleshooting_notes

            task_data = ctx.to_task_data()
            if ctx.subject.lower() in ("support request", "(no subject)", "no subject") and issue_summary:
                task_data["subject"] = issue_summary[:100]

            reply, ticket_id, status = create_ticket_and_reply(
                data=task_data,
                client_id=ctx.client_id,
                context=context_to_send,
                history=ctx.history,
                cursor=cursor,
                sentiment=ctx.sentiment,
                priority=priority,
                features=ctx.features
            )

            if status == "ticket_creation_failed" or not ticket_id:
                logger.error(f"❌ [Agent Tool Call] Ticket creation failed for client {ctx.client_id}")
                ctx.log_step("Ticket_Creation_Failed")
                return {
                    "status": "ticket_creation_failed",
                    "error": "Failed to create CRM ticket due to external connector failure or timeout.",
                    "hint": "The CRM ticket could not be generated at this time. Formulate a polite apology to the customer and reassure them that our support team will handle their inquiry manually."
                }

            ctx.ticket_id = ticket_id
            ctx.draft_reply = reply
            ctx.status = status
            ctx.log_step(f"Ticket_Created:{ticket_id}")

            return {
                "status": "ticket_created",
                "ticket_id": ticket_id,
                "reply_dispatched": status == "ticket_created_and_sent"
            }

        else:
            logger.warning(f"⚠️ Unrecognized tool name requested: {tool_name}")
            return {
                "status": "unrecognized_tool",
                "error": f"Tool '{tool_name}' is not recognized.",
                "available_tools": [
                    "lookup_order_status",
                    "lookup_payment_status",
                    "lookup_ticket_status",
                    "lookup_ticket_or_order_status",
                    "search_knowledge_base",
                    "escalate_and_create_ticket",
                ]
            }

    except Exception as e:
        logger.error(f"❌ [Agent Tool Call] Unexpected exception executing '{tool_name}': {e}", exc_info=True)
        ctx.log_step(f"Tool_Execution_Error:{tool_name}")
        return {
            "status": "error",
            "tool_name": tool_name,
            "error_type": type(e).__name__,
            "message": str(e),
            "hint": "The external service or connector encountered an unexpected system error. Formulate a polite acknowledgment letting the customer know our support team will review their request directly."
        }
