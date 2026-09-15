import logging
import json
import re
import os
import random
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from worker.celery_worker import celery
from app.db import get_db_ctx
from app.llm import current_client_id, extract_ticket_and_order_ids, generate_summary_llm
from app.text_cleaning import strip_quoted_reply, strip_disclaimers, extract_clean_text_from_html, is_html_content
from app.email_disclaimers import get_active_disclaimer_texts
from app.mailer import send_email
from app.chat_history import push_message, get_history, upsert_ticket_history
from app.pipeline.context import PipelineContext
from app.pipeline.filters import apply_deterministic_filters
from app.pipeline.agent import run_support_agent
from app.pipeline.dispatcher import dispatch_or_draft_reply, create_ticket_and_reply

logger = logging.getLogger(__name__)


def extract_order_id(text: str):
    return extract_ticket_and_order_ids(text)


def generate_ticket_id() -> str:
    date_part = datetime.now().strftime("%y%m%d")
    random_part = str(random.randint(0, 99999)).zfill(5)
    return f"T-{date_part}-{random_part}"


def resolve_thread_id(client_id: str, from_email: str, subject: str, in_reply_to: str = "", references: str = "", cursor=None) -> tuple[str, int]:
    """
    Resolves or initiates a conversation thread and returns (thread_id, prior_step).
    1. Matches via in_reply_to against message_id in email_logs.
    2. Matches via references against message_id.
    3. Fallback: Matches same sender + normalized subject within 7 days on active threads.
    4. Default: Generates new thread_id 'th_' + uuid4().hex[:12].
    """
    def _do_lookup(cur):
        # 1. Match via in_reply_to
        if in_reply_to and in_reply_to.strip():
            cur.execute("""
                SELECT thread_id, troubleshooting_step 
                FROM email_logs 
                WHERE client_id = %s AND message_id = %s AND thread_id IS NOT NULL 
                ORDER BY id DESC LIMIT 1
            """, (client_id, in_reply_to.strip()))
            row = cur.fetchone()
            if row and row[0]:
                return row[0], int(row[1] or 0)

        # 2. Match via references
        if references and references.strip():
            ref_tokens = re.findall(r'<[^>]+>', references)
            for ref in ref_tokens:
                cur.execute("""
                    SELECT thread_id, troubleshooting_step 
                    FROM email_logs 
                    WHERE client_id = %s AND message_id = %s AND thread_id IS NOT NULL 
                    ORDER BY id DESC LIMIT 1
                """, (client_id, ref))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0], int(row[1] or 0)

        # 3. Subject-based thread matching within 7 days
        # Only apply if the incoming email is an explicit reply/forward (e.g. Re: / Fwd:)
        # and has a descriptive, non-generic subject. Freshly composed emails must start a new thread.
        is_reply_subject = bool(re.match(r'^(re|fwd|fw):\s*', subject or '', flags=re.IGNORECASE))
        clean_subj = re.sub(r'^(re|fwd|fw):\s*', '', subject or '', flags=re.IGNORECASE).strip()
        GENERIC_SUBJECTS = {
            "support request", "no subject", "(no subject)", "facing problem",
            "problem", "issue", "help", "query", "question", "urgent",
            "support", "error", "trouble", "assistance", "request", "bug"
        }
        if is_reply_subject and clean_subj and clean_subj.lower() not in GENERIC_SUBJECTS:
            cur.execute("""
                SELECT thread_id, troubleshooting_step 
                FROM email_logs 
                WHERE client_id = %s AND from_email = %s 
                  AND subject LIKE %s
                  AND created_at >= NOW() - INTERVAL 7 DAY
                  AND is_resolved = 0
                  AND thread_id IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """, (client_id, from_email, f"%{clean_subj[:50]}%"))
            row = cur.fetchone()
            if row and row[0]:
                return row[0], int(row[1] or 0)

        return None, 0

    thread_id = None
    prior_step = 0
    try:
        if cursor is not None:
            thread_id, prior_step = _do_lookup(cursor)
        else:
            with get_db_ctx() as db:
                with db.cursor() as cur:
                    thread_id, prior_step = _do_lookup(cur)
    except Exception as e:
        logger.warning(f"⚠️ Thread resolution failed: {e}")

    if not thread_id:
        thread_id = f"th_{uuid.uuid4().hex[:12]}"

    return thread_id, prior_step


