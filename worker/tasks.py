from app.connector_config import run_order_status_lookup



from worker.celery_worker import celery
from app.rag import query_rag, get_rag_id, query_knowledge
from app.llm import generate_reply_llm, detect_intent_llm, scan_history_for_ticket, extract_issue_description, generate_summary_llm, extract_ticket_and_order_ids
from app.scoring import llm_score
from app.decision import decision_engine
from app.mailer import send_email
from app.db import get_db
from app.chat_history import push_message, get_history, get_pending_state, upsert_ticket_history, get_ticket_history
import random
from datetime import datetime

from app.keyword_filter import get_blocked_keywords, is_blocked, insert_blocked_email

from app.text_cleaning import strip_quoted_reply, strip_disclaimers
from app.email_disclaimers import get_active_disclaimer_texts

import logging
import re
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def extract_order_id(text):
    return extract_ticket_and_order_ids(text)

def generate_ticket_id():
    date_part = datetime.now().strftime("%y%m%d")
    random_part = str(random.randint(0, 99999)).zfill(5)
    return f"T-{date_part}-{random_part}"



def get_client_features(cursor, client_id):
    defaults = {
        "feature_ticket_creation": True, "feature_auto_send": True,
        "feature_rag": True, "feature_order_tracking": True, "feature_manual_reply": True,
        "feature_strip_disclaimers": True,
        "admin_bot_enabled": True, "client_bot_enabled": True,
    }
    try:
        cursor.execute("""
            SELECT feature_ticket_creation, feature_auto_send, feature_rag,
                   feature_order_tracking, feature_manual_reply,
                   COALESCE(feature_strip_disclaimers, 1),
                   COALESCE(admin_bot_enabled, 1), COALESCE(client_bot_enabled, 1)
            FROM email_accounts WHERE client_id = %s
        """, (client_id,))
        row = cursor.fetchone()
        if row:
            return {
                "feature_ticket_creation": bool(row[0]), "feature_auto_send": bool(row[1]),
                "feature_rag": bool(row[2]), "feature_order_tracking": bool(row[3]),
                "feature_manual_reply": bool(row[4]),
                "feature_strip_disclaimers": bool(row[5]) if row[5] is not None else True,
                "admin_bot_enabled": bool(row[6]) if row[6] is not None else True,
                "client_bot_enabled": bool(row[7]) if row[7] is not None else True,
            }
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch client features for {client_id}, using defaults: {e}")
    return defaults


def _dispatch_or_draft_reply(
    client_id: str,
    from_email: str,
    subject: str,
    reply_body: str,
    features: dict,
    confidence_score: int = 0,
    intent: str = None,
    sentiment: str = None,
    priority: str = "Normal",
    ticket_id: str = None,
    original_body: str = "",
    in_reply_to: str = None,
    message_id: str = None,
    sender_name: str = None,
    execution_steps: list = None,
) -> tuple:
    """
    If feature_auto_send is True, dispatches reply immediately via SMTP.
    If feature_auto_send is False (Draft Mode), creates a pending draft in draft_emails.
    Returns (status_str, save_history_bool)
    """
    if features.get("feature_auto_send", True):
        logger.info(f"📤 [Client {client_id}] Auto-send enabled — dispatching reply via SMTP")
        send_email(client_id, from_email, "Re: " + subject, reply_body, in_reply_to=in_reply_to)
        if execution_steps is not None:
            execution_steps.append("SMTP_Send")
        return "sent", True
    else:
        logger.info(f"📝 [Client {client_id}] Auto-send disabled (Draft Mode) — saving reply to draft_emails")
        try:
            from app.draft_service import create_draft
            draft_id = create_draft(
                client_id=client_id,
                from_email=from_email,
                to_email=from_email,
                subject=subject,
                original_body=original_body,
                draft_reply=reply_body,
                confidence_score=confidence_score,
                intent=intent,
                sentiment=sentiment,
                priority=priority,
                ticket_id=ticket_id,
                in_reply_to=in_reply_to,
                message_id=message_id,
                sender_name=sender_name,
            )
            logger.info(f"✅ Draft created successfully with ID #{draft_id}")
        except Exception as d_err:
            logger.error(f"❌ Failed to create draft in draft_emails: {d_err}", exc_info=True)
        if execution_steps is not None:
            execution_steps.append("Saved_To_Drafts")
        return "draft_created", False


def publish_email_update(client_id: str):
    try:
        import os
        import redis
        redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0") or "redis://localhost:6379/0"
        r = redis.from_url(redis_url)
        r.publish("email_updates", json.dumps({"type": "NEW_EMAIL", "client_id": client_id}))
        logger.info(f"📡 Published real-time update to 'email_updates' channel for client {client_id}")
        r.close()
    except Exception as e:
        logger.warning(f"⚠️ Failed to publish real-time notification: {e}")

# ==============================
# Templated verification emails
# (deterministic, no LLM cost)
# ==============================
def _tpl_please_verify(ticket_id: str, customer_name: str = "Customer") -> str:
    return (
        f"Hi {customer_name},\n\n"
        f"Thank you for reaching out. We received your query regarding ticket "
        f"**{ticket_id}**, but we were unable to locate this ID in our system.\n\n"
        f"Could you please double-check the ticket ID and confirm it in your reply? "
        f"If you have a reference email or screenshot, feel free to attach it.\n\n"
        f"Thanks & Regards,\n"
        f"Support Team"
    )

def _tpl_verification_failed(customer_name: str = "Customer") -> str:
    return (
        f"Hi {customer_name},\n\n"
        f"We were still unable to locate the ticket ID in our system after verification.\n\n"
        f"No worries — please describe your issue in your next reply and we will raise a "
        f"fresh support ticket on your behalf right away.\n\n"
        f"Thanks & Regards,\n"
        f"Support Team"
    )


