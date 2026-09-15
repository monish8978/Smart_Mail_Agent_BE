import logging
from typing import Optional, List

from app.llm_utils import _format_history
from app.llm_config import client, current_client_id, resolve_model

logger = logging.getLogger(__name__)


# ==============================
# 🧹 Extract Issue Description
# ==============================
def extract_issue_description(body: str, history: Optional[list] = None) -> str:
    """
    Extract a clean 2–3 sentence problem description from a customer email body.
    Strips greetings, signatures, prior-thread noise, and filler.
    Used before creating a ticket when the customer has described their issue
    after a failed ticket-ID verification.

    Returns a plain string suitable for use as ticket `problem_description`.
    Falls back to a truncated version of body on failure.
    """
    history_block = _format_history(history or [])

    prompt = f"""
You are a support ticket assistant. Extract a clean, concise problem description
from the customer email below.

## Rules
- Return 2–3 sentences maximum.
- Use only information present in the email body.
- Strip greetings, sign-offs, pleasantries, and email-thread boilerplate.
- Strip any prior quoted/forwarded content.
- Do NOT invent or infer details not stated by the customer.
- Return ONLY the plain description text. No labels, no JSON, no markdown.

{history_block}

## Customer Email Body
{body}
"""

    try:
        logger.info("🧹 Extracting issue description from customer body")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "extract_issue_description"),
            caller="extract_issue_description",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise extraction system. "
                        "Return only the plain extracted text, nothing else."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        description = res.choices[0].message.content.strip()
        logger.info(f"✅ Issue description extracted: {description[:120]}...")
        return description

    except Exception as e:
        logger.error(f"❌ Issue description extraction failed: {e}")
        # Graceful fallback: trim raw body to 500 chars
        return body[:500].strip()


# ==============================
# 📝 Generate Issue Summary
# ==============================
def generate_summary_llm(
    context: str,
    customer_body: str,
    history: Optional[list] = None,
    old_summary: str = ""
) -> str:
    """
    Generate or update a concise 250-character summary of the customer issue.

    Sources used (in order of priority):
    1. old_summary — existing summary from MySQL chat_history (if any)
    2. history     — Redis conversation history for this customer+ticket
    3. context     — ticket/API context passed to generate_reply_llm
    4. customer_body — the current incoming email body

    Returns a plain string, max 250 characters.
    Falls back to a truncated customer_body on failure.
    """
    history_block = _format_history(history or [])

    old_summary_block = ""
    if old_summary:
        old_summary_block = f"## Existing Summary (update this, do not repeat it verbatim)\n{old_summary}\n"

    prompt = f"""
You are a support ticket summariser.
Generate a concise summary of the customer's issue in 250 characters or less.

## Rules
- Maximum 250 characters — hard limit, no exceptions.
- Plain text only. No bullet points, no labels, no JSON, no markdown.
- Capture: what the problem is, current status if known, any resolution steps taken.
- If an existing summary is provided, update it with new information — do not repeat it verbatim.
- Do NOT include customer name, ticket ID, or email address.
- Do NOT invent details not present in the sources below.

{old_summary_block}

## Conversation History
{history_block if history_block else "No prior history."}

## Ticket / API Context
{context if context else "No context available."}

## Current Customer Email
{customer_body}

Return ONLY the plain summary text. Nothing else.
"""

    try:
        logger.info("📝 Generating issue summary")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_summary_llm"),
            caller="generate_summary_llm",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise summariser. "
                        "Return only plain text under 250 characters. No labels, no formatting."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        summary = res.choices[0].message.content.strip()

        # Hard enforce 250 char limit
        if len(summary) > 250:
            summary = summary[:247] + "..."

        logger.info(f"✅ Summary generated: {summary}")
        return summary

    except Exception as e:
        logger.error(f"❌ Summary generation failed: {e}")
        return customer_body[:247].strip() + "..." if len(customer_body) > 247 else customer_body.strip()
