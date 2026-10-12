import logging
import json
import os
from typing import Optional, List, Dict, Any
from enum import Enum

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, field_validator

from app.auth_deps import get_current_user, require_client_access, verify_ingestion_auth
from app.rate_limiter import RedisRateLimiter
from app.db import get_db_ctx
from worker.tasks import process_email_task
from app.email_credential import save_email_account, ensure_ticket_record_table
from app.chat_history import get_history, clear_history, push_message
from app.mailer import send_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Emails & Tickets"])


class EmailRequest(BaseModel):
    client_id: str
    from_email: str
    subject: str
    body: str


class AcceptEmailRequest(BaseModel):
    client_id: str
    email: EmailStr
    password: str
    score_threshold: int = 80
    response_tone: str = "Formal"
    agent_type: str = "customer_support"

    @field_validator("password")
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class PauseEmailRequest(BaseModel):
    client_id: str
    email: str


class MarketingSenderRequest(BaseModel):
    client_id: str
    sender_email: str


class PausedEmailHistoryUpdateRequest(BaseModel):
    status: str  # 'ignored' or 'replied'


class ManualReplyRequest(BaseModel):
    client_id: str
    to_email: str
    subject: str
    body: str = ""
    reply_text: str
    blocked_record_id: Optional[int] = None
    paused_history_record_id: Optional[int] = None


class ApprovePendingReplyRequest(BaseModel):
    client_id: str
    log_id: int