def generate_and_save_summary(db, cursor, log_id, data, chroma_context):
    try:
        client_id = data.get("client_id")
        from_email = data.get("from_email")
        subject = data.get("subject")
        body = data.get("body")
        
        import re
        # Subject normalizer helper
        norm_subject = re.sub(r'^(Re|RE|Fwd|FWD|fwd|re):\s*', '', subject).strip()
        
        # Fetch up to 5 prior emails from same sender
        cursor.execute("""
            SELECT body, reply, summary 
            FROM email_logs 
            WHERE client_id = %s 
              AND from_email = %s 
              AND id < %s
            ORDER BY id DESC LIMIT 5
        """, (client_id, from_email, log_id))
        prior_rows = cursor.fetchall()
        
        old_summary = ""
        history_list = []
        for r in prior_rows:
            if r[2] and not old_summary:
                old_summary = r[2]
            history_list.insert(0, {"role": "customer", "body": r[0]})
            if r[1]:
                history_list.insert(1, {"role": "support", "body": r[1]})
                
        from app.llm import generate_summary_llm
        summary = generate_summary_llm(
            context=chroma_context,
            customer_body=body,
            history=history_list,
            old_summary=old_summary
        )
        
        cursor.execute("UPDATE email_logs SET summary = %s WHERE id = %s", (summary, log_id))
        logger.info(f"📊 Summary generated & updated for ID {log_id}: {summary}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to generate/save summary: {e}")


@celery.task(bind=True, max_retries=3)
def process_email_task(self, data):
    logger.info(f"📥 Received email task: from={data.get('from_email')} subject={data.get('subject')}")

    from app.llm import current_client_id
    client_id = data.get("client_id", "SYSTEM")
    ctx_token = current_client_id.set(client_id)

    db = None
    try:
        db = get_db()
        cursor = db.cursor()
        # ==============================