def get_client_features(cursor=None, client_id: str = "") -> Dict[str, Any]:
    defaults = {
        "feature_ticket_creation": True, "feature_auto_send": True,
        "feature_rag": True, "feature_order_tracking": True, "feature_manual_reply": True,
        "feature_strip_disclaimers": True,
        "admin_bot_enabled": True, "client_bot_enabled": True,
    }
    def _query(cur):
        cur.execute("""
            SELECT feature_ticket_creation, feature_auto_send, feature_rag,
                   feature_order_tracking, feature_manual_reply,
                   COALESCE(feature_strip_disclaimers, 1),
                   COALESCE(admin_bot_enabled, 1), COALESCE(client_bot_enabled, 1)
            FROM email_accounts WHERE client_id = %s
        """, (client_id,))
        row = cur.fetchone()
        if row:
            return {
                "feature_ticket_creation": bool(row[0]), "feature_auto_send": bool(row[1]),
                "feature_rag": bool(row[2]), "feature_order_tracking": bool(row[3]),
                "feature_manual_reply": bool(row[4]),
                "feature_strip_disclaimers": bool(row[5]) if row[5] is not None else True,
                "admin_bot_enabled": bool(row[6]) if row[6] is not None else True,
                "client_bot_enabled": bool(row[7]) if row[7] is not None else True,
            }
        return defaults

    try:
        if cursor is not None:
            return _query(cursor)
        else:
            with get_db_ctx() as db:
                with db.cursor() as cur:
                    return _query(cur)
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch client features for {client_id}, using defaults: {e}")
    return defaults


def publish_email_update(client_id: str):
    try:
        from app.redis_pool import get_redis_main
        r = get_redis_main()
        r.publish("email_updates", json.dumps({"type": "NEW_EMAIL", "client_id": client_id}))
        logger.info(f"📡 Published real-time update to 'email_updates' channel for client {client_id}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to publish real-time notification: {e}")


def generate_and_save_summary(
    log_id: int, 
    data: Dict[str, Any], 
    context_text: str = "", 
    db=None, 
    cursor=None
):
    """
    Generates an LLM summary of the issue without holding open a long-lived database connection.
    1. Short DB read: Prior 5 emails from same sender.
    2. LLM inference: generate_summary_llm() with ZERO DB connection held.
    3. Short DB write: UPDATE email_logs SET summary = %s.
    """
    try:
        client_id = data.get("client_id")
        from_email = data.get("from_email")
        body = data.get("body")

        # Step 1: Read prior rows in discrete transaction
        def _read_prior(cur):
            cur.execute("""
                SELECT body, reply, summary 
                FROM email_logs 
                WHERE client_id = %s 
                  AND from_email = %s 
                  AND id < %s
                ORDER BY id DESC LIMIT 5
            """, (client_id, from_email, log_id))
            return cur.fetchall()

        if cursor is not None:
            prior_rows = _read_prior(cursor)
        else:
            with get_db_ctx() as db_conn:
                with db_conn.cursor() as cur:
                    prior_rows = _read_prior(cur)

        old_summary = ""
        history_list = []
        for r in prior_rows:
            if r[2] and not old_summary:
                old_summary = r[2]
            history_list.insert(0, {"role": "customer", "body": r[0]})
            if r[1]:
                history_list.insert(1, {"role": "support", "body": r[1]})

        # Step 2: LLM inference with ZERO database connection held
        summary = generate_summary_llm(
            context=context_text,
            customer_body=body,
            history=history_list,
            old_summary=old_summary
        )

        # Step 3: Write summary in discrete transaction
        def _write_summary(cur, conn):
            cur.execute("UPDATE email_logs SET summary = %s WHERE id = %s", (summary, log_id))
            conn.commit()

        if cursor is not None and db is not None:
            _write_summary(cursor, db)
        else:
            with get_db_ctx() as db_conn:
                with db_conn.cursor() as cur:
                    _write_summary(cur, db_conn)

        logger.info(f"📊 Summary generated & updated for ID {log_id}: {summary}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to generate/save summary: {e}")


