"""
app/text_cleaning.py

Strips quoted/forwarded reply-chain content from email bodies before they
are used as LLM or RAG input.
"""

import re
import logging

logger = logging.getLogger(__name__)

_ON_DATE_WROTE_RE = re.compile(
    r"\bOn\s+.{0,60}?\d{1,2}(:\d{2})?\s*(AM|PM|am|pm)?\s*.{0,80}?wrote:\s*$",
    re.MULTILINE,
)

_ORIGINAL_MESSAGE_RE = re.compile(
    r"^-{2,}\s*Original Message\s*-{2,}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_FWD_HEADER_BLOCK_RE = re.compile(
    r"^From:\s.*\n(Sent|Date):\s.*\n(To:\s.*\n)?(Cc:\s.*\n)?Subject:\s.*$",
    re.MULTILINE | re.IGNORECASE,
)

_QUOTE_BLOCK_START_RE = re.compile(r"^\s*>.*$", re.MULTILINE)


def strip_quoted_reply(body: str) -> str:
    if not body or not isinstance(body, str):
        return ""

    cut_index = len(body)
    matched_pattern = None

    for pattern, name in (
        (_ON_DATE_WROTE_RE, "on_date_wrote"),
        (_ORIGINAL_MESSAGE_RE, "original_message"),
        (_FWD_HEADER_BLOCK_RE, "fwd_header_block"),
    ):
        m = pattern.search(body)
        if m and m.start() < cut_index:
            cut_index = m.start()
            matched_pattern = name

    if matched_pattern is None:
        qm = _QUOTE_BLOCK_START_RE.search(body)
        if qm and qm.start() < cut_index:
            cut_index = qm.start()
            matched_pattern = "quote_block"

    cleaned = body[:cut_index].strip()

    if matched_pattern:
        logger.info(
            f"✂️ Stripped quoted reply chain (matched={matched_pattern}), "
            f"{len(body)} -> {len(cleaned)} chars"
        )
    else:
        logger.debug("✂️ No quote marker found — body unchanged")

    if not cleaned:
        logger.warning(
            "⚠️ Quote-stripping would have emptied the body — "
            "falling back to original unstripped text"
        )
        return body.strip()

    return cleaned

_BUILTIN_DISCLAIMER_PATTERNS = [
    re.compile(
        r"(?:(?:\r?\n|^)(?:--\s*\r?\n)?(?:\*?\s*(?:DISCLAIMER|Confidentiality Notice|IMPORTANT NOTICE|PRIVACY NOTICE):?\*?|"
        r"This email and (?:any|its) attachments are confidential|"
        r"This e-mail message, including any attachments, is for the sole use|"
        r"The information contained in this (?:email|message) (?:is|may be) (?:confidential|legally privileged)|"
        r"Towards Vision Technologies Limited is not liable|"
        r"If you (?:have )?received this email in error|"
        r"This message is intended solely for the addressee)[\s\S]*)",
        re.IGNORECASE
    ),
]


def strip_disclaimers(body: str, custom_disclaimers: list[str] | None = None) -> str:
    """
    Strips custom configured client disclaimers and standard legal/confidentiality
    disclaimers from an email message body before passing it to AI/LLM.
    Saves token costs and prevents LLMs from echoing or hallucinating on disclaimers.
    """
    if not body or not isinstance(body, str):
        return ""

    text = body
    earliest_cut = len(text)
    matched_reason = None

    # 1. Check custom configured disclaimers
    if custom_disclaimers:
        text_lower = text.lower()
        for d in custom_disclaimers:
            if not d or not d.strip():
                continue
            d_clean = d.strip().lower()
            idx = text_lower.find(d_clean)
            if idx != -1 and idx < earliest_cut:
                earliest_cut = idx
                matched_reason = f"custom_disclaimer: '{d[:30]}...'"

    # 2. Check built-in standard legal disclaimer regexes
    for pattern in _BUILTIN_DISCLAIMER_PATTERNS:
        m = pattern.search(text)
        if m and m.start() < earliest_cut:
            earliest_cut = m.start()
            matched_reason = "builtin_disclaimer_pattern"

    if earliest_cut < len(text):
        cleaned = text[:earliest_cut].strip()
        if cleaned:
            logger.info(
                f"🛡️ Stripped email disclaimer ({matched_reason}), "
                f"{len(text)} -> {len(cleaned)} chars (saved ~{(len(text)-len(cleaned))//4} tokens)"
            )
            return cleaned
        else:
            logger.warning(
                "⚠️ Disclaimer stripping would have emptied the email body — "
                "keeping original body to prevent message loss"
            )
            return body

    return body


def is_html_content(text: str) -> bool:
    """Check if a string looks like raw HTML content."""
    if not text or not isinstance(text, str):
        return False
    lower = text.strip().lower()
    return (
        lower.startswith("<!doctype html")
        or lower.startswith("<html")
        or ("<head" in lower and "<body" in lower)
        or ("<table" in lower and "</table" in lower)
        or (lower.count("<p") + lower.count("<div") + lower.count("<br") >= 3)
    )


def extract_clean_text_from_html(html_content: str) -> str:
    """
    Converts raw HTML into clean, human-readable plain text.
    Strips scripts, styles, metadata, and comments, preserving line breaks.
    """
    if not html_content or not isinstance(html_content, str):
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")

        # Strip scripts, styles, head, meta
        for element in soup(["script", "style", "head", "meta", "noscript", "svg"]):
            element.decompose()

        # Add newlines around block tags
        for tag in soup.find_all(["p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"]):
            tag.insert_before("\n")

        text = soup.get_text()
    except Exception:
        # Fallback to regex-based HTML cleaning
        import html
        text = re.sub(r"<(script|style|head|meta)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        text = re.sub(r"<(?:br|p|div|tr|li|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)

    # Normalize whitespace and multiple consecutive blank lines
    lines = [line.strip() for line in text.splitlines()]
    non_empty_lines = []
    prev_blank = False
    for line in lines:
        if line:
            non_empty_lines.append(line)
            prev_blank = False
        elif not prev_blank:
            non_empty_lines.append("")
            prev_blank = True

    cleaned_text = "\n".join(non_empty_lines).strip()
    return cleaned_text