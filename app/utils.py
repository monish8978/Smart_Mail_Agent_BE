import re


def normalize_subject(raw_subject: str, body: str = "") -> str:
    """
    Normalize email subject: if missing/blank/placeholder, derive from
    first line of body or default to 'Support Request'.
    """
    clean = (raw_subject or "").strip()
    if not clean or clean.lower() in ("(no subject)", "no subject", "none", "null"):
        first_line = body.strip().split("\n")[0].strip() if body and body.strip() else ""
        clean_first = re.sub(r'[\r\n\t]+', ' ', first_line)[:60].strip()
        clean = clean_first if len(clean_first) >= 3 else "Support Request"
    return clean
