import redis
import json
import os
import logging
from datetime import datetime, timedelta
from app.db import get_db

logger = logging.getLogger(__name__)

# DB 1 — separate from Celery broker (DB 0)
REDIS_HISTORY_URL = os.getenv("REDIS_HISTORY_URL", "redis://mail_ai_redis:6379/1")
HISTORY_TTL       = 3600   # 1 hour sliding window
MAX_MESSAGES      = 15     # last 15 messages kept in Redis

# State expiry — after this, treat as verification_failed rather than no-state
STATE_EXPIRY_HOURS = 2

redis_client = redis.from_url(REDIS_HISTORY_URL, decode_responses=True)


# ==============================
# 🔑 Key helper
# ==============================
def _make_key(client_id: str, from_email: str) -> str:
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

    db = None
    try:
        db = get_db()
        cursor = db.cursor()
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
        if db:
            db.rollback()
    finally:
        if db:
            db.close()


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

    db = None
    try:
        db = get_db()
        cursor = db.cursor()
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
    finally:
        if db:
            db.close()


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
    meta: str = ""
):
    """
    Write to Redis (hot cache) only.
    MySQL long-term storage is now handled by upsert_ticket_history.
    meta: optional JSON string encoding conversation state.
    Pass a dict and this function will serialise it automatically.
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
        key = _make_key(client_id, from_email)
        redis_client.rpush(key, json.dumps(entry))
        redis_client.ltrim(key, -MAX_MESSAGES, -1)
        redis_client.expire(key, HISTORY_TTL)
        logger.info(f"✅ Redis push — key={key} role={role} meta={meta!r}")
    except Exception as e:
        logger.error(f"❌ Redis push failed: {e}")


# ==============================
# 📖 Get history (Redis, cache-first)
# ==============================
def get_history(
    client_id: str,
    from_email: str,
    last_n: int = MAX_MESSAGES,
    ticket_id: str = ""
) -> list:
    """
    Returns Redis conversation history (oldest → newest).
    If ticket_id is provided and a MySQL summary row exists,
    prepends a synthetic 'context' entry so the LLM prompt
    has long-term issue context even after Redis TTL expires.
    """
    if not client_id:
        logger.warning("⚠️ get_history called with no client_id — returning []")
        return []

    key = _make_key(client_id, from_email)
    history = []

    try:
        raw = redis_client.lrange(key, -last_n, -1)
        if raw:
            history = [json.loads(e) for e in raw]
            logger.info(f"⚡ Redis cache hit — key={key} count={len(history)}")
    except Exception as e:
        logger.error(f"❌ Redis read failed: {e}")

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
            # Prepend so it appears before Redis messages in the prompt
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




        