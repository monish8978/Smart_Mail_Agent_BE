import os
import re
import logging
from app.llm import resolve_model, current_client_id, client

logger = logging.getLogger(__name__)


# ==============================
# 🧠 LLM BASED SCORING
# ==============================
def llm_score(reply, query):

    prompt = f"""
You are a STRICT evaluator for customer support email replies.

Evaluate the reply based on the query.

Scoring Rules:
- 90-100 → Perfect, accurate, complete, very helpful
- 70-89 → Good but slightly incomplete
- 50-69 → Average, vague or partially helpful
- 30-49 → Poor, unclear or missing key info
- 0-29 → Wrong, irrelevant, or hallucinated

STRICT PENALTIES:
- If reply does NOT answer the query → score MUST be below 50
- If reply is generic → max 70
- If wrong info → below 40
- If order query but no order details → max 60
- If tone is bad → subtract heavily

Checklist:
1. Does it answer the query?
2. Is it specific (not generic)?
3. Is it correct?
4. Is it helpful?
5. Is tone professional?

Query:
{query}

Reply:
{reply}

Return ONLY a number between 0 and 100.
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "llm_score"),
            messages=[
                {"role": "system", "content": "You are a strict evaluator. Return ONLY a single integer between 0 and 100. No explanation. No reasoning. No text."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            reasoning_effort="none"
        )

        output = res.choices[0].message.content.strip()
        logger.info(f"📊 Raw score output: {output}")

        match = re.search(r'\d+', output)
        if match:
            score = int(match.group())
        else:
            score = 60

    except Exception as e:
        logger.error(f"❌ LLM scoring failed: {e}")
        score = 60

    # Apply rule-based penalties
    score += rule_based_penalty(reply, query)

    # Clamp score between 0–100
    score = max(0, min(100, score))

    logger.info(f"📊 Final score after rules: {score}")

    return score


# ==============================
# ⚙️ RULE BASED PENALTIES
# ==============================
def rule_based_penalty(reply, query):
    penalty = 0

    reply_lower = reply.lower()
    query_lower = query.lower()

    # 🚫 Order query but no order info
    if "order" in query_lower:
        if not any(word in reply_lower for word in ["order", "status", "tracking", "delivery"]):
            penalty -= 30

    # 🚫 Too short (generic reply)
    if len(reply.split()) < 12:
        penalty -= 20

    # 🚫 Very generic phrases
    generic_phrases = [
        "we will get back to you",
        "thank you for reaching out",
        "we are looking into it"
    ]
    if any(p in reply_lower for p in generic_phrases):
        penalty -= 10

    # 🚫 No numbers when expected (like order id)
    if "ord" in query_lower and "ord" not in reply_lower:
        penalty -= 25

    return penalty


