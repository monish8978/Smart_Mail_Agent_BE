import re
import json
import logging

from app.llm_utils import extract_ticket_and_order_ids, _format_history
from app.llm_config import client, current_client_id, resolve_model

logger = logging.getLogger(__name__)


# ==============================
# 🧠 Detect Intent
# ==============================
def detect_intent_llm(query: str) -> dict:
    prompt = f"""
You are a query classifier. Your only job is to analyze the user query and return structured JSON.

## Task
Classify the query into exactly one intent, extract ALL ticket_ids/order_ids if present, perform sentiment analysis, and assign a priority level.

## Intents
- `issue_resolved`: Customer explicitly indicates that their problem, issue, ticket, or inquiry has been resolved, fixed, sorted out, is working fine now, or they no longer need assistance (with no new questions or pending problems).
- `off_topic_nonsense`: Standalone greetings/casual openers with no issue described (e.g. "hello", "hi there", "hello ladies", "hey guys", "good morning"), unintelligible gibberish, keyboard mash, spam, test text, blank/random characters, or completely off-topic emails unrelated to company support, products, or services.
- `ticket_create`: User is explicitly asking to create, open, raise, or log a new ticket/complaint/case, or asking support to create a ticket for their issue
- `ticket_status`: User is asking about status of an existing ticket, order, complaint, delivery, or support request
- `marketing_promotional`: Marketing email, promotional campaign, newsletter, job alert blast, webinar invite, discount/sale offer, automated digest, or educational course advertisement (requiring no customer support action)
- `general_query`: Genuine customer support query, product question, policy inquiry, technical issue, or problem description requiring an answer (MUST contain an actual question, problem, or inquiry).

## Sentiment Analysis
Classify user sentiment into exactly one of:
- `Angry`: User shows frustration, anger, impatience, or threatens escalation/cancellation.
- `Neutral`: General query, factual, standard request, off-topic, or marketing/newsletter announcement.
- `Happy`: Expresses gratitude, happiness, satisfaction, or that their issue is resolved.

## Priority Tagging
Classify priority level into exactly one of:
- `Critical`: Urgent issues like order cancellation, immediate refunds, lawsuit threats, legal actions, security/data issues, or extreme user anger.
- `High`: General support issues with angry/impatient sentiment, or containing key words like "urgent", "broken", "cancel", "refund", "sue", "failed".
- `Medium`: General query or ticket status checks with neutral sentiment.
- `Low`: Marketing/newsletter emails, promotional updates, positive feedback, issue resolved notices, off-topic nonsense, or suggestions.

## Ticket & Order ID Extraction
Extract ALL ticket IDs, case numbers, order IDs, or tracking references mentioned in the query.
Examples:
- Numeric & Hash IDs: `#275424000000399001`, `275424000000399001`, `#98765`, `#123456`
- Support tickets: `T-260505-00117`, `T-YYMMDD-XXXXX`
- Helpdesk / Incident / Case IDs: `INC1234567`, `CAS-98765`, `SR-10293`
- Order / Tracking IDs: `ORD12345`, `ORD-98765`, `ORDER#54321`

## Rules
- Return ONLY raw JSON. No explanation, no markdown, no extra text.
- Extract ALL ticket/order IDs found in the query into the `ticket_ids` list. Return clean IDs (strip leading '#' symbols).
- If no ticket_id is found, set ticket_ids to empty list [].
- If the query is just a greeting, salutation, or pleasantry with no issue described (e.g. "hi", "hello", "hello ladies", "good morning", "how are you"), intent MUST be `off_topic_nonsense`.
- Do NOT classify a query as `general_query` unless it presents an actual question, problem description, product inquiry, or request for support.
- If intent is `issue_resolved`, sentiment is typically `Happy` or `Neutral` and priority is `Low`.
- If intent is `off_topic_nonsense` or `marketing_promotional`, sentiment is typically `Neutral` and priority is `Low`.

## Output Format
{{
  "intent": "issue_resolved" | "off_topic_nonsense" | "ticket_create" | "ticket_status" | "marketing_promotional" | "general_query",
  "ticket_ids": ["<id1>", "<id2>"] | [],
  "sentiment": "Angry" | "Neutral" | "Happy",
  "priority": "Critical" | "High" | "Medium" | "Low"
}}

## User Query
{query}
"""

    try:
        logger.info("🧠 Detecting intent using LLM")

        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "detect_intent_llm"),
            caller="detect_intent_llm",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a JSON-only response system. "
                        "Return ONLY valid JSON. No markdown, no explanation, no extra text."
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

        output = res.choices[0].message.content.strip()
        logger.info(f"🧠 Intent raw output: {output}")

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in LLM response")

        cleaned_output = match.group(0).strip()
        logger.info(f"🧹 Cleaned JSON output: {cleaned_output}")

        data = json.loads(cleaned_output)
        intent = data.get("intent", "general_query")
        raw_ticket_ids = data.get("ticket_ids", [])
        sentiment = data.get("sentiment", "Neutral")
        priority = data.get("priority", "Medium")

        if isinstance(raw_ticket_ids, str):
            raw_ticket_ids = [raw_ticket_ids] if raw_ticket_ids else []

        cleaned_ticket_ids = []
        for tid in raw_ticket_ids:
            if isinstance(tid, str):
                c = tid.strip().lstrip("#").strip()
                if c and re.search(r'\d', c) and c.lower() not in ("none", "null", "n/a", "unknown") and c not in cleaned_ticket_ids:
                    cleaned_ticket_ids.append(c)

        # Regex fallback verification if LLM missed ticket IDs
        if not cleaned_ticket_ids:
            regex_ids = extract_ticket_and_order_ids(query)
            if regex_ids:
                cleaned_ticket_ids = regex_ids
                logger.info(f"🔎 Regex supplemented ticket IDs: {cleaned_ticket_ids}")

        logger.info(f"✅ Intent detected: intent={intent}, ticket_ids={cleaned_ticket_ids}, sentiment={sentiment}, priority={priority}")
        return {
            "intent": intent, 
            "ticket_ids": cleaned_ticket_ids, 
            "sentiment": sentiment, 
            "priority": priority,
            "used_fallback": False
        }

    except Exception as e:
        logger.error(f"❌ Intent detection failed: {e}")

        ticket_ids = []
        try:
            ticket_ids = extract_ticket_and_order_ids(query)
        except Exception:
            pass

        q_lower = query.lower().strip()
        
        # Check resolved patterns
        resolved_keywords = [
            "issue is resolved", "issue resolved", "problem is resolved", "problem resolved",
            "solved now", "fixed now", "working now", "it works now", "working fine now",
            "all good now", "never mind", "nevermind", "please close the ticket", "close ticket",
            "no longer need help", "resolved my issue"
        ]
        
        if any(rk in q_lower for rk in resolved_keywords):
            fallback_intent = "issue_resolved"
            fallback_priority = "Low"
            fallback_sentiment = "Happy"
        elif ticket_ids:
            fallback_intent = "ticket_status"
            fallback_priority = "Medium"
            fallback_sentiment = "Neutral"
        else:
            fallback_intent = "general_query"
            if any(w in q_lower for w in ["sue", "legal", "lawyer", "court", "scam"]):
                fallback_priority = "Critical"
                fallback_sentiment = "Angry"
            elif any(w in q_lower for w in ["refund", "cancel", "urgent", "wrong", "fake", "bad", "worst"]):
                fallback_priority = "High"
                fallback_sentiment = "Angry"
            elif any(w in q_lower for w in ["thanks", "thank you", "great", "good", "happy"]):
                fallback_priority = "Low"
                fallback_sentiment = "Happy"
            else:
                fallback_priority = "Medium"
                fallback_sentiment = "Neutral"

        logger.info(f"🔁 Fallback intent: intent={fallback_intent}, ticket_ids={ticket_ids}, sentiment={fallback_sentiment}, priority={fallback_priority}")
        return {
            "intent": fallback_intent, 
            "ticket_ids": ticket_ids, 
            "sentiment": fallback_sentiment, 
            "priority": fallback_priority,
            "used_fallback": True
        }