def _finalize_task_and_log(ctx: PipelineContext, task_id: str, cursor=None, db=None):
    """
    Standardized persistence helper:
    1. Inserts execution metadata and response into email_logs in a short discrete transaction.
    2. Auto-populates draft_emails if pending manual review.
    3. Updates celery_task_log to 'completed'.
    4. Commits DB transaction immediately.
    5. Asynchronously generates summary (if applicable).
    6. Publishes real-time notification to Redis.
    """
    def _execute_persistence(cur, conn):
        cur.execute("""
            INSERT INTO email_logs (
                client_id, from_email, subject, body, body_html, reply, score,
                status, rag_id, sentiment, priority, execution_steps, summary,
                message_id, in_reply_to, thread_id, troubleshooting_step, is_resolved
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            ctx.client_id,
            ctx.from_email,
            ctx.subject,
            ctx.body,
            ctx.body_html or None,
            ctx.draft_reply,
            ctx.score,
            ctx.status,
            ctx.rag_id,
            ctx.sentiment,
            ctx.priority,
            json.dumps(ctx.execution_steps),
            ctx.summary or "",
            ctx.message_id or None,
            ctx.in_reply_to or None,
            ctx.thread_id or None,
            ctx.troubleshooting_step or 0,
            1 if ctx.is_resolved else 0
        ))
        log_id = cur.lastrowid

        # Auto-populate Pending Review in draft_emails if manual review is required
        if ctx.status == "pending_manual_review":
            try:
                from app.draft_service import create_draft
                create_draft(
                    client_id=ctx.client_id,
                    email_log_id=log_id,
                    from_email=ctx.from_email,
                    to_email=ctx.from_email,
                    subject=ctx.subject,
                    original_body=ctx.body,
                    draft_reply=ctx.draft_reply or (
                        f"Hello,\n\n"
                        f"Thank you for reaching out regarding '{ctx.subject}'.\n"
                        f"Your inquiry has been received and escalated for specialist assistance. A support engineer will update you shortly.\n\n"
                        f"Best regards,\nSupport Team"
                    ),
                    confidence_score=ctx.score,
                    intent=ctx.intent or "ticket_creation_failed",
                    sentiment=ctx.sentiment,
                    priority=ctx.priority,
                    ticket_id=ctx.ticket_id,
                    in_reply_to=ctx.message_id,
                    message_id=ctx.message_id,
                    sender_name=ctx.sender_name,
                )
                logger.info(f"📝 Auto-created Pending Review draft in draft_emails for log_id {log_id}")
            except Exception as draft_err:
                logger.error(f"⚠️ Failed to create draft for pending manual review: {draft_err}")

        cur.execute("UPDATE celery_task_log SET status = 'completed' WHERE task_id = %s", (task_id,))
        conn.commit()
        return log_id

    db_log_id = None
    if cursor is not None and db is not None:
        db_log_id = _execute_persistence(cursor, db)
    else:
        with get_db_ctx() as db_conn:
            with db_conn.cursor() as cur:
                db_log_id = _execute_persistence(cur, db_conn)

    # Post-commit summary generation (runs outside main transaction without holding connection during LLM inference)
    if db_log_id and not ctx.summary and ctx.status not in ("system_bounce_dropped", "rate_limited", "automation_halted", "no_action_needed"):
        try:
            generate_and_save_summary(
                log_id=db_log_id,
                data={
                    "client_id": ctx.client_id,
                    "from_email": ctx.from_email,
                    "subject": ctx.subject,
                    "body": ctx.body
                },
                context_text=ctx.context_text
            )
        except Exception as sum_err:
            logger.warning(f"⚠️ Summary generation skipped: {sum_err}")

    publish_email_update(ctx.client_id)
    logger.info(f"✅ [Task {task_id}] Finalized with status='{ctx.status}', log_id={db_log_id}")


# Backward-compatible aliases
_create_ticket_and_reply = create_ticket_and_reply
_dispatch_or_draft_reply = dispatch_or_draft_reply


@celery.task(bind=True, max_retries=2, default_retry_delay=10)
def process_email_task(self, data: Dict[str, Any]):
    task_id = self.request.id or str(uuid.uuid4())
    client_id = data.get("client_id", "SYSTEM") or "SYSTEM"
    from_email = (data.get("from_email") or "").strip()
    logger.info(f"📥 Received email task [{task_id}]: from={from_email} subject={data.get('subject')}")

    # Set context token for tenant isolation
    ctx_token = current_client_id.set(client_id)
    ctx = PipelineContext.from_task_data(task_id, data)

    try:
        # ====================================================================
        # Phase A: Short Pre-Flight Gate (~10ms DB checkout)
        # ====================================================================
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # 1. Idempotency Check
                cursor.execute("SELECT status FROM celery_task_log WHERE task_id = %s", (task_id,))
                existing = cursor.fetchone()
                if existing:
                    if existing[0] == "completed":
                        logger.info(f"⏭ Task {task_id} already completed — skipping to prevent duplicate")
                        return
                    elif existing[0] == "processing":
                        logger.warning(f"🔄 Task {task_id} is a retry — continuing carefully")
                else:
                    cursor.execute(
                        "INSERT INTO celery_task_log (task_id, client_id, from_email, status) VALUES (%s, %s, %s, 'processing')",
                        (task_id, client_id, from_email)
                    )
                    db.commit()

                if data.get("__test_fatal_error__"):
                    raise RuntimeError("Deliberate poison pill test error")

                features = get_client_features(cursor, client_id)
                ctx.features = features

                # Resolve Thread ID and prior troubleshooting step
                thread_id, prior_step = resolve_thread_id(
                    client_id=client_id,
                    from_email=from_email,
                    subject=data.get("subject") or "",
                    in_reply_to=data.get("in_reply_to") or "",
                    references=data.get("references") or "",
                    cursor=cursor
                )
                ctx.thread_id = thread_id
                ctx.troubleshooting_step = prior_step
                logger.info(f"🧵 [Thread Resolved] ID: {thread_id} | Prior Step: {prior_step}")

                # Level 0 Fast Deterministic Gates
                if apply_deterministic_filters(ctx, cursor):
                    _finalize_task_and_log(ctx, task_id=task_id, cursor=cursor, db=db)
                    return

        # ====================================================================
        # Phase B: Network, AI & Tools Execution (ZERO DB connection held)
        # ====================================================================
        # Normalize subject: derive from first line of body if blank
        from app.utils import normalize_subject
        ctx.subject = normalize_subject(ctx.subject, ctx.body)
        logger.info(f"🏷️ Subject normalized to: '{ctx.subject}'")
        data["subject"] = ctx.subject

        # Strip disclaimers if enabled
        if ctx.features.get("feature_strip_disclaimers", True):
            try:
                active_disclaimers = get_active_disclaimer_texts(client_id)
                ctx.body = strip_disclaimers(ctx.body, active_disclaimers)
            except Exception as e:
                logger.warning(f"⚠️ Failed to strip disclaimers: {e}")

        # Deterministic Multi-Ticket Check
        email_query = f"Subject: {ctx.subject}\n\n{ctx.body}"
        ticket_ids = extract_ticket_and_order_ids(email_query)
        ticket_ids = list(dict.fromkeys(ticket_ids))

        if len(ticket_ids) > 1:
            logger.warning(f"⚠️ Multiple ticket IDs in email: {ticket_ids}")
            ctx.log_step("Clarification_Request")
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
                ctx.client_id,
                ctx.from_email,
                "Re: " + ctx.subject,
                clarification_reply,
                in_reply_to=ctx.message_id
            )
            ctx.draft_reply = clarification_reply
            ctx.status = "clarification_sent"
            ctx.summary = f"Multiple ticket IDs referenced ({', '.join(ticket_ids)}). Sent clarification request."
            ctx.score = 0
            _finalize_task_and_log(ctx, task_id=task_id)
            return

        if ticket_ids:
            ctx.ticket_id = ticket_ids[0]

        # Load Conversation History from Redis (with SQL fallback)
        ctx.history = get_history(client_id, ctx.from_email, last_n=10, thread_id=ctx.thread_id)
        logger.info(f"📜 Loaded {len(ctx.history)} history messages (thread={ctx.thread_id}, step={ctx.troubleshooting_step})")

        # Autonomous Support Agent Loop (No DB held across 10-30s LLM / Tool execution)
        ctx = run_support_agent(ctx, cursor=None)

        # Post-Agent Dispatch & Outbox
        if ctx.status in ("ticket_created_and_sent", "ticket_created_draft_pending"):
            logger.info(f"🎫 Ticket escalation finalized by agent tool ({ctx.status})")
        elif ctx.response_action == "create_ticket" and not ctx.is_resolved:
            logger.info("🎫 Evaluator decided to escalate and create ticket")
            reply, outgoing_ticket_id, status = create_ticket_and_reply(
                data=ctx.to_task_data(),
                client_id=client_id,
                context=ctx.draft_reply or ctx.body,
                history=ctx.history,
                cursor=None,
                sentiment=ctx.sentiment,
                priority=ctx.priority,
                features=ctx.features
            )
            ctx.draft_reply = reply
            ctx.ticket_id = outgoing_ticket_id
            ctx.status = status
            if status == "ticket_creation_failed":
                logger.error("❌ Ticket creation failed — holding for manual review")
                ctx.log_step("Ticket_Creation_Failed")
                ctx.status = "pending_manual_review"
            else:
                try:
                    summary = generate_summary_llm(context="", customer_body=ctx.body, history=ctx.history, old_summary="")
                    upsert_ticket_history(client_id=client_id, ticket_id=outgoing_ticket_id,
                                          customer_email=ctx.from_email, summary=summary,
                                          priority=ctx.priority, status="NEW")
                except Exception as up_err:
                    logger.warning(f"⚠️ Failed to upsert ticket history: {up_err}")
        elif ctx.draft_reply:
            status, save_history = dispatch_or_draft_reply(
                client_id=client_id,
                from_email=ctx.from_email,
                subject=ctx.subject,
                reply_body=ctx.draft_reply,
                features=ctx.features,
                confidence_score=ctx.score,
                intent=ctx.intent or ("issue_resolved" if ctx.is_resolved else "support_query"),
                sentiment=ctx.sentiment,
                priority=ctx.priority,
                ticket_id=ctx.ticket_id,
                original_body=ctx.body,
                in_reply_to=ctx.message_id,
                message_id=ctx.message_id,
                sender_name=ctx.sender_name,
                execution_steps=ctx.execution_steps
            )
            ctx.status = status
            if save_history:
                push_message(client_id=client_id, from_email=ctx.from_email, role="customer",
                             subject=ctx.subject, body=ctx.body, ticket_id=ctx.ticket_id or "", thread_id=ctx.thread_id)
                push_message(client_id=client_id, from_email=ctx.from_email, role="support",
                             subject="Re: " + ctx.subject, body=ctx.draft_reply, ticket_id=ctx.ticket_id or "", thread_id=ctx.thread_id)
        else:
            logger.error("❌ Agent returned empty reply — holding for manual review")
            ctx.status = "pending_manual_review"
            ctx.log_step("Agent_Fallback_Manual_Review")

        # ====================================================================
        # Phase C: Finalize Task & Write Logs (~20ms DB checkout)
        # ====================================================================
        _finalize_task_and_log(ctx, task_id=task_id)

    except Exception as e:
        logger.error(f"❌ Task failed: {e}", exc_info=True)
        retries = getattr(self.request, "retries", 0)
        max_retries = getattr(self, "max_retries", 2)
        if retries >= max_retries:
            logger.critical(
                f"🔥 [Poison Pill Circuit Breaker] Max retries ({max_retries}) exhausted for task {task_id} "
                f"(Client: {client_id}, Sender: {from_email}). Terminating to prevent worker starvation."
            )
            try:
                with get_db_ctx() as db:
                    with db.cursor() as cursor:
                        cursor.execute(
                            "UPDATE celery_task_log SET status = 'fatal_processing_error' WHERE task_id = %s",
                            (task_id,)
                        )
                        error_summary = f"Fatal worker error: {str(e)[:200]}"
                        cursor.execute("""
                            INSERT INTO email_logs (client_id, from_email, subject, body, status, summary)
                            VALUES (%s, %s, %s, %s, 'failed', %s)
                        """, (
                            client_id,
                            from_email,
                            (data.get("subject") or "Support Request")[:255],
                            (data.get("body") or "")[:1000],
                            error_summary
                        ))
                        db.commit()
            except Exception as log_err:
                logger.error(f"⚠️ Failed to write fatal task error to DB: {log_err}")
            return {"status": "fatal_processing_error", "error": str(e)}
        else:
            raise self.retry(exc=e, countdown=10)
    finally:
        publish_email_update(client_id)


@celery.task(name="worker.tasks.sweep_stale_outbox_task")
def sweep_stale_outbox_task(max_age_seconds: int = 120):
    """
    Periodic task to sweep abandoned outbox actions older than max_age_seconds.
    """
    from app.action_outbox import sweep_stale_actions
    logger.info(f"🧹 Running background outbox sweeper (max_age={max_age_seconds}s)...")
    res = sweep_stale_actions(max_age_seconds)
    return res
