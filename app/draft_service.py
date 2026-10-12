import logging
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.db import get_db_ctx
from app.mailer import send_email

logger = logging.getLogger(__name__)

def ensure_draft_emails_table(cursor=None):
    """
    Creates the draft_emails table if not exists with all required indexes.
    """
    create_sql = """
    CREATE TABLE IF NOT EXISTS draft_emails (
        id INT AUTO_INCREMENT PRIMARY KEY,
        client_id VARCHAR(50) NOT NULL,
        email_log_id INT DEFAULT NULL,
        from_email VARCHAR(255) NOT NULL,
        sender_name VARCHAR(255) DEFAULT NULL,
        to_email VARCHAR(255) NOT NULL,
        subject VARCHAR(500) NOT NULL,
        original_body MEDIUMTEXT NOT NULL,
        draft_reply MEDIUMTEXT NOT NULL,
        confidence_score INT DEFAULT 0,
        intent VARCHAR(100) DEFAULT NULL,
        sentiment VARCHAR(50) DEFAULT NULL,
        priority VARCHAR(20) DEFAULT 'Normal',
        ticket_id VARCHAR(100) DEFAULT NULL,
        in_reply_to VARCHAR(255) DEFAULT NULL,
        message_id VARCHAR(255) DEFAULT NULL,
        status ENUM('pending', 'approved', 'sent', 'rejected', 'discarded', 'sending') DEFAULT 'pending',
        rejection_reason VARCHAR(255) DEFAULT NULL,
        reviewed_by VARCHAR(100) DEFAULT NULL,
        reviewed_at TIMESTAMP NULL DEFAULT NULL,
        sent_at TIMESTAMP NULL DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_client_status (client_id, status),
        INDEX idx_from_email (from_email),
        INDEX idx_created_at (created_at),
        INDEX idx_intent (intent),
        INDEX idx_confidence (confidence_score)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    if cursor:
        cursor.execute(create_sql)
        _ensure_thread_id_column(cursor)
    else:
        with get_db_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(create_sql)
                _ensure_thread_id_column(cur)
            conn.commit()


def _ensure_thread_id_column(cur):
    try:
        cur.execute("SELECT thread_id FROM draft_emails LIMIT 1")
    except Exception:
        try:
            cur.execute("ALTER TABLE draft_emails ADD COLUMN thread_id VARCHAR(255) DEFAULT NULL AFTER message_id, ADD INDEX idx_thread_id (thread_id)")
        except Exception as e:
            logger.warning(f"Could not add thread_id column to draft_emails: {e}")
    try:
        cur.execute("""
            UPDATE draft_emails d
            JOIN email_logs e ON d.email_log_id = e.id
            SET d.thread_id = e.thread_id
            WHERE d.thread_id IS NULL AND e.thread_id IS NOT NULL
        """)
    except Exception:
        pass


def create_draft(
    client_id: str,
    from_email: str,
    to_email: str,
    subject: str,
    original_body: str,
    draft_reply: str,
    confidence_score: int = 0,
    intent: Optional[str] = None,
    sentiment: Optional[str] = None,
    priority: str = "Normal",
    ticket_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    message_id: Optional[str] = None,
    sender_name: Optional[str] = None,
    email_log_id: Optional[int] = None,
    thread_id: Optional[str] = None,
) -> int:
    """
    Inserts a newly generated AI reply into draft_emails with pending status.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            sql = """
            INSERT INTO draft_emails (
                client_id, email_log_id, from_email, sender_name, to_email,
                subject, original_body, draft_reply, confidence_score,
                intent, sentiment, priority, ticket_id, in_reply_to, message_id, thread_id, status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, 'pending'
            )
            """
            cur.execute(sql, (
                client_id, email_log_id, from_email, sender_name, to_email,
                subject, original_body, draft_reply, confidence_score,
                intent, sentiment, priority, ticket_id, in_reply_to, message_id, thread_id
            ))
            draft_id = cur.lastrowid
        conn.commit()
    logger.info(f"📝 [Client {client_id}] Created pending draft #{draft_id} for {from_email} (score={confidence_score}, intent={intent})")
    return draft_id