@router.post("/process-email", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def process_email(data: EmailRequest, auth_info: dict = Depends(verify_ingestion_auth)):
    if auth_info.get("type") == "session":
        require_client_access(data.client_id, auth_info["user"])
    process_email_task.delay(data.model_dump())
    return {"status": "queued"}


@router.post("/accept-email")
def accept_email(data: AcceptEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        save_email_account(
            data.client_id,
            data.email,
            data.password,
            data.score_threshold,
            data.response_tone,
            data.agent_type
        )
        return {
            "status": "saved",
            "client_id": data.client_id,
            "email": data.email,
            "score_threshold": data.score_threshold,
            "response_tone": data.response_tone,
            "agent_type": data.agent_type
        }
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/emails/{client_id}")
def get_emails_logs_endpoint(
    client_id: str,
    page: Optional[int] = None,
    page_size: Optional[int] = 25,
    search: Optional[str] = None,
    status: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_logs: {ex}")

                # Unpaginated fallback (for Dashboard, Inbox, etc.)
                if page is None:
                    if client_id == "ALL":
                        cursor.execute(f"""
                            SELECT id, from_email, subject, body, reply, score, status, created_at, rag_id, sentiment, priority, execution_steps, summary, body_html, {col} as client_id, thread_id, intent 
                            FROM email_logs 
                            ORDER BY created_at DESC
                        """)
                    else:
                        cursor.execute(f"""
                            SELECT id, from_email, subject, body, reply, score, status, created_at, rag_id, sentiment, priority, execution_steps, summary, body_html, {col} as client_id, thread_id, intent 
                            FROM email_logs 
                            WHERE {col} = %s 
                            ORDER BY created_at DESC
                        """, (client_id,))
                    rows = cursor.fetchall()
                    total_count = len(rows)
                else:
                    # Server-side paginated path (for AiProcessing)
                    conditions = []
                    params = []

                    if client_id != "ALL":
                        conditions.append(f"{col} = %s")
                        params.append(client_id)

                    if status and status != "all":
                        if status == "sent":
                            conditions.append("status = 'sent'")
                        elif status == "ticket":
                            conditions.append("status = 'ticket_created_and_sent'")
                        elif status == "failed":
                            conditions.append("(status LIKE '%failed%' OR status = 'failed')")

                    if search and search.strip():
                        s_clean = search.strip()
                        s_term = f"%{s_clean}%"
                        num_part = s_clean.lstrip("#") if s_clean.startswith("#") else (s_clean if s_clean.isdigit() else None)
                        if num_part and num_part.isdigit():
                            conditions.append("(id = %s OR thread_id LIKE %s OR from_email LIKE %s OR subject LIKE %s OR body LIKE %s OR intent LIKE %s OR sentiment LIKE %s)")
                            params.extend([int(num_part), s_term, s_term, s_term, s_term, s_term, s_term])
                        else:
                            conditions.append("(thread_id LIKE %s OR from_email LIKE %s OR subject LIKE %s OR body LIKE %s OR intent LIKE %s OR sentiment LIKE %s OR CAST(id AS CHAR) LIKE %s)")
                            params.extend([s_term, s_term, s_term, s_term, s_term, s_term, s_term])

                    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""

                    # Count total matching rows
                    count_query = f"SELECT COUNT(*) FROM email_logs{where_sql}"
                    cursor.execute(count_query, tuple(params))
                    total_count = cursor.fetchone()[0]

                    p = max(1, page)
                    ps = max(1, min(100, page_size or 25))
                    offset = (p - 1) * ps

                    query_sql = f"""
                        SELECT id, from_email, subject, body, reply, score, status, created_at, rag_id, sentiment, priority, execution_steps, summary, body_html, {col} as client_id, thread_id, intent 
                        FROM email_logs 
                        {where_sql}
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s
                    """
                    item_params = list(params) + [ps, offset]
                    cursor.execute(query_sql, tuple(item_params))
                    rows = cursor.fetchall()

                emails_list = []
                for r in rows:
                    ui_status = "New"
                    if r[6] in ["sent", "ticket_created_and_sent"]:
                        ui_status = "Replied" if r[6] == "sent" else "Ticket_Generated"
                    elif r[6] in ["failed", "send_failed", "ticket_created_send_failed", "ticket_creation_failed"]:
                        ui_status = "Failed"
                    elif r[6] == "pending":
                        ui_status = "Processing"
                    elif r[6] == "pending_manual_review":
                        ui_status = "Pending Review"
                    elif r[6] in ["no_action_needed", "handled"]:
                        ui_status = "No Action Needed"
                    elif r[6] == "paused":
                        ui_status = "Paused"
                    elif r[6] == "blocked_keyword":
                        ui_status = "Blocked"

                    steps = ["Start"]
                    if len(r) > 11 and r[11]:
                        try:
                            steps = json.loads(r[11])
                        except Exception:
                            pass

                    emails_list.append({
                        "id": r[0],
                        "mailId": f"msg-{r[0]}",
                        "sender": r[1],
                        "subject": r[2],
                        "preview": r[3],
                        "reply": r[4],
                        "confidence": f"{r[5]}%" if r[5] else "90%",
                        "status": ui_status,
                        "category": "Marketing / Promo" if r[6] in ["no_action_needed", "handled"] else "Customer Query",
                        "time": r[7].strftime("%I:%M %p") if r[7] else "Just Now",
                        "date_str": r[7].strftime("%b %d, %Y") if r[7] else "",
                        "raw_status": r[6],
                        "score": r[5] if r[5] is not None else 90,
                        "rag_id": r[8] if len(r) > 8 else None,
                        "sentiment": r[9] if len(r) > 9 and r[9] else "Neutral",
                        "priority": r[10] if len(r) > 10 and r[10] else "Medium",
                        "execution_steps": steps,
                        "summary": r[12] if len(r) > 12 and r[12] else "",
                        "body_html": r[13] if len(r) > 13 and r[13] else None,
                        "client_id": r[14] if len(r) > 14 else None,
                        "thread_id": r[15] if len(r) > 15 and r[15] else None,
                        "intent": r[16] if len(r) > 16 and r[16] else "general_support"
                    })

                if page is None:
                    return emails_list

                # KPI summary metrics for tenant
                metric_where = f"WHERE {col} = %s" if client_id != "ALL" else ""
                metric_params = (client_id,) if client_id != "ALL" else ()
                cursor.execute(f"""
                    SELECT 
                        COUNT(*), 
                        SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END), 
                        SUM(CASE WHEN status = 'ticket_created_and_sent' THEN 1 ELSE 0 END), 
                        AVG(score) 
                    FROM email_logs 
                    {metric_where}
                """, metric_params)
                m_row = cursor.fetchone()
                metrics = {
                    "total": m_row[0] or 0,
                    "auto_sent": int(m_row[1] or 0),
                    "ticketed": int(m_row[2] or 0),
                    "avg_score": round(float(m_row[3] or 0)) if m_row[3] is not None else 0
                }

                ps = max(1, min(100, page_size or 25))
                p = max(1, page)
                return {
                    "items": emails_list,
                    "total": total_count,
                    "page": p,
                    "page_size": ps,
                    "total_pages": max(1, (total_count + ps - 1) // ps),
                    "metrics": metrics
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch emails logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets/{client_id}")
def get_tickets_logs_endpoint(
    client_id: str,
    page: Optional[int] = None,
    page_size: Optional[int] = 25,
    search: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    intent: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                try:
                    ensure_ticket_record_table(cursor)
                except Exception as tbl_ex:
                    logger.warning(f"⚠️ Could not ensure ticket_record table: {tbl_ex}")

                col = "client_id"
                try:
                    cursor.execute("DESCRIBE ticket_record")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe ticket_record: {ex}")

                from_and_join = """
                    FROM ticket_record t
                    LEFT JOIN email_logs e ON e.id = (
                        SELECT e2.id 
                        FROM email_logs e2 
                        WHERE (e2.message_id = t.mail_id AND t.mail_id IS NOT NULL AND t.mail_id != '')
                           OR (t.thread_id IS NOT NULL AND t.thread_id != '' AND e2.thread_id = t.thread_id)
                           OR (t.mail_id = CONCAT('msg-', e2.id))
                           OR (t.ticket_id = CAST(e2.id AS CHAR))
                        ORDER BY e2.id DESC
                        LIMIT 1
                    )
                """

                base_query = f"""
                    SELECT 
                        t.ticket_id, 
                        t.mail_id, 
                        t.subject, 
                        t.body, 
                        t.status, 
                        t.created_at, 
                        t.sentiment, 
                        t.priority,
                        COALESCE(NULLIF(t.intent, ''), e.intent, 'general_support') as intent,
                        COALESCE(NULLIF(e.from_email, ''), '') as customer_email,
                        e.id as email_log_id,
                        COALESCE(t.thread_id, e.thread_id) as thread_id
                    {from_and_join}
                """

                # Unpaginated fallback (for existing callers)
                if page is None:
                    if client_id == "ALL":
                        cursor.execute(f"{base_query} ORDER BY t.created_at DESC")
                    else:
                        cursor.execute(f"{base_query} WHERE t.{col} = %s ORDER BY t.created_at DESC", (client_id,))
                    rows = cursor.fetchall()
                    total_count = len(rows)
                else:
                    # Paginated and filtered path
                    conditions = []
                    params = []

                    if client_id != "ALL":
                        conditions.append(f"t.{col} = %s")
                        params.append(client_id)

                    if status and status != "all":
                        conditions.append("t.status = %s")
                        params.append(status)

                    if priority and priority != "all":
                        conditions.append("t.priority = %s")
                        params.append(priority)

                    if intent and intent != "all":
                        conditions.append("(t.intent = %s OR e.intent = %s)")
                        params.extend([intent, intent])

                    if search and search.strip():
                        s_clean = search.strip()
                        s_term = f"%{s_clean}%"
                        num_part = s_clean.lstrip("#") if s_clean.startswith("#") else (s_clean if s_clean.isdigit() else None)
                        if num_part and num_part.isdigit():
                            conditions.append("(t.ticket_id LIKE %s OR e.id = %s OR t.subject LIKE %s OR t.body LIKE %s OR e.from_email LIKE %s OR COALESCE(t.thread_id, e.thread_id) LIKE %s OR t.mail_id LIKE %s)")
                            params.extend([s_term, int(num_part), s_term, s_term, s_term, s_term, s_term])
                        else:
                            conditions.append("(t.ticket_id LIKE %s OR t.subject LIKE %s OR t.body LIKE %s OR e.from_email LIKE %s OR COALESCE(t.thread_id, e.thread_id) LIKE %s OR t.mail_id LIKE %s OR t.intent LIKE %s OR e.intent LIKE %s)")
                            params.extend([s_term, s_term, s_term, s_term, s_term, s_term, s_term, s_term])

                    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""

                    # Count total records
                    count_sql = f"SELECT COUNT(*) {from_and_join} {where_sql}"
                    cursor.execute(count_sql, tuple(params))
                    total_count = cursor.fetchone()[0]

                    p = max(1, page)
                    ps = max(1, min(100, page_size or 25))
                    offset = (p - 1) * ps

                    query_sql = f"{base_query} {where_sql} ORDER BY t.created_at DESC LIMIT %s OFFSET %s"
                    item_params = list(params) + [ps, offset]
                    cursor.execute(query_sql, tuple(item_params))
                    rows = cursor.fetchall()

                tickets_list = []
                for r in rows:
                    raw_mail = r[1] or ""
                    cust_email = r[9] or ""
                    if not cust_email:
                        if "@" in raw_mail and not raw_mail.startswith("<") and not raw_mail.startswith("msg-"):
                            cust_email = raw_mail
                        else:
                            cust_email = "Unknown Customer"

                    tickets_list.append({
                        "id": r[0],
                        "mailId": r[1],
                        "customer_email": cust_email,
                        "customerEmail": cust_email,
                        "email_log_id": r[10],
                        "emailLogId": r[10],
                        "thread_id": r[11],
                        "threadId": r[11],
                        "intent": r[8] or "general_support",
                        "subject": r[2],
                        "preview": r[3],
                        "status": r[4],
                        "priority": r[7] if len(r) > 7 and r[7] else "Medium",
                        "sentiment": r[6] if len(r) > 6 and r[6] else "Neutral",
                        "time": r[5].strftime("%I:%M %p") if r[5] else "Just Now",
                        "date_str": r[5].strftime("%Y-%m-%d %I:%M %p") if r[5] else ""
                    })

                if page is None:
                    return tickets_list

                # KPI summary metrics for tenant
                m_where = f"WHERE t.{col} = %s" if client_id != "ALL" else ""
                m_params = (client_id,) if client_id != "ALL" else ()
                cursor.execute(f"""
                    SELECT 
                        COUNT(*),
                        SUM(CASE WHEN t.status = 'Ticket_Generated' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN t.status = 'Done_Replied' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN t.priority IN ('Critical', 'High') THEN 1 ELSE 0 END)
                    FROM ticket_record t
                    {m_where}
                """, m_params)
                tm_row = cursor.fetchone()
                metrics = {
                    "total": tm_row[0] or 0,
                    "pending": int(tm_row[1] or 0),
                    "resolved": int(tm_row[2] or 0),
                    "urgent": int(tm_row[3] or 0)
                }

                ps = max(1, min(100, page_size or 25))
                p = max(1, page)
                return {
                    "items": tickets_list,
                    "total": total_count,
                    "page": p,
                    "page_size": ps,
                    "total_pages": max(1, (total_count + ps - 1) // ps),
                    "metrics": metrics
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch tickets list: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat-history/{client_id}/{from_email}")
def get_chat_history_endpoint(client_id: str, from_email: str, last_n: int = 15, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    history = get_history(client_id, from_email, last_n=last_n)
    if not history:
        raise HTTPException(status_code=404, detail="No history found")
    return {
        "client_id": client_id,
        "from_email": from_email,
        "count": len(history),
        "history": history
    }


@router.delete("/chat-history/{client_id}/{from_email}")
def delete_chat_history_endpoint(client_id: str, from_email: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    clear_history(client_id, from_email)
    return {
        "status": "redis_cache_cleared",
        "client_id": client_id,
        "from_email": from_email,
        "note": "MySQL history is retained"
    }


@router.post("/pause-email")
def pause_email_endpoint(data: PauseEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("INSERT IGNORE INTO paused_emails (client_id, paused_email) VALUES (%s, %s)", (data.client_id, data.email))
                db.commit()
        return {"status": "success", "message": f"{data.email} is paused."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unpause-email")
def unpause_email_endpoint(data: PauseEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("DELETE FROM paused_emails WHERE client_id = %s AND paused_email = %s", (data.client_id, data.email))
                db.commit()
        return {"status": "success", "message": f"{data.email} is unpaused."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/paused-emails/{client_id}")
def get_paused_emails_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if client_id == "ALL":
                    cursor.execute("SELECT paused_email FROM paused_emails")
                else:
                    cursor.execute("SELECT paused_email FROM paused_emails WHERE client_id = %s", (client_id,))
                rows = cursor.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/marketing-senders")
def mark_marketing_sender_endpoint(data: MarketingSenderRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    clean_sender = data.sender_email.strip().lower()
    if not clean_sender:
        raise HTTPException(status_code=400, detail="sender_email cannot be empty")
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    INSERT IGNORE INTO marketing_senders (client_id, sender_email) 
                    VALUES (%s, %s)
                """, (data.client_id, clean_sender))
                db.commit()
        return {"status": "success", "message": f"{clean_sender} marked as Marketing / Promotional."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unmark-marketing-sender")
def unmark_marketing_sender_endpoint(data: MarketingSenderRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    clean_sender = data.sender_email.strip().lower()
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    DELETE FROM marketing_senders 
                    WHERE client_id = %s AND (LOWER(sender_email) = %s OR sender_email = %s)
                """, (data.client_id, clean_sender, clean_sender))
                db.commit()
        return {"status": "success", "message": f"{clean_sender} unmarked from Marketing / Promotional."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/marketing-senders/{client_id}")
def get_marketing_senders_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if client_id == "ALL":
                    cursor.execute("SELECT sender_email FROM marketing_senders ORDER BY created_at DESC")
                else:
                    cursor.execute("SELECT sender_email FROM marketing_senders WHERE client_id = %s ORDER BY created_at DESC", (client_id,))
                rows = cursor.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/paused-email-history/{client_id}")
@router.get("/paused-emails/{client_id}/history")
def get_paused_email_history_endpoint(
    client_id: str,
    status: Optional[str] = None,
    group_by_email: bool = False,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if status:
                if status not in ("pending_review", "ignored", "replied"):
                    raise HTTPException(status_code=400, detail="status must be one of: pending_review, ignored, replied")
                cursor.execute("""
                    SELECT id, from_email, subject, body, status, created_at, updated_at
                    FROM paused_email_history
                    WHERE client_id = %s AND status = %s
                    ORDER BY from_email, created_at DESC
                """, (client_id, status))
            else:
                cursor.execute("""
                    SELECT id, from_email, subject, body, status, created_at, updated_at
                    FROM paused_email_history
                    WHERE client_id = %s
                    ORDER BY from_email, created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()

    records = [{
        "id": r[0], "from_email": r[1], "subject": r[2], "body": r[3],
        "status": r[4], "created_at": str(r[5]), "updated_at": str(r[6]),
    } for r in rows]

    if not group_by_email:
        return records

    grouped = {}
    for rec in records:
        grouped.setdefault(rec["from_email"], []).append(rec)
    return grouped


@router.patch("/paused-email-history/{client_id}/{record_id}")
@router.patch("/paused-emails/{client_id}/history/{record_id}")
def update_paused_email_history_status_endpoint(
    client_id: str,
    record_id: int,
    data: PausedEmailHistoryUpdateRequest,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    if data.status not in ("ignored", "replied"):
        raise HTTPException(status_code=400, detail="status must be 'ignored' or 'replied'")

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE paused_email_history
                SET status = %s
                WHERE id = %s AND client_id = %s
            """, (data.status, record_id, client_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Record not found")
        db.commit()
    return {"status": "success"}


@router.post("/manual-reply")
def send_manual_reply_endpoint(data: ManualReplyRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if data.blocked_record_id is not None:
                    cursor.execute("""
                        SELECT status FROM reply_blocked_by_keyword
                        WHERE id = %s AND client_id = %s
                    """, (data.blocked_record_id, data.client_id))
                    record = cursor.fetchone()
                    if not record:
                        raise HTTPException(
                            status_code=404,
                            detail=f"blocked_record_id={data.blocked_record_id} not found for this client"
                        )
                    if record[0] != 'pending_review':
                        raise HTTPException(
                            status_code=400,
                            detail=f"Record already actioned — current status is '{record[0]}'"
                        )

                if data.paused_history_record_id is not None:
                    cursor.execute("""
                        SELECT status FROM paused_email_history
                        WHERE id = %s AND client_id = %s
                    """, (data.paused_history_record_id, data.client_id))
                    record = cursor.fetchone()
                    if not record:
                        raise HTTPException(
                            status_code=404,
                            detail=f"paused_history_record_id={data.paused_history_record_id} not found for this client"
                        )
                    if record[0] != 'pending_review':
                        raise HTTPException(
                            status_code=400,
                            detail=f"Record already actioned — current status is '{record[0]}'"
                        )

                send_status = send_email(
                    data.client_id, data.to_email, data.subject, data.reply_text
                )

                exec_steps = ["Start", "Manual_Reply", "SMTP_Send" if send_status else "SMTP_Failed"]
                cursor.execute("""
                    INSERT INTO email_logs (client_id, from_email, subject, body, reply, score, status, priority, sentiment, execution_steps)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    data.client_id, data.to_email, data.subject, data.body,
                    data.reply_text, 100,
                    'manual_reply' if send_status else 'manual_reply_send_failed',
                    'Medium', 'Neutral',
                    json.dumps(exec_steps)
                ))

                if data.blocked_record_id is not None and send_status:
                    cursor.execute("""
                        UPDATE reply_blocked_by_keyword
                        SET status = 'replied'
                        WHERE id = %s AND client_id = %s
                    """, (data.blocked_record_id, data.client_id))

                if data.paused_history_record_id is not None and send_status:
                    cursor.execute("""
                        UPDATE paused_email_history
                        SET status = 'replied'
                        WHERE id = %s AND client_id = %s
                    """, (data.paused_history_record_id, data.client_id))

                db.commit()

                if not send_status:
                    raise HTTPException(
                        status_code=502,
                        detail="Email failed to send — record kept as pending_review, log entry written"
                    )

        push_message(
            client_id=data.client_id,
            from_email=data.to_email,
            role="support",
            subject=data.subject,
            body=data.reply_text,
            ticket_id=""
        )

        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0") or "redis://localhost:6379/0"
            r = redis.from_url(redis_url)
            r.publish("email_updates", json.dumps({
                "type": "NEW_EMAIL",
                "client_id": data.client_id
            }))
            r.close()
        except Exception as ex:
            logger.warning(f"Redis publish failed: {ex}")

        return {"status": "success", "message": "Manual reply sent"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual reply error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve-pending-reply")
def approve_pending_reply_endpoint(data: ApprovePendingReplyRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT from_email, subject, reply, status FROM email_logs WHERE id=%s AND client_id=%s",
                (data.log_id, data.client_id)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Log entry not found")
            from_email, subject, reply, status = row
            if status != "pending_manual_review":
                raise HTTPException(status_code=400, detail=f"Log is not pending review (status={status})")
            if not reply:
                raise HTTPException(status_code=400, detail="No stored reply — use /manual-reply to compose one instead")

            send_status = send_email(data.client_id, from_email, "Re: " + subject, reply)
            new_status = "sent" if send_status else "send_failed"
            cursor.execute("UPDATE email_logs SET status=%s WHERE id=%s", (new_status, data.log_id))
            db.commit()

    push_message(client_id=data.client_id, from_email=from_email, role="support",
                 subject="Re: " + subject, body=reply, ticket_id="")
    return {"status": "success", "new_status": new_status}


@router.post("/emails/{email_log_id}/retry")
def retry_failed_email_endpoint(email_log_id: int, user: dict = Depends(get_current_user)):
    """
    DLQ Re-processing endpoint:
    Resets a failed or stalled email record back to 'pending',
    clears error summary, and re-dispatches into Celery pipeline.
    """
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT client_id, from_email, subject, body, body_html, message_id, in_reply_to, thread_id 
                FROM email_logs 
                WHERE id = %s
            """, (email_log_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Email record #{email_log_id} not found")
            client_id, from_email, subject, body, body_html, message_id, in_reply_to, thread_id = row
            require_client_access(client_id, user)

            cursor.execute("""
                UPDATE email_logs 
                SET status = 'pending', summary = 'Re-queued for execution by operator' 
                WHERE id = %s
            """, (email_log_id,))
            db.commit()

    import time
    task_payload = {
        "client_id": client_id,
        "from_email": from_email,
        "subject": subject or "",
        "body": body or "",
        "body_html": body_html,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "thread_id": thread_id,
        "__retry_log_id__": email_log_id,
        "__requeued_at__": int(time.time())
    }
    task = process_email_task.delay(task_payload)
    logger.info(f"🔁 [DLQ Retry] Re-queued email_logs #{email_log_id} as task {task.id}")
    return {
        "status": "requeued",
        "task_id": task.id,
        "email_log_id": email_log_id,
        "message": f"Email #{email_log_id} successfully re-queued into the pipeline."
    }

