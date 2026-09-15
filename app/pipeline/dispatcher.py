import logging
import re
from typing import Tuple, Optional, Dict, Any
from app.mailer import send_email
from app.action_outbox import execute_idempotent_action
from app.llm import generate_reply_llm, extract_name_from_email
from app.connector_config import run_ticket_create

logger = logging.getLogger(__name__)


def dispatch_or_draft_reply(
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
) -> Tuple[str, bool]:
    """
    Dispatches reply via SMTP if auto-send is enabled (with idempotency protection),
    or creates a draft in draft_emails if in Draft Mode.
    Returns (status_str, save_history_bool).
    """
    if features.get("feature_auto_send", True):
        logger.info(f"📤 [Client {client_id}] Auto-send enabled — dispatching reply via SMTP")
        clean_subj = (subject or "").strip()
        out_subject = clean_subj if clean_subj.lower().startswith("re:") else f"Re: {clean_subj}"

        # Execute idempotent email send
        def _send():
            return send_email(client_id, from_email, out_subject, reply_body, in_reply_to=in_reply_to)

        outbox_key_seed = message_id or f"{client_id}:{from_email}:{out_subject}"
        res = execute_idempotent_action(
            client_id=client_id,
            message_id=outbox_key_seed,
            action_type="smtp_send_reply",
            action_fn=_send
        )

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


def create_ticket_and_reply(
    data: Dict[str, Any],
    client_id: str,
    context: str,
    history: list,
    cursor: Optional[Any] = None,
    sentiment: str = "Neutral",
    priority: str = "Medium",
    features: Optional[dict] = None
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Creates a ticket via external CRM connector (guaranteed at-most-once by action outbox),
    saves to ticket_record, generates confirmation reply, and dispatches via SMTP / draft.
    Returns (reply, ticket_id, status)
    """
    if features is None:
        features = {"feature_auto_send": True}

    from app.utils import normalize_subject
    fallback_text = (data.get("body") or context or "").strip()
    effective_subject = normalize_subject(data.get("subject"), fallback_text)

    message_ref = data.get("message_id") or data.get("mail_id") or f"{client_id}:{data['from_email']}:{effective_subject}"

    # 1. Execute idempotent CRM Ticket Creation
    def _do_create():
        return run_ticket_create(
            client_id=client_id,
            from_email=data["from_email"],
            subject=effective_subject,
            body=data["body"],
            history=history,
            sentiment=sentiment,
            priority=priority,
        )

    outbox_res = execute_idempotent_action(
        client_id=client_id,
        message_id=message_ref,
        action_type="crm_ticket_create",
        action_fn=_do_create,
        extract_ref_fn=lambda r: r.get("ticket_id") if isinstance(r, dict) else None
    )

    if not outbox_res["success"] and not outbox_res["already_completed"]:
        logger.error(f"❌ ticket_creation_failed — outbox action failed: {outbox_res.get('error')}")
        return None, None, "ticket_creation_failed"

    resp = outbox_res.get("result") or {}
    outgoing_ticket_id = outbox_res.get("external_ref") or resp.get("ticket_id")

    if not outgoing_ticket_id:
        logger.error(f"❌ ticket_creation_failed — no ticket_id returned. outbox_res={outbox_res}")
        return None, None, "ticket_creation_failed"

    ticket_status = resp.get("status", "NEW") if isinstance(resp, dict) else "NEW"
    ticket_priority = resp.get("priority", priority) if isinstance(resp, dict) else priority
    ticket_issue = data.get("subject", "N/A")
    ticket_remarks = resp.get("remarks", "") if isinstance(resp, dict) else ""

    # 2. Record to local ticket_record table
    def _insert_ticket_record(cur):
        cur.execute("SELECT COUNT(*) FROM ticket_record WHERE ticket_id = %s", (outgoing_ticket_id,))
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO ticket_record (ticket_id, client_id, mail_id, subject, body, status, sentiment, priority)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                outgoing_ticket_id,
                client_id,
                data.get("mail_id") or data.get("message_id") or f"msg-{outgoing_ticket_id}",
                data["subject"],
                data["body"],
                "Ticket_Generated",
                sentiment,
                priority
            ))
            logger.info(f"✅ Ticket saved to ticket_record: {outgoing_ticket_id}")

    try:
        if cursor is not None:
            _insert_ticket_record(cursor)
        else:
            from app.db import get_db_ctx
            with get_db_ctx() as db:
                with db.cursor() as cur:
                    _insert_ticket_record(cur)
                    db.commit()
    except Exception as t_err:
        logger.warning(f"⚠️ Failed to save ticket_record: {t_err}")

    # 3. Draft ticket confirmation reply
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

    # 4. Dispatch reply or create draft
    status_code, sent_ok = dispatch_or_draft_reply(
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