def list_drafts(
    client_id: Optional[str] = None,
    status: Optional[str] = "pending",
    search: Optional[str] = None,
    intent: Optional[str] = None,
    sentiment: Optional[str] = None,
    from_email: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """
    Lists drafts with dynamic filtering, full-text search, and pagination.
    """
    ensure_draft_emails_table()
    conditions = []
    params = []

    if client_id and client_id != "ALL":
        conditions.append("d.client_id = %s")
        params.append(client_id)

    if status and status != "ALL":
        conditions.append("d.status = %s")
        params.append(status)

    if from_email:
        conditions.append("d.from_email LIKE %s")
        params.append(f"%{from_email.strip()}%")

    if intent and intent != "ALL":
        conditions.append("d.intent = %s")
        params.append(intent)

    if sentiment and sentiment != "ALL":
        conditions.append("d.sentiment = %s")
        params.append(sentiment)

    if min_score is not None:
        conditions.append("d.confidence_score >= %s")
        params.append(min_score)

    if max_score is not None:
        conditions.append("d.confidence_score <= %s")
        params.append(max_score)

    if date_from:
        conditions.append("d.created_at >= %s")
        params.append(date_from)

    if date_to:
        conditions.append("d.created_at <= %s")
        params.append(date_to)

    if search:
        search_clean = search.strip()
        search_term = f"%{search_clean}%"
        num_clean = search_clean.lstrip("#") if search_clean.startswith("#") else (search_clean if search_clean.isdigit() else None)

        if num_clean and num_clean.isdigit():
            conditions.append(
                "(d.id = %s OR d.email_log_id = %s OR d.subject LIKE %s OR d.original_body LIKE %s OR d.draft_reply LIKE %s OR d.from_email LIKE %s OR d.sender_name LIKE %s OR d.ticket_id LIKE %s OR COALESCE(d.thread_id, e.thread_id) LIKE %s)"
            )
            params.extend([int(num_clean), int(num_clean), search_term, search_term, search_term, search_term, search_term, search_term, search_term])
        else:
            conditions.append(
                "(d.subject LIKE %s OR d.original_body LIKE %s OR d.draft_reply LIKE %s OR d.from_email LIKE %s OR d.sender_name LIKE %s OR d.ticket_id LIKE %s OR COALESCE(d.thread_id, e.thread_id) LIKE %s OR CAST(d.id AS CHAR) LIKE %s OR CAST(d.email_log_id AS CHAR) LIKE %s)"
            )
            params.extend([search_term, search_term, search_term, search_term, search_term, search_term, search_term, search_term, search_term])

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    offset = max(0, (page - 1) * page_size)

    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            # Get total count
            count_sql = f"SELECT COUNT(*) FROM draft_emails d LEFT JOIN email_logs e ON d.email_log_id = e.id{where_clause}"
            cur.execute(count_sql, tuple(params))
            total_count = cur.fetchone()[0]

            # Get items
            query_sql = f"""
            SELECT d.id, d.client_id, d.email_log_id, d.from_email, d.sender_name, d.to_email,
                   d.subject, d.original_body, d.draft_reply, d.confidence_score,
                   d.intent, d.sentiment, d.priority, d.ticket_id, d.in_reply_to, d.message_id,
                   d.status, d.rejection_reason, d.reviewed_by, d.reviewed_at, d.sent_at, d.created_at,
                   COALESCE(d.thread_id, e.thread_id) AS thread_id
            FROM draft_emails d
            LEFT JOIN email_logs e ON d.email_log_id = e.id
            {where_clause}
            ORDER BY d.created_at DESC
            LIMIT %s OFFSET %s
            """
            item_params = list(params) + [page_size, offset]
            cur.execute(query_sql, tuple(item_params))
            rows = cur.fetchall()

            # Format items
            columns = [
                "id", "client_id", "email_log_id", "from_email", "sender_name", "to_email",
                "subject", "original_body", "draft_reply", "confidence_score",
                "intent", "sentiment", "priority", "ticket_id", "in_reply_to", "message_id",
                "status", "rejection_reason", "reviewed_by", "reviewed_at", "sent_at", "created_at",
                "thread_id"
            ]
            items = []
            for row in rows:
                item = dict(zip(columns, row))
                # Convert timestamps to isoformat strings
                for time_col in ["reviewed_at", "sent_at", "created_at"]:
                    if item[time_col]:
                        item[time_col] = item[time_col].isoformat()
                items.append(item)

    return {
        "items": items,
        "total": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total_count + page_size - 1) // page_size)
    }


