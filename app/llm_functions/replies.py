import re
import logging

from app.llm_utils import (
    strip_reasoning_and_think_tags,
    extract_name_from_email,
    _format_history,
)
from app.llm_prompts import AgentType, AGENT_PROMPTS, TONE_INSTRUCTIONS
from app.llm_config import client, current_client_id, resolve_model

logger = logging.getLogger(__name__)


# ==============================
# ✉️ Generate Reply
# ==============================
def generate_reply_llm(
    context: str,
    query: str,
    agent_type: AgentType,
    from_email: str = None,
    is_ticket: bool = False,
    ticket_id: str = None,
    history: list = None,
    is_status_inquiry: bool = False,
) -> str:
    """
    Generate professional email reply.
    history: list of prior conversation dicts from chat_history module.
    is_status_inquiry: when True, strictly prohibits hallucinating turnaround ETAs, diagnostic steps, or internal teams.
    """

    response_tone = "Formal"
    agent_type_override = agent_type  # keep caller's value as fallback
    department_name = None
    company_name = None
    client_id = current_client_id.get()
    if client_id and client_id != "SYSTEM":
        try:
            from app.email_credential import get_email_account
            account = get_email_account(client_id)
            if account:
                response_tone   = account.get("response_tone", "Formal")
                agent_type_override = account.get("agent_type", agent_type)
                department_name = account.get("department_name")
                company_name    = account.get("company_name")
        except Exception as e:
            logger.warning(f"Failed to fetch account profile: {e}")

    tone_instruction = TONE_INSTRUCTIONS.get(
        response_tone,
        f"Write your reply in a {response_tone} tone."
    )
    base_persona = AGENT_PROMPTS.get(agent_type_override, AGENT_PROMPTS["customer_support"])
    system_prompt = f"{base_persona}\n\nCRITICAL BRAND VOICE GUIDELINE: {tone_instruction}"

    customer_name = (
        extract_name_from_email(from_email)
        if from_email
        else "Customer"
    )

    logger.info(f"👤 Customer name: {customer_name}")

    history_block = _format_history(history or [])
    if history_block:
        logger.info(f"📜 Injecting {len(history or [])} history messages into prompt")

    # ==========================================
    # 🎫 Ticket Reply
    # ==========================================
    if is_ticket and ticket_id:

        if department_name:
            team_name = department_name
        else:
            agent_team_map = {
                "customer_support":     "Customer Support Team",
                "ecommerce_support":    "E-Commerce Support Team",
                "technical_support":    "Technical Support Team",
                "billing_support":      "Billing & Invoicing Team",
                "executive_escalation": "Executive Support Team",
                "customer_support_agent":  "Customer Support Team",
                "ecommerce_support_agent": "E-Commerce Support Team",
                "crm_support_agent":       "CRM Support Team"
            }
            team_name = agent_team_map.get(agent_type_override, "Support Team")

        prompt = f"""Write a professional email reply to {customer_name} acknowledging that support ticket #{ticket_id} has been logged and is currently in progress.

Customer Query:
{query}

Ticket Information:
- Ticket ID: {ticket_id}
- Customer Name: {customer_name}
{context}

Requirements:
1. Start with greeting: Hi {customer_name},
2. Acknowledge that ticket #{ticket_id} is registered and the team is reviewing their inquiry.
3. Keep it to 2-3 sentences.
4. Conclude with:
Thanks & Regards,
{team_name}

Write ONLY the final email message text. No preamble, no quotes, no explanation, no bulleted instructions.
"""

    # ==========================================
    # 💬 General Reply
    # ==========================================
    else:

        status_guardrails = ""
        if is_status_inquiry:
            status_guardrails = """- CRITICAL STATUS INQUIRY CONSTRAINTS:
  * State the current ticket/order status accurately as provided in Context.
  * NEVER invent, estimate, or promise timelines, ETAs, turnaround hours, or days (e.g. DO NOT say 'within 2 hours', 'within 24 hours', or 'within 48 hours').
  * NEVER invent fictional diagnostic procedures, manufacturing logs, internal QA teams, or technician assignments.
  * Address any specific notes, questions, or updates the customer mentioned, and assure them their notes are logged for the support team.
  * Reassure the customer that the support team is actively reviewing the case."""

        prompt = f"""
Customer Name:
{customer_name}

{history_block}

Context:
{context}

Customer Query:
{query}

Instructions:
- Be professional
- Be concise
- Do NOT hallucinate
- NEVER claim or state that a ticket has been created, and NEVER output placeholder ticket references like "[Insert Ticket ID]" or "[Ticket Number]" or "[Ticket ID]".
{status_guardrails}
- If previous conversation exists above, maintain continuity — do not repeat what was already addressed
- If no answer available, say politely
- End professionally

Write the email reply.

Email Ending
Thanks & Regards,
dont add name section example "[Your Name]"
Department: {department_name or 'derive from agent_type'}
Company: {company_name or 'derive from context/email'}
"""

    try:
        logger.info("📧 Generating AI reply")
        logger.info(f"📋📋📋 Prompt context being sent to LLM: {context[:1000] if context else 'EMPTY'}")
        logger.info(f"📋📋📋 Ending")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_reply_llm"),
            caller="generate_reply_llm",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            reasoning_effort="none",
            max_tokens=1500
        )

        reply = strip_reasoning_and_think_tags(res.choices[0].message.content)
        
        # 1. Clean any trailing chain-of-thought analysis or numbered notes after the sign-off block
        signoff_match = re.search(r'((?:Thanks\s*(?:&|and)\s*Regards|Best\s*regards|Sincerely)[\s\S]*?\n[^\n]+)', reply, flags=re.IGNORECASE)
        if signoff_match:
            reply = reply[:signoff_match.end()].strip()

        # 2. Filter out any echoed prompt/instruction bullet lines
        clean_lines = []
        for line in reply.split("\n"):
            clean_l = line.strip().strip('"').strip("'")
            if re.match(r'^\*?\s*(?:Write\s+\d|Mention\s+team|End\s+with|Return\s+ONLY|Start\s+with|Requirements:)', clean_l, flags=re.IGNORECASE):
                continue
            clean_lines.append(line)
        reply = "\n".join(clean_lines).strip().strip('"').strip("'")

        # 3. Strip any leaked "Subject: ..." or "Re: ..." header line placed at the very start of the email body
        reply = re.sub(r'^(?:Subject|Re):\s*[^\n]+\n+', '', reply, flags=re.IGNORECASE).strip()

        logger.info(f"✅ Reply generated successfully: {reply[:150]}...")
        return reply

    except Exception as e:
        logger.error(f"❌ Reply generation failed: {e}")
        return (
            "Sorry, we are unable to process "
            "your request at the moment."
        )


