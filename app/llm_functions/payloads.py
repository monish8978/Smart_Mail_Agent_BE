import re
import json
import logging
from typing import Optional, Dict, Any

from app.llm_config import client, current_client_id, resolve_model

logger = logging.getLogger(__name__)


# ==============================
# 📦 Dynamic Payload Design
# ==============================
def design_payload(
    paylod1: dict,
    mail_id: str,
    subject: str,
    body: str,
    status: str,
    personal_details: Optional[dict] = None
) -> dict:

    personal_details = personal_details or {}

    prompt = f"""
You are a payload mapping system.

You will be given a TEMPLATE payload and dynamic input data.
Your job is to map the dynamic input data into the same structure as the TEMPLATE payload.

TEMPLATE PAYLOAD:
{json.dumps(paylod1, indent=2)}

DYNAMIC INPUT DATA:
- mail_id: {mail_id}
- subject: {subject}
- body: {body}
- status: {status}

BODY CLEANING RULES:
- Remove email signatures (lines starting with "--")
- Remove disclaimer sections ("DISCLAIMER:", "This email and its attachments")
- Remove forwarded email headers ("From:", "Sent:", "To:", "Cc:")
- Use only the core message content for "description" or similar fields

PERSONAL DETAILS:
{json.dumps(personal_details, indent=2)}

MAPPING RULES:
- Keep all keys from the TEMPLATE exactly as they are
- Keep all values from the TEMPLATE that are NOT related to the dynamic input
- Replace ONLY the values that logically match the dynamic input:
  * "description" or similar → use body
  * "email" → use mail_id
  * "ticket_status" → use status
  * "subject" or similar → use subject
- Map PERSONAL DETAILS into the template where logical:
  * "person_name", "name", "customer_name" or similar → use personal_details name fields
  * "first_name" → use personal_details first_name if available
  * "last_name" → use personal_details last_name if available
  * "mobile_no", "phone", "contact" or similar → use personal_details phone/mobile if available
  * If a personal detail field has no match in template, ignore it
  * If template has a personal field but personal_details is empty, keep template value as-is
- Do NOT add new keys
- Do NOT remove existing keys
- Do NOT change data types

Return ONLY valid JSON. No explanation. No markdown. No extra text.
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "design_payload"),
            caller="design_payload",
            messages=[
                {
                    "role": "system",
                    "content": "You are a JSON-only response system. Return ONLY valid JSON. No markdown. No explanation."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            reasoning_effort="none"
        )

        output = res.choices[0].message.content.strip()

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON found in LLM response")

        return json.loads(match.group(0))

    except Exception as e:
        logger.error(f"❌ Payload design failed: {e}")
        fallback = paylod1.copy()
        fallback["description"] = body
        fallback["email"] = mail_id
        fallback["ticket_status"] = status
        if personal_details:
            fallback["person_name"] = personal_details.get("name") or personal_details.get("person_name", fallback.get("person_name", ""))
            fallback["mobile_no"] = personal_details.get("mobile") or personal_details.get("mobile_no", fallback.get("mobile_no", ""))
        return fallback
