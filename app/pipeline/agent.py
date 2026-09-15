import json
import logging
from typing import Dict, Any, List, Optional
import re
from app.pipeline.context import PipelineContext
from app.pipeline.tools import SUPPORT_TOOLS, execute_tool_call
from app.pipeline.drafter import append_client_disclaimers
from app.pipeline.evaluator import evaluate_draft_and_decide
from app.llm import extract_name_from_email, get_llm_config_for_client, client

logger = logging.getLogger(__name__)

RESOLUTION_PATTERNS = [
    r"\b((it|that|this)\s+(worked|works|is\s+working)|issue\s+(is\s+)?(fixed|resolved)|solved|problem\s+(is\s+)?(fixed|resolved)|all\s+good\s+now|working\s+(fine|now|again|properly)|that\s+helped|resolved\s+now)\b",
    r"\b(thank\s+you\s+(so\s+much|very\s+much)?\s*[,.]?\s*(it\s+is\s+working|it\s+works|all\s+set|that\s+did\s+it))\b",
]

def check_customer_resolution(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(re.search(pat, lower) for pat in RESOLUTION_PATTERNS)


class ParsedFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class ParsedToolCall:
    def __init__(self, tool_id: str, name: str, arguments: str):
        self.id = tool_id
        self.type = "function"
        self.function = ParsedFunction(name, arguments)


def extract_text_tool_calls(content: str) -> List[Any]:
    """Fallback parser for models that emit tool calls as text markup instead of message.tool_calls."""
    if not content or not isinstance(content, str):
        return []

    calls = []
    raw_blocks = re.findall(r"<tool_call>(.*?)</tool_call>", content, flags=re.DOTALL | re.IGNORECASE)
    for idx, block in enumerate(raw_blocks):
        block = block.strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
                args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("args") or {}
                if name:
                    arg_str = json.dumps(args) if isinstance(args, dict) else str(args)
                    calls.append(ParsedToolCall(f"call_text_{idx}", name, arg_str))
                    continue
        except Exception:
            pass

        fn_match = re.search(r"<function=([a-zA-Z0-9_-]+)>(.*?)(?:</function>|$)", block, flags=re.DOTALL | re.IGNORECASE)
        if fn_match:
            fn_name = fn_match.group(1).strip()
            fn_body = fn_match.group(2)
            param_matches = re.findall(r"<parameter=([a-zA-Z0-9_-]+)>(.*?)</parameter>", fn_body, flags=re.DOTALL | re.IGNORECASE)
            params = {p_name.strip(): p_val.strip() for p_name, p_val in param_matches}
            calls.append(ParsedToolCall(f"call_text_{idx}", fn_name, json.dumps(params)))

    return calls


def build_system_prompt(ctx: PipelineContext) -> str:
    """Constructs a grounded, instruction-guided system prompt for the customer support agent."""
    customer_name = extract_name_from_email(ctx.from_email)
    dept_name = "Customer Support Team"
    company_name = ""

    try:
        from app.email_credential import get_email_account
        acc = get_email_account(ctx.client_id)
        if acc:
            dept_name = acc.get("department_name") or dept_name
            company_name = acc.get("company_name") or company_name
    except Exception:
        pass

    signoff_lines = ["Thanks & Regards,"]
    if dept_name:
        signoff_lines.append(f"Department: {dept_name}")
    if company_name:
        signoff_lines.append(f"Company: {company_name}")
    signoff_text = "\n".join(signoff_lines)

    step_info = f"Current troubleshooting attempt: Step {ctx.troubleshooting_step + 1} of 3." if ctx.troubleshooting_step > 0 else ""

    resolution_instruction = ""
    if ctx.is_resolved:
        resolution_instruction = """
NOTE: The customer has indicated their problem is now RESOLVED.
- Acknowledge this warmly, confirm the issue is resolved, and thank them.
- DO NOT call 'escalate_and_create_ticket'.
"""

    return f"""You are a professional, accurate customer support agent for {company_name or 'our support team'}.
Customer Name: {customer_name}
{step_info}
{resolution_instruction}

You have direct access to tools to:
1. 'lookup_order_status': Look up status of an order ID/number (e.g. #1001, ORD-9201) in the e-commerce/store system.
2. 'lookup_payment_status': Look up payment transaction status, refund status, or invoice payment details (e.g. Stripe, Razorpay, Zoho Books).
3. 'lookup_ticket_status': Look up status of an existing support ticket reference (e.g. T-260526-00431, 10294) in the CRM.
4. 'lookup_ticket_or_order_status': Combined fallback lookup tool for ticket or order references.
5. 'search_knowledge_base': Search company policies, FAQs, manuals, and troubleshooting guides.
6. 'escalate_and_create_ticket': Create a formal support ticket in the CRM if the issue cannot be solved with existing knowledge, if 3 troubleshooting attempts have failed, or if the user requests human agent assistance.

GUIDELINES & HARD CONSTRAINTS:
- If the customer asks about or provides an order number/ID (e.g., #1001, ORD10294, 'where is my order'), you MUST call 'lookup_order_status'.
- If the customer asks about payment, billing, charge, or transaction status (e.g., transaction ID, invoice ID, payment reference), you MUST call 'lookup_payment_status'.
- If the customer asks about or provides an existing support ticket reference (e.g., T-260505-00117, ticket #4921), you MUST call 'lookup_ticket_status'.
- If the customer reports a technical issue or problem:
  1. Search the knowledge base using 'search_knowledge_base' for diagnostic guides and solutions.
  2. If steps exist, guide the customer through ONE clear troubleshooting action and ask them to test it and reply back with what happens.
  3. If previous steps failed (see conversation history), offer the next diagnostic step.
  4. Only call 'escalate_and_create_ticket' if:
     - All troubleshooting steps in the knowledge base have been exhausted, OR
     - 3 troubleshooting turns have already occurred and the issue remains unresolved, OR
     - The issue is a confirmed hardware/server failure or billing bug that self-troubleshooting cannot fix, OR
     - The customer explicitly asks for human support / ticket creation.
- If the customer states the issue is resolved or that a step worked, confirm resolution politely and DO NOT create a ticket.
- NEVER invent facts, order statuses, turnaround times, or tracking links that were not returned by tools.
- Address the customer politely: "Dear {customer_name},".
- End your response with the standard sign-off:
{signoff_text}
"""


def run_support_agent(ctx: PipelineContext, cursor: Optional[Any] = None) -> PipelineContext:
    """
    Autonomous ReAct / Tool-Calling Agent Loop.
    The LLM reasons over the customer query, selects tools to call, inspects tool outputs,
    and drafts a grounded, factual response.
    """
    logger.info(f"🤖 [Agent Loop] Starting support agent execution for client={ctx.client_id}, sender={ctx.from_email}")
    ctx.log_step("Agent_Loop_Start")

    # Detect if customer confirmed resolution from prior troubleshooting
    if check_customer_resolution(ctx.body) and (ctx.history or ctx.troubleshooting_step > 0):
        ctx.is_resolved = True
        ctx.log_step("Customer_Confirmed_Resolved")
        logger.info(f"✅ Customer confirmed resolution for client={ctx.client_id}, sender={ctx.from_email}")

    # 1. Resolve LLM client and configuration
    cfg = get_llm_config_for_client(ctx.client_id, "run_support_agent")
    raw_model = cfg.get("model_name") or "llama-3.3-70b-versatile"
    provider = (cfg.get("provider") or "groq").lower()

    # Normalize model for tool calling
    model_name = raw_model.strip()
    if provider == "groq" and (model_name in ("compound-mini", "compound", "groq/compound-mini", "") or "llama" in model_name):
        model_name = "qwen/qwen3.6-27b"

    # 2. Build conversation messages
    system_prompt = build_system_prompt(ctx)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]

    # Incorporate history if available
    for h in ctx.history[-6:]:
        role = "user" if h.get("role") == "customer" else "assistant"
        body = h.get("body", "")
        if body:
            messages.append({"role": role, "content": body})

    # Latest incoming message
    user_query = f"Subject: {ctx.subject}\n\n{ctx.body}"
    messages.append({"role": "user", "content": user_query})

    # 3. Round 1: Let the model decide whether to call tools
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=SUPPORT_TOOLS,
            tool_choice="auto",
            temperature=0.1,
            caller="run_support_agent"
        )
    except Exception as e:
        err_str = str(e)
        if ("tool calling" in err_str.lower() or "404" in err_str) and provider == "groq" and model_name != "qwen/qwen3.6-27b":
            logger.warning(f"⚠️ Model {model_name} failed tool calling on Groq, retrying with qwen/qwen3.6-27b")
            model_name = "qwen/qwen3.6-27b"
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=SUPPORT_TOOLS,
                    tool_choice="auto",
                    temperature=0.1,
                    caller="run_support_agent"
                )
            except Exception as retry_err:
                logger.error(f"❌ [Agent Loop] Tool calling retry failed: {retry_err}")
                ctx.log_step(f"Agent_Completion_Error:{str(retry_err)[:50]}")
                ctx.draft_reply = append_client_disclaimers(
                    ctx.client_id,
                    f"Dear Customer,\n\nThank you for contacting us. We have received your inquiry regarding '{ctx.subject}'. Our support team is currently reviewing your message and will get back to you shortly.\n\nThanks & Regards,\nSupport Team"
                )
                ctx.score = 50
                ctx.response_action = "draft_mode"
                return ctx
        else:
            logger.error(f"❌ [Agent Loop] Primary completion failed: {e}. Falling back to default customer acknowledgement.")
            ctx.log_step(f"Agent_Completion_Error:{str(e)[:50]}")
            ctx.draft_reply = append_client_disclaimers(
                ctx.client_id,
                f"Dear Customer,\n\nThank you for contacting us. We have received your inquiry regarding '{ctx.subject}'. Our support team is currently reviewing your message and will get back to you shortly.\n\nThanks & Regards,\nSupport Team"
            )
            ctx.score = 50
            ctx.response_action = "draft_mode"
            return ctx

    msg = response.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None)
    is_text_tool_calls = False
    if not tool_calls and msg.content and ("<tool_call>" in msg.content or "<function=" in msg.content):
        extracted = extract_text_tool_calls(msg.content)
        if extracted:
            tool_calls = extracted
            is_text_tool_calls = True

    # 4. Handle Tool Calls
    from app.llm import strip_reasoning_and_think_tags
    if tool_calls:
        logger.info(f"⚡ [Agent Loop] Model emitted {len(tool_calls)} tool call(s)")
        # Append assistant message with tool calls
        if is_text_tool_calls:
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments if isinstance(tc.function.arguments, str) else json.dumps(tc.function.arguments)
                        }
                    }
                    for tc in tool_calls
                ]
            })
        else:
            messages.append(msg)

        ticket_escalated = False
        for tc in tool_calls:
            tool_name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                parsed_args = {}

            tool_result = execute_tool_call(tool_name, parsed_args, ctx, cursor)

            if tool_name == "escalate_and_create_ticket" and tool_result.get("status") == "ticket_created":
                ticket_escalated = True

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(tool_result)
            })

        # If ticket was already created and reply dispatched by the tool, return early
        if ticket_escalated and ctx.status in ("ticket_created_and_sent", "ticket_created_draft_pending"):
            logger.info("✅ [Agent Loop] Ticket escalation completed and reply formulated by outbox.")
            return ctx

        # Round 2: Model synthesizes final answer with tool outputs
        messages.append({
            "role": "user",
            "content": "Using the knowledge and tool results above, provide your final helpful troubleshooting response to the customer. Do not call any further tools or output tool tags."
        })
        try:
            second_response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.2,
                caller="run_support_agent"
            )
            raw_content = second_response.choices[0].message.content or ""
            raw_content = re.sub(r"<tool_call>.*?</tool_call>", "", raw_content, flags=re.DOTALL | re.IGNORECASE).strip()
            final_draft = strip_reasoning_and_think_tags(raw_content)
        except Exception as e2:
            logger.error(f"❌ [Agent Loop] Secondary synthesis failed: {e2}")
            final_draft = "Thank you for contacting us. We have received your inquiry and our support team is reviewing your request."

    else:
        # Direct response without tool calls (greeting, casual pleasantry, or direct clarification)
        logger.info("ℹ️ [Agent Loop] Model answered directly without invoking external tools")
        final_draft = strip_reasoning_and_think_tags(msg.content or "")

    # Increment troubleshooting step if diagnostic tool called and issue not resolved / escalated
    tool_names_called = [tc.function.name for tc in (tool_calls or [])]
    if "search_knowledge_base" in tool_names_called and not ticket_escalated and not ctx.is_resolved:
        ctx.troubleshooting_step += 1
        ctx.log_step(f"Troubleshooting_Step_{ctx.troubleshooting_step}")

    # 5. Post-Processing: Disclaimers and Evaluation
    final_draft = append_client_disclaimers(ctx.client_id, final_draft)
    score, decision = evaluate_draft_and_decide(
        client_id=ctx.client_id,
        reply=final_draft,
        query=user_query,
        context_succeeded=bool(ctx.context_text or ctx.context_data or not tool_calls or ctx.is_resolved),
        is_resolved=ctx.is_resolved,
        troubleshooting_step=ctx.troubleshooting_step
    )

    ctx.draft_reply = final_draft
    ctx.score = score
    ctx.response_action = decision
    ctx.log_step(f"Agent_Score:{score}:{decision}")

    logger.info(f"🏁 [Agent Loop] Complete. Score: {score}, Action: {decision}")
    return ctx
