import redis
import json
import os
import logging
from datetime import datetime, timedelta
from app.db import get_db, get_db_ctx
from app.redis_pool import get_redis_history

logger = logging.getLogger(__name__)

# DB 1 — separate from Celery broker (DB 0)
HISTORY_TTL       = 3600   # 1 hour sliding window
MAX_MESSAGES      = 15     # last 15 messages kept in Redis

# State expiry — after this, treat as verification_failed rather than no-state
STATE_EXPIRY_HOURS = 2

redis_client = get_redis_history()


# ==============================
# 🔑 Key helper
# ==============================
def _make_key(client_id: str, from_email: str, thread_id: str = "") -> str:
    if thread_id:
        return f"chat_history:{client_id}:th_{thread_id}"
    return f"chat_history:{client_id}:{from_email}"


# ==============================
# 🛠 Ensure MySQL table
# ==============================
def _ensure_table(cursor):
    """
    New chat_history schema — one row per unique ticket.
    Indexed on (client_id, ticket_id, customer_email).

    bot_email is intentionally excluded — always derivable from
    email_accounts via client_id (credential service).

    Drop old table and recreate if schema has changed:
        DROP TABLE IF EXISTS chat_history;
    Then restart the worker to let this function recreate it.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            client_id      VARCHAR(50)   NOT NULL,
            ticket_id      VARCHAR(50)   NOT NULL,
            customer_email VARCHAR(255)  NOT NULL,
            summary        VARCHAR(250)  DEFAULT '',
            priority       VARCHAR(50)   DEFAULT 'Normal',
            status         VARCHAR(50)   DEFAULT 'NEW',
            created_at     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
                                         ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_ticket (ticket_id),
            INDEX idx_client_ticket_email (client_id, ticket_id, customer_email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

ensure_chat_history_table = _ensure_table


# ==============================
# 💾 MySQL — upsert ticket row
# ==============================
def upsert_ticket_history(
    client_id: str,
    ticket_id: str,
    customer_email: str,
    summary: str = "",
    priority: str = "Normal",
    status: str = "NEW"
):
    """
    Insert a new row for this ticket, or update summary/priority/status
    if the row already exists.
    Called only when a real ticket ID is available from the CRM API.
    Never called for RAG-only resolutions.
    """
    if not ticket_id:
        logger.warning("⚠️ upsert_ticket_history called with no ticket_id — skipping")
        return

    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                _ensure_table(cursor)

                cursor.execute("""
                    INSERT INTO chat_history
                        (client_id, ticket_id, customer_email, summary, priority, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        summary    = VALUES(summary),
                        priority   = VALUES(priority),
                        status     = VALUES(status),
                        updated_at = CURRENT_TIMESTAMP
                """, (client_id, ticket_id, customer_email, summary, priority, status))

                db.commit()
                logger.info(
                    f"💾 chat_history upserted — client={client_id} ticket={ticket_id} status={status}"
                )
    except Exception as e:
        logger.error(f"❌ chat_history upsert failed: {e}")


# ==============================
# 📖 MySQL — get ticket row
# ==============================
def get_ticket_history(ticket_id: str) -> dict | None:
    """
    Fetch the MySQL summary row for a given ticket ID.
    Returns None if not found.
    Used to inject old_summary into generate_summary_llm.
    """
    if not ticket_id:
        return None

    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                _ensure_table(cursor)

                cursor.execute("""
                    SELECT client_id, ticket_id, customer_email, summary, priority, status, created_at, updated_at
                    FROM chat_history
                    WHERE ticket_id = %s
                    LIMIT 1
                """, (ticket_id,))
                row = cursor.fetchone()

                if not row:
                    return None

                return {
                    "client_id":      row[0],
                    "ticket_id":      row[1],
                    "customer_email": row[2],
                    "summary":        row[3] or "",
                    "priority":       row[4] or "Normal",
                    "status":         row[5] or "NEW",
                    "created_at":     row[6].isoformat() if row[6] else "",
                    "updated_at":     row[7].isoformat() if row[7] else ""
                }
    except Exception as e:
        logger.error(f"❌ chat_history fetch failed: {e}")
        return None


def get_history_from_sql(client_id: str, from_email: str, thread_id: str = "", limit: int = 10) -> list:
    """
    Reconstructs conversation dialogue from email_logs table when Redis cache misses
    (e.g. customer replied hours or days later after Redis TTL expired).
    """
    if not client_id:
        return []

    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if thread_id:
                    cursor.execute("""
                        SELECT body, reply, subject, created_at, status, summary, troubleshooting_step
                        FROM email_logs
                        WHERE client_id = %s AND thread_id = %s
                          AND status NOT IN ('system_bounce_dropped', 'rate_limited', 'automation_halted')
                        ORDER BY id DESC LIMIT %s
                    """, (client_id, thread_id, limit))
                else:
                    cursor.execute("""
                        SELECT body, reply, subject, created_at, status, summary, troubleshooting_step
                        FROM email_logs
                        WHERE client_id = %s AND from_email = %s
                          AND status NOT IN ('system_bounce_dropped', 'rate_limited', 'automation_halted')
                        ORDER BY id DESC LIMIT %s
                    """, (client_id, from_email, limit))
                rows = cursor.fetchall()
                if not rows:
                    return []

                # Reverse to get chronological order (oldest to newest)
                history = []
                for row in reversed(rows):
                    cust_body = row[0]
                    support_reply = row[1]
                    subj = row[2] or ""
                    ts = row[3].isoformat() if row[3] else ""

                    if cust_body:
                        history.append({
                            "role": "customer",
                            "subject": subj,
                            "body": cust_body,
                            "timestamp": ts,
                            "meta": json.dumps({"step": row[6] or 0}) if row[6] else ""
                        })
                    if support_reply:
                        history.append({
                            "role": "support",
                            "subject": f"Re: {subj}",
                            "body": support_reply,
                            "timestamp": ts,
                            "meta": json.dumps({"status": row[4], "step": row[6] or 0})
                        })
                logger.info(f"💾 Reconstructed {len(history)} messages from SQL email_logs (client={client_id}, thread={thread_id or from_email})")
                return history
    except Exception as e:
        logger.error(f"❌ Failed to fetch history from SQL: {e}")
        return []


# ==============================
# 📤 Push message (Redis)
# ==============================
def push_message(
    client_id: str,
    from_email: str,
    role: str,
    subject: str,
    body: str,
    ticket_id: str = "",
    meta: str = "",
    thread_id: str = ""
):
    """
    Write to Redis (hot cache).
    """
    if not client_id:
        logger.warning("⚠️ push_message called with no client_id — skipping")
        return

    if isinstance(meta, dict):
        meta = json.dumps(meta)

    entry = {
        "ticket_id": ticket_id or "",
        "role":      role,
        "subject":   subject,
        "body":      body,
        "meta":      meta or "",
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        key = _make_key(client_id, from_email, thread_id)
        redis_client.rpush(key, json.dumps(entry))
        redis_client.ltrim(key, -MAX_MESSAGES, -1)
        redis_client.expire(key, HISTORY_TTL)
        logger.info(f"✅ Redis push — key={key} role={role} meta={meta!r}")

        # Also push to email key if thread_id was used, so sender-level lookups find it
        if thread_id:
            email_key = _make_key(client_id, from_email)
            redis_client.rpush(email_key, json.dumps(entry))
            redis_client.ltrim(email_key, -MAX_MESSAGES, -1)
            redis_client.expire(email_key, HISTORY_TTL)
    except Exception as e:
        logger.error(f"❌ Redis push failed: {e}")


# ==============================
# 📖 Get history (Redis, cache-first, SQL fallback)
# ==============================
def get_history(
    client_id: str,
    from_email: str,
    last_n: int = MAX_MESSAGES,
    ticket_id: str = "",
    thread_id: str = ""
) -> list:
    """
    Returns conversation history (oldest → newest).
    1. Checks Redis cache using thread_id (or from_email).
    2. If Redis is cold/empty, reconstructs history from MySQL email_logs.
    3. If ticket_id is provided, attaches MySQL summary row.
    """
    if not client_id:
        logger.warning("⚠️ get_history called with no client_id — returning []")
        return []

    key = _make_key(client_id, from_email, thread_id)
    history = []

    try:
        raw = redis_client.lrange(key, -last_n, -1)
        if raw:
            history = [json.loads(e) for e in raw]
            logger.info(f"⚡ Redis cache hit — key={key} count={len(history)}")
        elif thread_id:
            # Try from_email key in Redis before SQL
            alt_key = _make_key(client_id, from_email)
            raw_alt = redis_client.lrange(alt_key, -last_n, -1)
            if raw_alt:
                history = [json.loads(e) for e in raw_alt]
                logger.info(f"⚡ Redis cache hit on sender key — key={alt_key} count={len(history)}")
    except Exception as e:
        logger.error(f"❌ Redis read failed: {e}")

    # Fallback to MySQL email_logs if Redis cache missed (TTL expired)
    if not history:
        logger.info(f"🔄 Redis cache miss for {key} — querying SQL fallback...")
        history = get_history_from_sql(client_id, from_email, thread_id, limit=last_n)
        if history:
            # Warm up Redis cache
            try:
                for entry in history:
                    redis_client.rpush(key, json.dumps(entry))
                redis_client.expire(key, HISTORY_TTL)
            except Exception as w_err:
                logger.warning(f"⚠️ Failed to re-warm Redis cache: {w_err}")

    # Inject MySQL summary as synthetic context entry if available
    if ticket_id:
        row = get_ticket_history(ticket_id)
        if row and row.get("summary"):
            summary_entry = {
                "ticket_id": ticket_id,
                "role":      "context",
                "subject":   "Previous issue summary",
                "body":      row["summary"],
                "meta":      "",
                "timestamp": row.get("updated_at", "")
            }
            history = [summary_entry] + history
            logger.info(f"📋 Injected MySQL summary for ticket={ticket_id}")

    return history


# ==============================
# 🔎 Get pending state
# ==============================
def get_pending_state(client_id: str, from_email: str) -> dict | None:
    """
    Walk Redis history newest → oldest and return the first non-empty
    meta dict found on a 'support' message.

    Stops at the first support message regardless of meta:
    - Empty meta = state machine was cleared = return None
    - Non-empty meta = active state, check expiry then return

    Expiry:
    - pending_verification expired → promote to verification_failed
    - verification_failed expired  → return None (start fresh)
    """
    history = get_history(client_id, from_email)

    for entry in reversed(history):
        if entry.get("role") == "support":
            if not entry.get("meta"):
                logger.info(
                    f"🔎 Most recent support message has no state — cleared — client={client_id}"
                )
                return None
            try:
                state = json.loads(entry["meta"])
                state_name = state.get("state", "")

                timestamp = entry.get("timestamp", "")
                if timestamp:
                    age = datetime.utcnow() - datetime.fromisoformat(timestamp)
                    if age > timedelta(hours=STATE_EXPIRY_HOURS):
                        if state_name == "pending_verification":
                            logger.info(
                                f"⏰ pending_verification expired (age={age})"
                                f" — promoting to verification_failed"
                            )
                            return {"state": "verification_failed"}

                        logger.info(
                            f"⏰ State '{state_name}' expired (age={age}) — ignoring"
                        )
                        return None

                logger.info(f"🔎 Found pending state: {state} — client={client_id}")
                return state

            except (json.JSONDecodeError, TypeError):
                continue

    logger.info(f"🔎 No pending state found — client={client_id}")
    return None


# ==============================
# 🧹 Clear Redis history
# ==============================
def clear_history(client_id: str, from_email: str):
    """Clears Redis only — MySQL chat_history is permanent."""
    try:
        key = _make_key(client_id, from_email)
        redis_client.delete(key)
        logger.info(f"🗑 Redis cache cleared — key={key}")
    except Exception as e:
        logger.error(f"❌ Redis clear failed: {e}")


# ==============================
# 📊 Count
# ==============================
def get_history_count(client_id: str, from_email: str) -> int:
    try:
        key = _make_key(client_id, from_email)
        return redis_client.llen(key)
    except Exception as e:
        logger.error(f"❌ Redis llen failed: {e}")
        return 0




        