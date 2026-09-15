import re
import logging

logger = logging.getLogger(__name__)


def strip_reasoning_and_think_tags(text: str) -> str:
    if not isinstance(text, str):
        return text
    # 1. Strip closed tags first
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'<thought>[\s\S]*?</thought>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'```thinking[\s\S]*?```', '', cleaned, flags=re.IGNORECASE)

    # 2. If closed tag didn't match and unclosed tag exists (e.g. truncated mid-thought)
    if '<think>' in cleaned.lower():
        cleaned = re.sub(r'<think>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '<thought>' in cleaned.lower():
        cleaned = re.sub(r'<thought>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '<reasoning>' in cleaned.lower():
        cleaned = re.sub(r'<reasoning>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    if '```thinking' in cleaned.lower():
        cleaned = re.sub(r'```thinking[\s\S]*$', '', cleaned, flags=re.IGNORECASE)

    cleaned = cleaned.strip()

    # 3. If cleaning removed everything because the output was 100% truncated thinking process,
    # recover the most recent greeting/message drafted in the thought process
    if not cleaned and text:
        match = re.search(r'(Hi\s+[^\n]+,\s*[\s\S]+)', text, flags=re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
            # Clean any trailing thought markers
            cleaned = re.sub(r'</?think>.*$', '', cleaned, flags=re.IGNORECASE).strip()

    return cleaned.strip()


def extract_name_from_email(email: str) -> str:
    try:
        username = email.split('@')[0]
        name_parts = re.split(r'[._\-]', username)
        formatted_name = ' '.join(
            [part.capitalize() for part in name_parts if part]
        )
        return formatted_name if formatted_name else "Customer"
    except Exception:
        return "Customer"


def _format_history(history: list) -> str:
    if not history:
        return ""

    lines = ["--- Previous Conversation ---"]
    for entry in history:
        role_label = "Customer" if entry.get("role") == "customer" else "Support"
        ts      = entry.get("timestamp", "")[:16]
        subject = entry.get("subject", "")
        body    = entry.get("body", "")[:300]
        ticket  = entry.get("ticket_id", "")

        line = f"[{ts}] {role_label}"
        if ticket:
            line += f" (Ticket: {ticket})"
        line += f"\nSubject: {subject}\n{body}"
        lines.append(line)

    lines.append("--- End of History ---")
    return "\n\n".join(lines)


def extract_ticket_and_order_ids(text: str) -> list[str]:
    """
    Extracts all ticket IDs, order numbers, case numbers, and reference numbers
    from text using comprehensive regex patterns. Returns cleaned, deduplicated IDs.
    Also handles email line-wrapping/folding across newlines.
    """
    if not text:
        return []

    # Normalize soft line breaks ONLY within ticket tokens split across lines (e.g., #27542400000039\r\n9001 -> #275424000000399001)
    texts_to_check = [text]
    unwrapped_text = re.sub(r'(#\d+|\bT-\d+|\bORD-\d+|\bINC\d+|\d+)[\r\n]+(\d+)', r'\1\2', text)
    if unwrapped_text != text:
        texts_to_check.append(unwrapped_text)

    ids = []
    for t in texts_to_check:
        # 1. Standard pattern formats like T-YYMMDD-XXXXX
        for m in re.finditer(r'\b(T-\d{6}-\d+)\b', t, re.IGNORECASE):
            ids.append(m.group(1).upper())
        # 2. Common CRM / Ticketing prefixes (ORD, INC, CAS, SR, REQ)
        for m in re.finditer(r'\b(ORD-?\d+|INC\d+|CAS-\d+(?:-[A-Za-z0-9]+)?|SR-\d+|REQ\d+)\b', t, re.IGNORECASE):
            ids.append(m.group(1).upper())
        # 3. Explicit keywords: ticket/case/order/complaint/issue/ref followed by an ID
        for m in re.finditer(r'(?:ticket|case|order|complaint|issue|incident|ref(?:erence)?)\s*(?:id|no|num|number)?\s*[:#\s-]?\s*#?([A-Za-z0-9_-]{1,30})', t, re.IGNORECASE):
            val = m.group(1).strip()
            if not re.search(r'\d', val):
                continue
            if val.lower() not in (
                "status", "update", "details", "information", "number", "issue", "query",
                "support", "please", "regarding", "about", "there", "here", "with", "from",
                "that", "this", "resolved", "fixed", "been", "have"
            ):
                ids.append(val)
        # 4. Hash followed by digits/alphanumeric (e.g. #275424000000399001, #98765, #121)
        for m in re.finditer(r'#([A-Za-z0-9_-]{1,30})', t):
            val = m.group(1).strip()
            if val and re.search(r'\d', val):
                ids.append(val)

    clean_ids = []
    for item in ids:
        cleaned = item.strip().lstrip("#").strip()
        if cleaned and cleaned not in clean_ids:
            if any(c != cleaned and cleaned in c for c in ids):
                continue
            clean_ids.append(cleaned)
    return clean_ids