# Idempotency Check
# ==============================
        task_id = self.request.id

        cursor.execute("""
    CREATE TABLE IF NOT EXISTS celery_task_log (
        task_id     VARCHAR(255) NOT NULL PRIMARY KEY,
        client_id   VARCHAR(50)  NOT NULL,
        from_email  VARCHAR(255) NOT NULL,
        status      VARCHAR(50)  NOT NULL DEFAULT 'processing',
        created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
""")

        cursor.execute(
    "SELECT status FROM celery_task_log WHERE task_id = %s",
    (task_id,)
)
        existing = cursor.fetchone()

        if existing:
            existing_status = existing[0]
            if existing_status == "completed":
                logger.info(f"⏭ Task {task_id} already completed — skipping to prevent duplicate")
                db.close()
                return
            elif existing_status == "processing":
                logger.warning(f"🔄 Task {task_id} is a retry — continuing carefully")
        else:
            cursor.execute(
        "INSERT INTO celery_task_log (task_id, client_id, from_email, status) VALUES (%s, %s, %s, 'processing')",
        (task_id, data.get("client_id", "SYSTEM"), data.get("from_email", ""))
    )
            db.commit()


        # Ensure body is clean text and preserve raw HTML
        body_text = data.get("body", "") or ""
        body_html = data.get("body_html", "") or ""

        from app.text_cleaning import extract_clean_text_from_html, is_html_content
        if is_html_content(body_text):
            if not body_html:
                body_html = body_text
            body_text = extract_clean_text_from_html(body_text)

        # ==============================
        # Check Master Bot Automation Switch
        # ==============================
        features = get_client_features(cursor, client_id)
        admin_bot_enabled = features.get("admin_bot_enabled", True)
        client_bot_enabled = features.get("client_bot_enabled", True)

        if not admin_bot_enabled or not client_bot_enabled:
            reason = "Disabled by Administrator" if not admin_bot_enabled else "Paused by Client"
            logger.info(f"🛑 [Client {client_id}] Master Bot Switch is OFF ({reason}) — halting task without processing")
            status = 'automation_halted'
            execution_steps = ["Start", f"Master_Switch_Halt:{reason}"]
            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps, summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                client_id, data.get('from_email'), data.get('subject'), body_text, body_html or None,
                None, 0, status, None, 'Neutral', 'Low',
                json.dumps(execution_steps),
                f"Master automation switch is OFF ({reason}). Email flow halted."
            ))
            cursor.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s", (task_id,))
            db.commit()
            db.close()
            publish_email_update(client_id)
            return

        # ==============================
        # Check if Email is Paused
        # ==============================
        cursor.execute("SELECT id FROM paused_emails WHERE client_id = %s AND paused_email = %s", (client_id, data.get('from_email')))
        if cursor.fetchone():
            logger.info(f"⏸️ Email from {data.get('from_email')} is paused. Skipping auto-reply.")
            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, status, priority, sentiment, execution_steps)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (client_id, data.get('from_email'), data.get('subject'), body_text, body_html or None, 'paused', 'Medium', 'Neutral', json.dumps(["Start", "Paused"])))

            from app.paused_email_history import ensure_paused_email_history_table
            ensure_paused_email_history_table(cursor)
            cursor.execute("""
                INSERT INTO paused_email_history (client_id, from_email, subject, body, status)
                VALUES (%s, %s, %s, %s, 'pending_review')
            """, (client_id, data.get('from_email'), data.get('subject'), body_text))

            db.commit()
            db.close()
            publish_email_update(client_id)
            return
        
        blocked_keywords = get_blocked_keywords(cursor, client_id)
        email_text = f"{data.get('subject','')} {body_text}"
        matched_kw = is_blocked(email_text, blocked_keywords) if blocked_keywords else None

        if matched_kw:
            status = 'pending_review'
            logger.info(f"🚫 Email matched blocked keyword '{matched_kw}' — routing to {status}")
            insert_blocked_email(
                cursor, client_id,
                data["from_email"], data["subject"], body_text,
                matched_kw, status=status
            )
            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, status, priority, sentiment, execution_steps)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (client_id, data.get('from_email'), data.get('subject'), body_text, body_html or None,
                'blocked_keyword', 'High', 'Neutral',
                json.dumps(["Start", f"Blocked_Keyword:{matched_kw}"])))
            cursor.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s", (task_id,))
            db.commit()
            db.close()
            publish_email_update(client_id)
            return

        # ==============================
        # Check if Sender is Marked as Marketing / Promotional
        # ==============================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marketing_senders (
                id INT AUTO_INCREMENT PRIMARY KEY,
                client_id VARCHAR(50) NOT NULL,
                sender_email VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_client_sender (client_id, sender_email)
            )
        """)
        from_email_raw = data.get('from_email', '') or ''
        from_email_clean = from_email_raw.lower().strip()
        cursor.execute("""
            SELECT id, sender_email FROM marketing_senders 
            WHERE client_id = %s 
              AND (LOWER(sender_email) = %s OR %s LIKE CONCAT('%%@', LOWER(sender_email)))
        """, (client_id, from_email_clean, from_email_clean))
        matched_sender = cursor.fetchone()

        if matched_sender:
            logger.info(f"📢 [Client {client_id}] Sender '{from_email_clean}' is in marketing_senders (rule: {matched_sender[1]}) — bypassing all processing")
            status = 'no_action_needed'
            execution_steps = ["Start", f"Rule_Match:Marketing_Sender:{matched_sender[1]}"]
            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps, summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                client_id, data.get('from_email'), data.get('subject'), body_text, body_html or None,
                None, 0, status, rag_id if 'rag_id' in locals() else None, 'Neutral', 'Low',
                json.dumps(execution_steps),
                f"Marketing email from marked sender ({matched_sender[1]}). No automated processing needed."
            ))
            cursor.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s", (task_id,))
            db.commit()
            db.close()
            publish_email_update(client_id)
            return

        # ==============================
        # Get RAG ID / ChromaDB context
        # ==============================
        client_id = data.get("client_id")
        features = get_client_features(cursor, client_id)
        rag_id = get_rag_id(client_id)
        logger.info(f"🔎 RAG ID: {rag_id} Client ID: {client_id}")

        cleaned_body = strip_quoted_reply(body_text)
        if features.get("feature_strip_disclaimers", True):
            try:
                active_disclaimers = get_active_disclaimer_texts(client_id)
                cleaned_body = strip_disclaimers(cleaned_body, active_disclaimers)
            except Exception as e:
                logger.warning(f"⚠️ Failed to strip disclaimers: {e}")

        data["body"] = cleaned_body
        email_query = f"Subject: {data['subject']}\n\n{cleaned_body}"

        chroma_context = ""
        if client_id and features.get("feature_rag", True):
            try:
                chroma_context = query_knowledge(client_id, email_query)
            except Exception as e:
                logger.error(f"❌ ChromaDB Query failed: {e}")

        # ==============================
        # STEP 1: Intent Detection
        # ==============================
        execution_steps = ["Start", "Intent_Detection"]
        
        intent_data = detect_intent_llm(email_query)
        intent     = intent_data.get("intent")
        ticket_ids = intent_data.get("ticket_ids", [])
        sentiment  = intent_data.get("sentiment", "Neutral")
        priority   = intent_data.get("priority", "Medium")
        used_fallback = intent_data.get("used_fallback", False)

        # ==============================
        # MARKETING / PROMOTIONAL / NO-ACTION HANDLER
        # ==============================
        if intent in ("marketing_promotional", "no_action_needed"):
            logger.info(f"📢 [Client {client_id}] Email classified as '{intent}' — auto-routing to no_action_needed")
            execution_steps.append("Marketing_Promotional_No_Action")
            status = "no_action_needed"
            priority = "Low"
            sentiment = "Neutral"

            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                client_id,
                data.get("from_email"),
                data.get("subject"),
                body_text,
                body_html or None,
                None,
                0,
                status,
                rag_id,
                sentiment,
                priority,
                json.dumps(execution_steps)
            ))
            db_log_id = cursor.lastrowid

            generate_and_save_summary(db, cursor, db_log_id, {**data, "body": body_text}, chroma_context)
            cursor.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s", (task_id,))
            db.commit()
            db.close()
            publish_email_update(client_id)
            logger.info(f"✅ Marketing email #{db_log_id} recorded as no_action_needed, task done")
            return

        # Keyword-based escalation ONLY applies when the LLM itself failed
        # and detect_intent_llm fell back to regex/keyword classification.
        # When the LLM succeeded, its sentiment/priority judgment is trusted
        # as-is — this block must not override a working LLM result.
        if used_fallback:
            q_lower = email_query.lower()
            if any(w in q_lower for w in ["sue", "legal", "lawyer", "court", "scam"]):
                priority = "Critical"
                sentiment = "Angry"
            elif any(w in q_lower for w in ["refund", "cancel", "urgent", "wrong", "fake", "bad", "worst"]):
                if priority not in ["Critical", "High"]:
                    priority = "High"
                if sentiment == "Neutral":
                    sentiment = "Angry"
        else:
            logger.info(f"🎯 LLM classification trusted (no fallback) — skipping keyword escalation override")

        if not ticket_ids:
            fallback = extract_order_id(email_query)
            if fallback:
                ticket_ids = fallback

        # Deduplicate — same ID in subject + body causes false multi-ticket
        ticket_ids = list(dict.fromkeys(ticket_ids))

        logger.info(f"🎯 Intent: {intent} Ticket IDs: {ticket_ids} Sentiment: {sentiment} Priority: {priority}")

        # ==============================
        # MULTI-TICKET CLARIFICATION
        # ==============================
        if len(ticket_ids) > 1:
            logger.warning(f"⚠️ Multiple ticket IDs in email: {ticket_ids}")
            execution_steps.append("Clarification_Request")

            id_list = "\n".join(f"  - {tid}" for tid in ticket_ids)
            clarification_reply = (
                f"Dear Customer,\n\n"
                f"Thank you for reaching out. We noticed your email mentions multiple ticket/order IDs:\n\n"
                f"{id_list}\n\n"
                f"Could you please clarify which ticket you would like us to look into? "
                f"Replying with a single ticket ID will help us assist you faster.\n\n"
                f"Thanks & Regards,\n"
                f"Support Team"
            )

            send_email(
                data.get("client_id"),
                data["from_email"],
                "Re: " + data["subject"],
                clarification_reply
            )

            cursor.execute("""
                INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                data.get("client_id"),
                data["from_email"],
                data["subject"],
                body_text,
                body_html or None,
                clarification_reply,
                0,
                "clarification_sent",
                rag_id,
                sentiment,
                priority,
                json.dumps(execution_steps)
            ))
            db_log_id = cursor.lastrowid
            generate_and_save_summary(db, cursor, db_log_id, {**data, "body": body_text}, chroma_context)
            db.commit()
            db.close()
            publish_email_update(client_id)
            logger.info("✅ Clarification sent, task done")
            return

        ticket_id = ticket_ids[0] if ticket_ids else None

        # ==============================
        # Load conversation history
        # ==============================
        history = get_history(client_id, data["from_email"], last_n=15)
        logger.info(f"📜 Loaded {len(history)} history messages")

        # ==============================
        # STEP 2: Routing
        # ==============================
        save_to_history = False
        outgoing_ticket_id = None
        reply = None
        score = 0
        status = "pending"

        # Pre-load verification state once — reused by all paths to avoid
        # hitting Redis multiple times per task.
        active_state = get_pending_state(client_id, data["from_email"])
        active_state_name = active_state.get("state") if active_state else None
        _VERIFICATION_STATES = ("pending_verification", "verification_failed")

        if active_state_name:
            logger.info(f"🔒 Active verification state detected: {active_state_name} — PATHs A and B will be skipped")

        if not features["feature_order_tracking"] and (intent == "ticket_status" or ticket_id):
            logger.info("🚫 feature_order_tracking disabled — forcing general_query path")
            intent = "general_query"    

        # ──────────────────────────────
        # TICKET STATUS PRE-RESOLUTION:
        # If user asks for ticket status but no ID was in the email body/subject,
        # scan conversation history before falling back. If no ticket ID exists
        # in history either, send a clarification requesting the ticket ID.
        # ──────────────────────────────
        if intent == "ticket_status" and not ticket_id and active_state_name not in _VERIFICATION_STATES:
            logger.info("🔍 ticket_status intent detected without ticket_id in email — checking history")
            scan_result = scan_history_for_ticket(email_query, history)
            if scan_result.get("ambiguous"):
                ambiguous_ids = scan_result.get("ticket_ids", [])
                logger.warning(f"⚠️ Ambiguous history IDs for ticket_status: {ambiguous_ids}")
                execution_steps.append("Clarification_Request")
                id_list = "\n".join(f"  - {tid}" for tid in ambiguous_ids)
                clarification_reply = (
                    f"Dear Customer,\n\n"
                    f"Thank you for reaching out. We found multiple previous tickets in your conversation history:\n\n"
                    f"{id_list}\n\n"
                    f"Could you please let us know which ticket number you would like an update on?\n\n"
                    f"Thanks & Regards,\n"
                    f"Support Team"
                )
                send_email(client_id, data["from_email"], "Re: " + data["subject"], clarification_reply)
                cursor.execute("""
                    INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    client_id, data["from_email"], data["subject"], body_text, body_html or None,
                    clarification_reply, 0, "clarification_sent", rag_id, sentiment, priority, json.dumps(execution_steps)
                ))
                db_log_id = cursor.lastrowid
                generate_and_save_summary(db, cursor, db_log_id, {**data, "body": body_text}, chroma_context)
                db.commit()
                db.close()
                publish_email_update(client_id)
                logger.info("✅ Ambiguous ticket clarification sent, task done")
                return

            elif scan_result.get("found") and scan_result.get("ticket_id"):
                ticket_id = scan_result.get("ticket_id")
                ticket_ids = [ticket_id]
                logger.info(f"✅ Found ticket ID from history: {ticket_id}")
            else:
                # No ticket ID in email AND no ticket ID in history -> ask customer for ticket ID
                logger.info("ℹ️ ticket_status inquiry with no ticket ID found in email or history — asking customer for ticket details")
                execution_steps.append("Clarification_Request")
                from app.llm import extract_name_from_email
                customer_name = extract_name_from_email(data["from_email"])
                clarification_reply = (
                    f"Dear {customer_name},\n\n"
                    f"Thank you for contacting us regarding your request. Could you please provide your ticket ID or order number (e.g., #275424000000399001 or T-260505-00117) so we can check the status and assist you promptly?\n\n"
                    f"Thanks & Regards,\n"
                    f"Customer Support"
                )
                send_email(client_id, data["from_email"], "Re: " + data["subject"], clarification_reply)
                cursor.execute("""
                    INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    client_id, data["from_email"], data["subject"], body_text, body_html or None,
                    clarification_reply, 0, "clarification_sent", rag_id, sentiment, priority, json.dumps(execution_steps)
                ))
                db_log_id = cursor.lastrowid
                generate_and_save_summary(db, cursor, db_log_id, {**data, "body": body_text}, chroma_context)
                db.commit()
                db.close()
                publish_email_update(client_id)
                logger.info("✅ Ticket ID request clarification sent, task done")
                return

        # ──────────────────────────────
        # PATH A: ticket_status intent OR ticket_id present in email.
        # Skipped when a verification flow is already active — the quoted
        # ticket ID in the reply thread must not re-trigger PATH A.
        # ──────────────────────────────
        if (intent == "ticket_status" or ticket_id) and active_state_name not in _VERIFICATION_STATES:
            logger.info("🚀 PATH A — ticket/order status from email")
            execution_steps.append("Order_Check")

            from app.connector_config import run_order_status_lookup
            api_res = run_order_status_lookup(
                client_id=client_id,
                ticket_id=ticket_id,
                body=data["body"],
                history=history,
                subject=data["subject"],
                from_email=data["from_email"],
                sentiment=sentiment,
                priority=priority,
                intent=intent,
            )

            if api_res.get("success"):
                from app.connector_executor import format_mapped_data_for_prompt
                ticket_info = api_res.get("data", {})
                context = format_mapped_data_for_prompt(ticket_info)
                
                logger.info("📄 Ticket context ready from API")

                reply = generate_reply_llm(
                    context, data["body"], "crm_support_agent",
                    data["from_email"], history=history
                )
                score = llm_score(reply, data["body"])

                status, sent_ok = _dispatch_or_draft_reply(
                    client_id=client_id,
                    from_email=data["from_email"],
                    subject=data["subject"],
                    reply_body=reply,
                    features=features,
                    confidence_score=score,
                    intent=intent,
                    sentiment=sentiment,
                    priority=ticket_info.get("priority_name", "Normal"),
                    ticket_id=ticket_id,
                    original_body=data["body"],
                    in_reply_to=data.get("message_id"),
                    message_id=data.get("message_id"),
                    execution_steps=execution_steps,
                )
                save_to_history = sent_ok

                # Upsert MySQL chat_history with latest API status/priority + updated summary
                old_row = get_ticket_history(ticket_id)
                old_summary = old_row.get("summary", "") if old_row else ""
                summary = generate_summary_llm(
                    context=context,
                    customer_body=data["body"],
                    history=history,
                    old_summary=old_summary
                )
                upsert_ticket_history(
                    client_id=client_id,
                    ticket_id=ticket_id,
                    customer_email=data["from_email"],
                    summary=summary,
                    priority=ticket_info.get("priority_name", "Normal"),
                    status=ticket_info.get("ticket_status", "NEW")
                )

            else:
                # Customer gave us a specific ticket ID and API 404'd
                if ticket_id:
                    logger.warning(
                        f"⚠️ PATH A — API 404 for customer-supplied ticket {ticket_id}"
                        f" — starting pending_verification flow"
                    )
                    from app.llm import extract_name_from_email
                    customer_name = extract_name_from_email(data["from_email"])

                    reply = _tpl_please_verify(ticket_id, customer_name)
                    send_email(
                        client_id,
                        data["from_email"],
                        "Re: " + data["subject"],
                        reply
                    )
                    status = "pending_verification"
                    execution_steps.append("Ticket_Escalation")

                    push_message(
                        client_id=client_id,
                        from_email=data["from_email"],
                        role="customer",
                        subject=data["subject"],
                        body=data["body"],
                        ticket_id=""
                    )
                    push_message(
                        client_id=client_id,
                        from_email=data["from_email"],
                        role="support",
                        subject="Re: " + data["subject"],
                        body=reply,
                        ticket_id="",
                        meta={"state": "pending_verification", "ticket_id": ticket_id}
                    )
                    save_to_history = False

                else:
                    logger.warning("⚠️ PATH A — API failed with no ticket_id, falling to general flow")
                    intent = "general_query"

        # ──────────────────────────────
        # PATH DIRECT: explicit ticket_create intent
        # ──────────────────────────────
        if reply is None and intent == "ticket_create" and active_state_name not in _VERIFICATION_STATES:
            if not features.get("feature_ticket_creation", True):
                logger.info("⏸ feature_ticket_creation disabled — holding in manual queue")
                execution_steps.append("Held_For_Manual_Review_No_Ticket")
                status = "pending_manual_review"
                reply = None
                save_to_history = False
            else:
                logger.info("🎫 PATH TICKET CREATE — explicit ticket_create intent detected")
                execution_steps.append("Ticket_Escalation")
                reply, outgoing_ticket_id, status = _create_ticket_and_reply(
                    data, client_id, context="", history=history,
                    cursor=cursor, sentiment=sentiment, priority=priority,
                    features=features
                )
                if status == "ticket_creation_failed":
                    logger.error("❌ Ticket creation failed — holding for manual review")
                    execution_steps.append("Ticket_Creation_Failed")
                    status = "pending_manual_review"
                    save_to_history = False
                else:
                    save_to_history = (status == "ticket_created_and_sent")
                    if outgoing_ticket_id:
                        summary = generate_summary_llm(context="", customer_body=data["body"],
                                                        history=history, old_summary="")
                        upsert_ticket_history(client_id=client_id, ticket_id=outgoing_ticket_id,
                                               customer_email=data["from_email"], summary=summary,
                                               priority=priority, status="NEW")

        # ──────────────────────────────
        # PATH B: general_query — try RAG first.
        # Skipped when a verification flow is active — a high RAG score on the
        # customer's issue description must not bypass the verification_failed
        # → create ticket branch.
        # Also skipped if intent is ticket_status or ticket_create.
        # ──────────────────────────────
        if reply is None and active_state_name in _VERIFICATION_STATES:
            logger.info(f"⏭ PATH B skipped — active verification state: {active_state_name}")

        if reply is None and intent not in ("ticket_create", "ticket_status") and active_state_name not in _VERIFICATION_STATES and features["feature_rag"]:
            logger.info("📚 PATH B — trying RAG/ChromaDB")
            execution_steps.append("RAG_Search")

            if chroma_context:
                context = chroma_context
            elif rag_id:
                rag_res = query_rag(rag_id, email_query)
                context = rag_res.get("answer", "")
            else:
                context = ""

            rag_succeeded = bool(context and "No context found" not in context)

            if rag_succeeded:
                logger.info("✅ RAG succeeded — generating reply")

                reply = generate_reply_llm(
                    context, data["body"], "crm_support_agent",
                    data["from_email"], history=history
                )
                score = llm_score(reply, data["body"])

                from worker.credential_service import get_email_score_threshold
                threshold = get_email_score_threshold(client_id)
                logger.info(f"📊 Score={score} Threshold={threshold}")
                execution_steps.append("Confidence_Evaluation")

                if score >= threshold:
                    status, sent_ok = _dispatch_or_draft_reply(
                        client_id=client_id,
                        from_email=data["from_email"],
                        subject=data["subject"],
                        reply_body=reply,
                        features=features,
                        confidence_score=score,
                        intent=intent,
                        sentiment=sentiment,
                        priority=priority,
                        ticket_id=None,
                        original_body=data["body"],
                        in_reply_to=data.get("message_id"),
                        message_id=data.get("message_id"),
                        execution_steps=execution_steps,
                    )
                    save_to_history = False
                else:
                    logger.warning("⚠️ RAG score below threshold — falling to history scan")
                    execution_steps.append("Ticket_Escalation")
                    reply = None
                    score = 0

        # ──────────────────────────────
        # PATH C: RAG failed or low score.
        #
        # Verification state machine:
        #   [no state]             → scan history for ticket_id
        #                            → API 404 → "please verify" → pending_verification
        #   [pending_verification] → re-try API → still 404 → verification_failed
        #   [verification_failed]  → extract description → create ticket directly
        #
        # Note: PATH A now handles the case where the customer supplies a ticket_id
        # directly and the API 404s, so PATH C only sees pending_verification /
        # verification_failed on follow-up replies from that flow.
        # ──────────────────────────────
        if reply is None:
            logger.info("🔍 PATH C — checking conversation state")

            pending_state = active_state
            state_name = active_state_name
            logger.info(f"🔍 Pending state: {state_name}")

            if state_name == "verification_failed":
                effective_id = ticket_id or pending_state.get("ticket_id")
                if effective_id:
                    from app.connector_config import run_order_status_lookup
                    logger.info(f"🔄 PATH C / verification_failed — re-checking connector for {effective_id} before escalating")
                    api_res = run_order_status_lookup(
                        client_id=client_id, ticket_id=effective_id, body=data["body"],
                        history=history, subject=data["subject"], from_email=data["from_email"],
                        sentiment=sentiment, priority=priority, intent=intent,
                    )
                    if api_res.get("success"):
                        logger.info(f"✅ Record found for {effective_id} — resolving inquiry and clearing verification state")
                        from app.connector_executor import format_mapped_data_for_prompt
                        ticket_info = api_res.get("data", {})
                        context = format_mapped_data_for_prompt(ticket_info)
                        reply = generate_reply_llm(context, data["body"], "crm_support_agent", data["from_email"], history=history)
                        score = llm_score(reply, data["body"])
                        status, sent_ok = _dispatch_or_draft_reply(
                            client_id=client_id, from_email=data["from_email"], subject=data["subject"],
                            reply_body=reply, features=features, confidence_score=score, intent=intent,
                            sentiment=sentiment, priority=ticket_info.get("priority_name", "Normal"),
                            ticket_id=effective_id, original_body=data["body"], in_reply_to=data.get("message_id"),
                            message_id=data.get("message_id"), execution_steps=execution_steps,
                        )
                        push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                     subject=data["subject"], body=data["body"], ticket_id=effective_id)
                        push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                     subject="Re: " + data["subject"], body=reply, ticket_id=effective_id, meta="")
                        save_to_history = False

                if reply is None:
                    if not features["feature_ticket_creation"]:
                        logger.info("⏸ feature_ticket_creation disabled — holding for manual review")
                        execution_steps.append("Held_For_Manual_Review_No_Ticket")
                        status = "pending_manual_review"
                        reply = None
                        push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                     subject=data["subject"], body=data["body"], ticket_id="")
                        push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                     subject="Re: " + data["subject"],
                                     body="[Held for manual review — no automated reply sent]",
                                     ticket_id="", meta="")
                        save_to_history = False
                    else:
                        logger.info("🎫 PATH C / verification_failed — extracting issue and creating ticket")
                        execution_steps.append("Ticket_Escalation")

                        clean_description = extract_issue_description(data["body"], history)
                        enriched_data = {**data, "body": clean_description, "subject": clean_description[:80]}
                        reply, outgoing_ticket_id, status = _create_ticket_and_reply(
                            enriched_data, client_id, context="", history=history,
                            cursor=cursor, sentiment=sentiment, priority=priority
                        )

                    if status == "ticket_creation_failed":
                        logger.error("❌ Ticket creation failed — holding for manual review")
                        execution_steps.append("Ticket_Creation_Failed")
                        status = "pending_manual_review"
                        push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                     subject=data["subject"], body=data["body"], ticket_id="")
                        push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                     subject="Re: " + data["subject"],
                                     body="[Ticket creation failed — held for manual review]",
                                     ticket_id="", meta="")
                        save_to_history = False
                    else:
                        push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                     subject=data["subject"], body=data["body"], ticket_id="")
                        push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                     subject="Re: " + data["subject"], body=reply,
                                     ticket_id=outgoing_ticket_id or "", meta="")
                        save_to_history = False

                        if outgoing_ticket_id:
                            summary = generate_summary_llm(context="", customer_body=data["body"],
                                                            history=history, old_summary="")
                            upsert_ticket_history(client_id=client_id, ticket_id=outgoing_ticket_id,
                                                   customer_email=data["from_email"], summary=summary,
                                                   priority="Normal", status="NEW")

            elif state_name == "pending_verification":
                effective_id = ticket_id or pending_state.get("ticket_id", "")
                logger.info(f"🔄 PATH C / pending_verification — re-trying API for {effective_id}")
                execution_steps.append("Order_Check")

                from app.connector_config import run_order_status_lookup
                api_res = run_order_status_lookup(
                    client_id=client_id,
                    ticket_id=effective_id,
                    body=data["body"],
                    history=history,
                    subject=data["subject"],
                    from_email=data["from_email"],
                    sentiment=sentiment,
                    priority=priority,
                    intent=intent,
                )

                if api_res.get("success"):
                    from app.connector_executor import format_mapped_data_for_prompt
                    ticket_info = api_res.get("data", {})
                    context = format_mapped_data_for_prompt(ticket_info)

                    reply = generate_reply_llm(
                        context, data["body"], "crm_support_agent",
                        data["from_email"], history=history
                    )
                    score = llm_score(reply, data["body"])

                    status, sent_ok = _dispatch_or_draft_reply(
                        client_id=client_id,
                        from_email=data["from_email"],
                        subject=data["subject"],
                        reply_body=reply,
                        features=features,
                        confidence_score=score,
                        intent=intent,
                        sentiment=sentiment,
                        priority=ticket_info.get("priority_name", "Normal"),
                        ticket_id=stored_ticket_id,
                        original_body=data["body"],
                        in_reply_to=data.get("message_id"),
                        message_id=data.get("message_id"),
                        execution_steps=execution_steps,
                    )

                    push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                 subject=data["subject"], body=data["body"], ticket_id="")
                    push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                 subject="Re: " + data["subject"], body=reply, ticket_id="", meta="")
                    save_to_history = False

                    old_row = get_ticket_history(stored_ticket_id)
                    old_summary = old_row.get("summary", "") if old_row else ""
                    summary = generate_summary_llm(
                        context=context, customer_body=data["body"],
                        history=history, old_summary=old_summary
                    )
                    upsert_ticket_history(
                        client_id=client_id, ticket_id=stored_ticket_id,
                        customer_email=data["from_email"], summary=summary,
                        priority=ticket_info.get("priority_name", "Normal"),
                        status=ticket_info.get("ticket_status", "NEW")
                    )
                else:
                    logger.warning(
                        f"⚠️ PATH C / pending_verification — API still 404 for {stored_ticket_id}"
                        f" — escalating to verification_failed"
                    )
                    from app.llm import extract_name_from_email
                    customer_name = extract_name_from_email(data["from_email"])

                    reply = _tpl_verification_failed(customer_name)
                    send_email(client_id, data["from_email"], "Re: " + data["subject"], reply)
                    status = "verification_failed"
                    execution_steps.append("Ticket_Escalation")

                    push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                 subject=data["subject"], body=data["body"], ticket_id="")
                    push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                 subject="Re: " + data["subject"], body=reply,
                                 ticket_id="", meta={"state": "verification_failed"})
                    save_to_history = False

            else:
                logger.info("🔍 PATH C — no prior state, scanning history for ticket ID")

                scan_result = scan_history_for_ticket(email_query, history)
                logger.info(f"🔍 History scan result: {scan_result}")

                if scan_result.get("ambiguous"):
                    ambiguous_ids = scan_result.get("ticket_ids", [])
                    logger.warning(f"⚠️ Ambiguous history IDs: {ambiguous_ids}")
                    execution_steps.append("Clarification_Request")

                    id_list = "\n".join(f"  - {tid}" for tid in ambiguous_ids)
                    reply = (
                        f"Dear Customer,\n\n"
                        f"We found multiple previous tickets in your history:\n\n"
                        f"{id_list}\n\n"
                        f"Could you please let us know which one you are referring to?\n\n"
                        f"Thanks & Regards,\nSupport Team"
                    )
                    send_email(client_id, data["from_email"], "Re: " + data["subject"], reply)
                    status = "clarification_sent"
                    save_to_history = True

                elif scan_result.get("found"):
                    history_ticket_id = scan_result.get("ticket_id")
                    logger.info(f"✅ Found ticket ID in history: {history_ticket_id}")
                    execution_steps.append("Order_Check")

                    from app.connector_config import run_order_status_lookup
                    api_res = run_order_status_lookup(
                        client_id=client_id,
                        ticket_id=history_ticket_id,
                        body=data["body"],
                        history=history,
                        subject=data["subject"],
                        from_email=data["from_email"],
                        sentiment=sentiment,
                        priority=priority,
                        intent=intent,
                    )

                    if api_res.get("success"):
                        from app.connector_executor import format_mapped_data_for_prompt
                        ticket_info = api_res.get("data", {})
                        context = format_mapped_data_for_prompt(ticket_info)

                        reply = generate_reply_llm(
                            context, data["body"], "crm_support_agent",
                            data["from_email"], history=history
                        )
                        score = llm_score(reply, data["body"])

                        status, sent_ok = _dispatch_or_draft_reply(
                            client_id=client_id,
                            from_email=data["from_email"],
                            subject=data["subject"],
                            reply_body=reply,
                            features=features,
                            confidence_score=score,
                            intent=intent,
                            sentiment=sentiment,
                            priority=ticket_info.get("priority_name", "Normal"),
                            ticket_id=history_ticket_id,
                            original_body=data["body"],
                            in_reply_to=data.get("message_id"),
                            message_id=data.get("message_id"),
                            execution_steps=execution_steps,
                        )
                        save_to_history = sent_ok

                        old_row = get_ticket_history(history_ticket_id)
                        old_summary = old_row.get("summary", "") if old_row else ""
                        summary = generate_summary_llm(
                            context=context, customer_body=data["body"],
                            history=history, old_summary=old_summary
                        )
                        upsert_ticket_history(
                            client_id=client_id, ticket_id=history_ticket_id,
                            customer_email=data["from_email"], summary=summary,
                            priority=ticket_info.get("priority_name", "Normal"),
                            status=ticket_info.get("ticket_status", "NEW")
                        )
                    else:
                        logger.warning(
                            f"⚠️ API 404 for history ticket {history_ticket_id}"
                            f" — starting pending_verification flow"
                        )
                        from app.llm import extract_name_from_email
                        customer_name = extract_name_from_email(data["from_email"])

                        reply = _tpl_please_verify(history_ticket_id, customer_name)
                        send_email(client_id, data["from_email"], "Re: " + data["subject"], reply)
                        status = "pending_verification"
                        execution_steps.append("Ticket_Escalation")

                        push_message(client_id=client_id, from_email=data["from_email"], role="customer",
                                     subject=data["subject"], body=data["body"], ticket_id="")
                        push_message(client_id=client_id, from_email=data["from_email"], role="support",
                                     subject="Re: " + data["subject"], body=reply, ticket_id="",
                                     meta={"state": "pending_verification", "ticket_id": history_ticket_id})
                        save_to_history = False

                else:
                    if not features["feature_ticket_creation"]:
                        logger.info("⏸ feature_ticket_creation disabled — holding in manual queue")
                        execution_steps.append("Held_For_Manual_Review_No_Ticket")
                        status = "pending_manual_review"
                        reply = None
                        save_to_history = False
                    else:
                        logger.info("🎫 PATH C — no history ID found, creating ticket")
                        execution_steps.append("Ticket_Escalation")
                        reply, outgoing_ticket_id, status = _create_ticket_and_reply(
                            data, client_id, context="", history=history,
                            cursor=cursor, sentiment=sentiment, priority=priority,
                            features=features
                        )

                        if status == "ticket_creation_failed":
                            logger.error("❌ Ticket creation failed — holding for manual review")
                            execution_steps.append("Ticket_Creation_Failed")
                            status = "pending_manual_review"
                            save_to_history = False
                        else:
                            save_to_history = (status == "ticket_created_and_sent")
                            if outgoing_ticket_id:
                                summary = generate_summary_llm(context="", customer_body=data["body"],
                                                                history=history, old_summary="")
                                upsert_ticket_history(client_id=client_id, ticket_id=outgoing_ticket_id,
                                                       customer_email=data["from_email"], summary=summary,
                                                       priority=priority, status="NEW")
        # ==============================
        # Save to history if needed
        # ==============================
        if save_to_history:
            push_message(
                client_id=client_id,
                from_email=data["from_email"],
                role="customer",
                subject=data["subject"],
                body=data["body"],
                ticket_id=""
            )
            push_message(
                client_id=client_id,
                from_email=data["from_email"],
                role="support",
                subject="Re: " + data["subject"],
                body=reply,
                ticket_id=outgoing_ticket_id or ""
            )
            logger.info("💬 Conversation saved to history")
        else:
            logger.info("⏭ History save skipped (RAG success or already pushed)")

        # ==============================
        # Save Logs
        # ==============================
        cursor.execute("""
            INSERT INTO email_logs (client_id, from_email, subject, body, body_html, reply, score, status, rag_id, sentiment, priority, execution_steps)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            client_id,
            data["from_email"],
            data["subject"],
            body_text,
            body_html or None,
            reply,
            score,
            status,
            rag_id,
            sentiment,
            priority,
            json.dumps(execution_steps)
        ))
        db_log_id = cursor.lastrowid
        generate_and_save_summary(db, cursor, db_log_id, {**data, "body": body_text}, chroma_context)

        cursor.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s",(task_id,))
        db.commit()
        db.close()
        db = None
        logger.info("✅ Task completed successfully")

        # Publish real-time notification to Redis
        # try:
        #     import os
        #     import redis
        #     redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0")
        #     if not redis_url:
        #         redis_url = "redis://localhost:6379/0"
        #     r = redis.from_url(redis_url)
        #     try:
        #         r.publish("email_updates", json.dumps({"type": "NEW_EMAIL", "client_id": client_id}))
        #         logger.info("📡 Published real-time update to 'email_updates' channel")
        #     finally:
        #         r.close()
        # except Exception as pub_err:
        #     logger.warning(f"⚠️ Failed to publish real-time notification: {pub_err}")

    except Exception as e:
        logger.error(f"❌ Task failed: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=10)
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass
    publish_email_update(client_id)


# ==============================
# Helper: create ticket + reply
# ==============================
def _create_ticket_and_reply(data, client_id, context, history, cursor, sentiment="Neutral", priority="Medium", features=None):
    """
    Creates a ticket via API, generates formatted reply, dispatches email or saves to draft.
    Returns (reply, ticket_id, status)
    """
    if features is None:
        features = {"feature_auto_send": True}

    from app.llm import extract_name_from_email
    personal_details = {"name": extract_name_from_email(data["from_email"])}

    from app.connector_config import run_ticket_create
    resp = run_ticket_create(
        client_id=client_id,
        from_email=data["from_email"],
        subject=data["subject"],
        body=data["body"],
        history=history,
        sentiment=sentiment,
        priority=priority,
    )
    logger.info(f"🎫 Ticket response: {resp}")

    outgoing_ticket_id = resp.get("ticket_id") if resp else None

    if not outgoing_ticket_id:
        logger.error(f"❌ ticket_creation_failed — connector returned no ticket_id. resp={resp}")
        return None, None, "ticket_creation_failed"

    ticket_status   = resp.get("status",   "NEW")
    ticket_priority = resp.get("priority", priority)
    ticket_issue    = data.get("subject",  "N/A")
    ticket_remarks  = resp.get("remarks",  "")

    try:

        cursor.execute("SELECT COUNT(*) FROM ticket_record WHERE ticket_id = %s", (outgoing_ticket_id,))
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO ticket_record (ticket_id, client_id, mail_id, subject, body, status, sentiment, priority)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                outgoing_ticket_id,
                client_id,
                data.get("mail_id", f"msg-{outgoing_ticket_id}"),
                data["subject"],
                data["body"],
                "Ticket_Generated",
                sentiment,
                priority
            ))
            logger.info(f"✅ Ticket saved to ticket_record: {outgoing_ticket_id}")
    except Exception as t_err:
        logger.warning(f"⚠️ Failed to save ticket_record: {t_err}")

    ticket_context = (
        f"Ticket ID: {outgoing_ticket_id}\n"
        f"Status: {ticket_status}\n"
        f"Priority: {ticket_priority}\n"
        f"Issue: {ticket_issue}\n"
        f"Remarks: {ticket_remarks}"
    )

    reply = generate_reply_llm(
        context=ticket_context,
        query=data["body"],
        agent_type="ecommerce_support_agent",
        from_email=data["from_email"],
        is_ticket=True,
        ticket_id=outgoing_ticket_id,
        history=history
    )

    status_code, sent_ok = _dispatch_or_draft_reply(
        client_id=client_id,
        from_email=data["from_email"],
        subject=f"Ticket Update: {outgoing_ticket_id}",
        reply_body=reply,
        features=features,
        confidence_score=90,
        intent="ticket_created",
        sentiment=sentiment,
        priority=priority,
        ticket_id=outgoing_ticket_id,
        original_body=data["body"],
        in_reply_to=data.get("message_id"),
        message_id=data.get("message_id"),
    )

    final_status = "ticket_created_and_sent" if status_code == "sent" else "ticket_created_draft_pending"
    return reply, outgoing_ticket_id, final_status