# ==============================
# 🔍 Scan History for Ticket ID
# ==============================
def scan_history_for_ticket(query: str, history: list) -> dict:
    """
    LLM scans conversation history to find a relevant ticket/order ID
    for the current customer query.

    Returns:
        {"found": True,  "ticket_id": "275424000000399001", "ambiguous": False}
        {"found": False, "ticket_id": None, "ambiguous": False}
        {"found": True, "ticket_id": None, "ambiguous": True, "ticket_ids": [...]}
    """
    if not history:
        return {"found": False, "ticket_id": None, "ambiguous": False}

    history_text = _format_history(history)

    prompt = f"""
You are a support assistant analyzing a conversation history to find a relevant ticket or order ID.

## Current Customer Query
{query}

## Conversation History
{history_text}

## Task
1. Look through the conversation history for any ticket/case IDs (e.g. #275424000000399001, T-260601-12345, INC123456) or order IDs (e.g. ORD12345, #98765).
2. Determine if any of them are relevant to the current query.
3. CRITICAL FOLLOW-UP RULE: If the customer's query is a follow-up inquiry, question, or reply referring to their ongoing conversation, past problem, root cause, or resolution (e.g. "why was the problem", "what caused it", "why did this happen", "is it resolved", "thank you", "any update"), map it to the most recent ticket ID found in the conversation history rather than returning false.
4. If one is clearly relevant or inferred from the thread, return it (clean ID without '#').
5. If multiple distinct unresolved tickets exist and you genuinely cannot determine which is relevant, return them as ambiguous.
6. If no ticket IDs exist anywhere in the conversation history, return not found.

## Output Format
Return ONLY valid JSON. No explanation. No markdown.

If one relevant ID found:
{{"found": true, "ticket_id": "<id>", "ambiguous": false}}

If multiple found and cannot decide:
{{"found": true, "ticket_id": null, "ambiguous": true, "ticket_ids": ["<id1>", "<id2>"]}}

If none found:
{{"found": false, "ticket_id": null, "ambiguous": false}}
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "scan_history_for_ticket"),
            caller="scan_history_for_ticket",
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
        logger.info(f"🔍 History scan raw output: {output}")

        match = re.search(r'\{.*\}', output, re.DOTALL)
        if not match:
            raise ValueError("No JSON found in history scan response")

        data = json.loads(match.group(0).strip())
        
        tid = data.get("ticket_id")
        if isinstance(tid, str):
            data["ticket_id"] = tid.strip().lstrip("#").strip()

        tids = data.get("ticket_ids", [])
        if isinstance(tids, list):
            cleaned_list = []
            for item in tids:
                if isinstance(item, str):
                    c = item.strip().lstrip("#").strip()
                    if c and c not in cleaned_list:
                        cleaned_list.append(c)
            data["ticket_ids"] = cleaned_list

        logger.info(f"✅ History scan result: {data}")
        return data

    except Exception as e:
        logger.error(f"❌ History scan failed: {e}")
        history_ids = extract_ticket_and_order_ids(history_text)
        if len(history_ids) == 1:
            return {"found": True, "ticket_id": history_ids[0], "ambiguous": False}
        elif len(history_ids) > 1:
            return {"found": True, "ticket_id": None, "ambiguous": True, "ticket_ids": history_ids}
        return {"found": False, "ticket_id": None, "ambiguous": False}