def generate_issue_resolved_reply(
    from_email: str,
    subject: str = "",
    query: str = "",
    ticket_id: str = None,
    history: list = None
) -> str:
    """
    Generates a polite, warm confirmation acknowledging that the customer's
    issue is resolved, without creating or modifying tickets.
    """
    customer_name = extract_name_from_email(from_email) if from_email else "Customer"
    client_id = current_client_id.get()
    department_name = None
    company_name = None
    response_tone = "Friendly"

    if client_id and client_id != "SYSTEM":
        try:
            from app.email_credential import get_email_account
            account = get_email_account(client_id)
            if account:
                response_tone = account.get("response_tone", "Friendly")
                department_name = account.get("department_name")
                company_name = account.get("company_name")
        except Exception as e:
            logger.warning(f"Failed to fetch account profile for resolved reply: {e}")

    team_name = department_name or "Support Team"
    if company_name:
        team_name = f"{company_name} {team_name}"

    ticket_mention = f" regarding ticket #{ticket_id}" if ticket_id else ""

    prompt = f"""Write a short, professional, and courteous email response to {customer_name}.
The customer sent an email stating that their issue{ticket_mention} has been resolved / is working fine.

Customer Query:
{query}

Requirements:
1. Greet: Hi {customer_name},
2. Express that you are glad to hear everything is sorted out and working properly.
3. Let them know they are welcome to reach back out anytime if they need any further assistance.
4. Keep it concise (2-3 sentences total).
5. Tone: {response_tone}
6. Conclude with:
Thanks & Regards,
{team_name}

Write ONLY the final email text. No explanation, no quotes.
"""
    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_reply_llm"),
            caller="generate_issue_resolved_reply",
            messages=[
                {"role": "system", "content": "You are a courteous customer support assistant. Write concise, warm emails."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=300
        )
        reply = strip_reasoning_and_think_tags(res.choices[0].message.content).strip()
        return reply
    except Exception as e:
        logger.error(f"❌ Failed to generate issue_resolved reply via LLM: {e}")
        return (
            f"Hi {customer_name},\n\n"
            f"Thank you for letting us know! We are glad to hear that your issue{ticket_mention} has been resolved.\n\n"
            f"If you ever need any further assistance, please feel free to reach back out.\n\n"
            f"Thanks & Regards,\n"
            f"{team_name}"
        )


def generate_off_topic_reply(
    from_email: str,
    subject: str = "",
    query: str = "",
    history: list = None
) -> str:
    """
    Generates a polite boundary-setting response for off-topic, gibberish,
    or non-support inquiries without escalating or creating tickets.
    """
    customer_name = extract_name_from_email(from_email) if from_email else "Customer"
    client_id = current_client_id.get()
    department_name = None
    company_name = None

    if client_id and client_id != "SYSTEM":
        try:
            from app.email_credential import get_email_account
            account = get_email_account(client_id)
            if account:
                department_name = account.get("department_name")
                company_name = account.get("company_name")
        except Exception as e:
            logger.warning(f"Failed to fetch account profile for off_topic reply: {e}")

    team_name = department_name or "Customer Support Team"
    if company_name:
        team_name = f"{company_name} {team_name}"

    prompt = f"""Write a polite, professional customer support email to {customer_name}.
The customer sent an email that appears to be incomplete, gibberish, or outside the scope of customer support:

Customer Email:
{query}

Requirements:
1. Greet: Hi {customer_name},
2. Politely mention that we received their email, but we were unable to identify a clear support request or inquiry from the message.
3. Invite them to reply with specific details or order/account information if they require assistance with our products or services.
4. Keep it concise, respectful, and helpful (2-3 sentences max).
5. Conclude with:
Thanks & Regards,
{team_name}

Write ONLY the final email text. No explanation, no quotes.
"""
    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_reply_llm"),
            caller="generate_ticket_status_reply",
            messages=[
                {"role": "system", "content": "You are a professional customer support assistant. Write concise, polite boundary-setting emails."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=300
        )
        reply = strip_reasoning_and_think_tags(res.choices[0].message.content).strip()
        return reply
    except Exception as e:
        logger.error(f"❌ Failed to generate off_topic reply via LLM: {e}")
        return (
            f"Hi {customer_name},\n\n"
            f"Thank you for contacting us. We received your email, but were unable to identify a specific question or support request.\n\n"
            f"If you need assistance with our products or services, please reply with details and we will be glad to help.\n\n"
            f"Thanks & Regards,\n"
            f"{team_name}"
        )