def get_draft(draft_id: int, client_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieves a single draft by ID with security verification.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            sql = """
            SELECT d.id, d.client_id, d.email_log_id, d.from_email, d.sender_name, d.to_email,
                   d.subject, d.original_body, d.draft_reply, d.confidence_score,
                   d.intent, d.sentiment, d.priority, d.ticket_id, d.in_reply_to, d.message_id,
                   d.status, d.rejection_reason, d.reviewed_by, d.reviewed_at, d.sent_at, d.created_at,
                   COALESCE(d.thread_id, e.thread_id) AS thread_id
            FROM draft_emails d
            LEFT JOIN email_logs e ON d.email_log_id = e.id
            WHERE d.id = %s
            """
            params = [draft_id]
            if client_id and client_id != "ALL":
                sql += " AND d.client_id = %s"
                params.append(client_id)
            sql += " LIMIT 1"

            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            if not row:
                return None
            
            columns = [
                "id", "client_id", "email_log_id", "from_email", "sender_name", "to_email",
                "subject", "original_body", "draft_reply", "confidence_score",
                "intent", "sentiment", "priority", "ticket_id", "in_reply_to", "message_id",
                "status", "rejection_reason", "reviewed_by", "reviewed_at", "sent_at", "created_at",
                "thread_id"
            ]
            item = dict(zip(columns, row))
            for time_col in ["reviewed_at", "sent_at", "created_at"]:
                if item.get(time_col):
                    item[time_col] = item[time_col].isoformat()
            return item


def update_draft(
    draft_id: int,
    client_id: Optional[str] = None,
    subject: Optional[str] = None,
    draft_reply: Optional[str] = None,
    to_email: Optional[str] = None,
    reviewed_by: Optional[str] = None,
) -> bool:
    """
    Updates the content or recipient of a pending draft.
    """
    ensure_draft_emails_table()
    updates = []
    params = []

    if subject is not None:
        updates.append("subject = %s")
        params.append(subject)

    if draft_reply is not None:
        updates.append("draft_reply = %s")
        params.append(draft_reply)

    if to_email is not None:
        updates.append("to_email = %s")
        params.append(to_email)

    if reviewed_by is not None:
        updates.append("reviewed_by = %s")
        params.append(reviewed_by)
        updates.append("reviewed_at = CURRENT_TIMESTAMP")

    if not updates:
        return False

    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            where = "WHERE id = %s AND status = 'pending'"
            params.append(draft_id)
            if client_id and client_id != "ALL":
                where += " AND client_id = %s"
                params.append(client_id)
            
            sql = f"UPDATE draft_emails SET {', '.join(updates)} {where}"
            cur.execute(sql, tuple(params))
            affected = cur.rowcount
        conn.commit()
    return affected > 0


def send_single_draft(draft_id: int, client_id: Optional[str] = None, reviewed_by: Optional[str] = "Admin") -> Dict[str, Any]:
    """
    Atomically locks and sends a single draft via SMTP, updating status to 'sent'.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            # Atomic lock transition to prevent double-send race conditions
            lock_sql = "UPDATE draft_emails SET status = 'sending' WHERE id = %s AND status = 'pending'"
            lock_params = [draft_id]
            if client_id and client_id != "ALL":
                lock_sql += " AND client_id = %s"
                lock_params.append(client_id)
            cur.execute(lock_sql, tuple(lock_params))
            if cur.rowcount == 0:
                return {"success": False, "error": f"Draft #{draft_id} is not in pending status or not found"}
        conn.commit()

    # Fetch draft payload
    draft = get_draft(draft_id)
    if not draft:
        return {"success": False, "error": f"Draft #{draft_id} could not be loaded"}

    # Execute SMTP Send
    target_client = draft["client_id"]
    recipient = draft["from_email"]
    subject = draft["subject"] if draft["subject"].startswith("Re:") else "Re: " + draft["subject"]
    body = draft["draft_reply"]
    in_reply_to = draft.get("message_id") or draft.get("in_reply_to")

    try:
        sent_ok = send_email(target_client, recipient, subject, body, in_reply_to=in_reply_to)
        if not sent_ok:
            # Revert status to pending on failure
            with get_db_ctx() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE draft_emails SET status = 'pending' WHERE id = %s", (draft_id,))
                conn.commit()
            return {"success": False, "error": "SMTP server rejected the email transmission"}

        # Mark sent
        with get_db_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE draft_emails 
                    SET status = 'sent', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP, sent_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (reviewed_by, draft_id))
            conn.commit()

        logger.info(f"🚀 [Client {target_client}] Successfully dispatched draft #{draft_id} to {recipient}")
        return {"success": True, "draft_id": draft_id, "sent_to": recipient}

    except Exception as e:
        logger.error(f"❌ Failed to dispatch draft #{draft_id}: {e}", exc_info=True)
        with get_db_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE draft_emails SET status = 'pending' WHERE id = %s", (draft_id,))
            conn.commit()
        return {"success": False, "error": str(e)}


def discard_draft(draft_id: int, client_id: Optional[str] = None, rejection_reason: Optional[str] = None, reviewed_by: Optional[str] = "Admin") -> bool:
    """
    Marks a pending draft as discarded/rejected.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            where = "WHERE id = %s AND status = 'pending'"
            params = [rejection_reason or "Manually discarded", reviewed_by, draft_id]
            if client_id and client_id != "ALL":
                where += " AND client_id = %s"
                params.append(client_id)
            sql = f"""
            UPDATE draft_emails 
            SET status = 'discarded', rejection_reason = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP 
            {where}
            """
            cur.execute(sql, tuple(params))
            affected = cur.rowcount
        conn.commit()
    logger.info(f"🗑️ Draft #{draft_id} discarded by {reviewed_by}")
    return affected > 0


def batch_send_drafts(draft_ids: List[int], client_id: Optional[str] = None, reviewed_by: Optional[str] = "Admin") -> Dict[str, Any]:
    """
    Dispatches multiple drafts by ID list sequentially with summary metrics.
    """
    if not draft_ids:
        return {"total": 0, "sent": 0, "failed": 0, "results": []}

    sent_count = 0
    failed_count = 0
    results = []

    for did in draft_ids:
        res = send_single_draft(did, client_id=client_id, reviewed_by=reviewed_by)
        if res.get("success"):
            sent_count += 1
        else:
            failed_count += 1
        results.append(res)

    logger.info(f"📦 Batch Send complete: {sent_count} sent, {failed_count} failed out of {len(draft_ids)} total")
    return {
        "total": len(draft_ids),
        "sent": sent_count,
        "failed": failed_count,
        "results": results
    }


def batch_send_by_filter(filter_params: Dict[str, Any], client_id: Optional[str] = None, reviewed_by: Optional[str] = "Admin") -> Dict[str, Any]:
    """
    Finds all pending drafts matching specific filter criteria and dispatches them in batch.
    """
    # Fetch up to 100 pending drafts matching the filter
    drafts_res = list_drafts(
        client_id=client_id,
        status="pending",
        search=filter_params.get("search"),
        intent=filter_params.get("intent"),
        sentiment=filter_params.get("sentiment"),
        from_email=filter_params.get("from_email"),
        min_score=filter_params.get("min_score"),
        max_score=filter_params.get("max_score"),
        date_from=filter_params.get("date_from"),
        date_to=filter_params.get("date_to"),
        page=1,
        page_size=100
    )

    ids = [d["id"] for d in drafts_res.get("items", [])]
    if not ids:
        return {"total": 0, "sent": 0, "failed": 0, "message": "No pending drafts matched the specified filter criteria"}

    return batch_send_drafts(ids, client_id=client_id, reviewed_by=reviewed_by)


def get_pending_drafts_count(client_id: Optional[str] = None) -> int:
    """
    Returns total count of pending drafts for topbar/sidebar badges.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            if client_id and client_id != "ALL":
                cur.execute("SELECT COUNT(*) FROM draft_emails WHERE client_id = %s AND status = 'pending'", (client_id,))
            else:
                cur.execute("SELECT COUNT(*) FROM draft_emails WHERE status = 'pending'")
            return cur.fetchone()[0]


def get_draft_metrics(client_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns high-level statistics for the Drafts dashboard cards.
    """
    ensure_draft_emails_table()
    with get_db_ctx() as conn:
        with conn.cursor() as cur:
            where = ""
            params = []
            if client_id and client_id != "ALL":
                where = "WHERE client_id = %s"
                params = [client_id]

            sql = f"""
            SELECT 
                COUNT(*) AS total_drafts,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                SUM(CASE WHEN status = 'discarded' THEN 1 ELSE 0 END) AS discarded_count,
                AVG(CASE WHEN status = 'pending' THEN confidence_score ELSE NULL END) AS avg_confidence
            FROM draft_emails {where}
            """
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            
            return {
                "total": row[0] or 0,
                "pending": int(row[1] or 0),
                "sent": int(row[2] or 0),
                "discarded": int(row[3] or 0),
                "avg_confidence": round(float(row[4] or 0), 1)
            }
