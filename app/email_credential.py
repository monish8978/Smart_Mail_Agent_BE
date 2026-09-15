# following by hyper_is_op

import uuid
from app.db import get_db, get_db_ctx
import logging
from typing import Any
import json

logger = logging.getLogger(__name__)


def _decrypt_imap_password(stored: str, client_id: str | None = None) -> str:
    """Decrypt IMAP password, handling both encrypted and legacy plaintext."""
    if not stored:
        return ""
    if stored.startswith("gAAAAA"):  # Fernet token prefix
        from app.secrets_crypto import decrypt_secret
        try:
            return decrypt_secret(stored, client_id=client_id)
        except Exception as e:
            logger.error(f"Failed to decrypt IMAP password: {e}")
            return stored
    return stored  # Legacy plaintext fallback during migration


def save_email_account(client_id: str, email: str, password: str, score_threshold: int = 80, response_tone: str = "Formal", agent_type: str = "customer_support"):
    logger.info(f"💾 Saving email account for client_id={client_id} email={email} score_threshold={score_threshold} response_tone={response_tone} agent_type={agent_type}")
    from app.secrets_crypto import encrypt_secret
    encrypted_password = encrypt_secret(password, client_id=client_id) if password and not password.startswith("gAAAAA") else (password or "")
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            logger.info(f"📝 Checking duplicate records for client_id={client_id}")
            cursor.execute("SELECT id FROM email_accounts WHERE client_id = %s LIMIT 1", (client_id,))
            row = cursor.fetchone()
            
            if row:
                logger.info(f"📝 Updating existing credentials for client_id={client_id}")
                cursor.execute("""
                    UPDATE email_accounts 
                    SET email = %s, password = %s, score_threshold = %s, response_tone = %s, agent_type = %s
                    WHERE client_id = %s
                """, (email, encrypted_password, score_threshold, response_tone, agent_type, client_id))
            else:
                logger.info(f"📝 Inserting new record for client_id={client_id}")
                cursor.execute("""
                    INSERT INTO email_accounts (client_id, email, password, score_threshold, response_tone, agent_type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (client_id, email, encrypted_password, score_threshold, response_tone, agent_type))
                
            db.commit()
            logger.info(f"✅ Email account saved successfully for client_id={client_id}")


def get_connector_cap(client_id: str) -> int:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT connector_cap FROM email_accounts WHERE client_id = %s LIMIT 1", (client_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 5


def get_email_account(client_id: str) -> dict:
    logger.info(f"🔎 Fetching email account for client_id={client_id}")
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if client_id == "ALL":
                cursor.execute("""
                    SELECT score_threshold FROM email_accounts ORDER BY id ASC LIMIT 1
                """)
                row = cursor.fetchone()
                thresh = row[0] if row and row[0] is not None else 80
                return {
                    "client_id": "ALL",
                    "score_threshold": thresh,
                    "response_tone": "Formal",
                    "agent_type": "customer_support_agent"
                }

            cursor.execute("""
                SELECT client_id, email, password, score_threshold, response_tone,
                       agent_type, department_name, company_name
                FROM email_accounts WHERE client_id = %s LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning(f"⚠️ No account found for client_id={client_id}")
                return {}
            return {
                "client_id":       row[0],
                "email":           row[1],
                "password":        _decrypt_imap_password(row[2], client_id=row[0]),
                "score_threshold": row[3] if row[3] is not None else 80,
                "response_tone":   row[4] if row[4] is not None else "Formal",
                "agent_type":      row[5] if row[5] is not None else "customer_support_agent",
                "department_name": row[6] if row[6] is not None else None,
                "company_name":    row[7] if row[7] is not None else None,
            }


def ensure_accounts_table_startup(cursor):
    """
    One-time startup call only. Creates table, adds columns, 
    adds unique index. Never called in hot path.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_accounts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            client_id VARCHAR(50) NOT NULL,
            email VARCHAR(255) NOT NULL,
            password VARCHAR(255) NOT NULL,
            score_threshold INT DEFAULT 80,
            response_tone VARCHAR(50) DEFAULT 'Formal',
            agent_type VARCHAR(50) DEFAULT 'customer_support_agent',
            department_name VARCHAR(100) DEFAULT NULL,
            company_name VARCHAR(100) DEFAULT NULL,
            connector_cap INT DEFAULT 5,
            feature_ticket_creation BOOLEAN DEFAULT TRUE,
            feature_auto_send BOOLEAN DEFAULT TRUE,
            feature_rag BOOLEAN DEFAULT TRUE,
            feature_order_tracking BOOLEAN DEFAULT TRUE,
            feature_manual_reply BOOLEAN DEFAULT TRUE,
            feature_strip_disclaimers BOOLEAN DEFAULT TRUE,
            admin_bot_enabled BOOLEAN DEFAULT TRUE,
            client_bot_enabled BOOLEAN DEFAULT TRUE,
            cost_multiplier FLOAT DEFAULT 1.0,
            monthly_budget_usd FLOAT DEFAULT NULL,
            auth_type VARCHAR(20) DEFAULT 'password',
            refresh_token TEXT DEFAULT NULL,
            oauth_provider VARCHAR(30) DEFAULT NULL,
            oauth_client_id VARCHAR(255) DEFAULT NULL,
            oauth_client_secret TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_client_id (client_id)
        )
    """)

    # Ensure columns exist if table was created by older versions
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'email_accounts'")
    existing_cols = {row[0] for row in cursor.fetchall()}

    if "user_id" in existing_cols and "client_id" not in existing_cols:
        cursor.execute("ALTER TABLE email_accounts CHANGE user_id client_id VARCHAR(50) NOT NULL")

    missing_cols = [
        ("score_threshold", "INT DEFAULT 80"),
        ("response_tone", "VARCHAR(50) DEFAULT 'Formal'"),
        ("agent_type", "VARCHAR(50) DEFAULT 'customer_support_agent'"),
        ("department_name", "VARCHAR(100) DEFAULT NULL"),
        ("company_name", "VARCHAR(100) DEFAULT NULL"),
        ("connector_cap", "INT DEFAULT 5"),
        ("feature_ticket_creation", "BOOLEAN DEFAULT TRUE"),
        ("feature_auto_send", "BOOLEAN DEFAULT TRUE"),
        ("feature_rag", "BOOLEAN DEFAULT TRUE"),
        ("feature_order_tracking", "BOOLEAN DEFAULT TRUE"),
        ("feature_manual_reply", "BOOLEAN DEFAULT TRUE"),
        ("feature_strip_disclaimers", "BOOLEAN DEFAULT TRUE"),
        ("admin_bot_enabled", "BOOLEAN DEFAULT TRUE"),
        ("client_bot_enabled", "BOOLEAN DEFAULT TRUE"),
        ("cost_multiplier", "FLOAT DEFAULT 1.0"),
        ("monthly_budget_usd", "FLOAT DEFAULT NULL"),
        ("auth_type", "VARCHAR(20) DEFAULT 'password'"),
        ("refresh_token", "TEXT DEFAULT NULL"),
        ("oauth_provider", "VARCHAR(30) DEFAULT NULL"),
        ("oauth_client_id", "VARCHAR(255) DEFAULT NULL"),
        ("oauth_client_secret", "TEXT DEFAULT NULL"),
    ]

    for col_name, col_def in missing_cols:
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE email_accounts ADD COLUMN {col_name} {col_def}")

    # --- Encrypt any plaintext IMAP passwords ---
    try:
        from app.secrets_crypto import encrypt_secret
        cursor.execute("SELECT id, password FROM email_accounts WHERE password IS NOT NULL AND password != ''")
        rows = cursor.fetchall()
        migrated = 0
        for row_id, raw_pw in rows:
            # Skip if already Fernet-encrypted (base64 token starting with 'gAAAAA')
            if raw_pw and raw_pw.startswith("gAAAAA"):
                continue
            encrypted = encrypt_secret(raw_pw)
            cursor.execute("UPDATE email_accounts SET password = %s WHERE id = %s", (encrypted, row_id))
            migrated += 1
        if migrated:
            logger.info(f"🔐 Migrated {migrated} plaintext IMAP passwords to encrypted storage")
    except Exception as e:
        logger.error(f"❌ IMAP password encryption migration failed: {e}")
        raise  # Fail startup loudly — don't silently skip


def ensure_ticket_record_table(cursor):
    """
    Creates ticket_record table if it does not exist.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticket_record (
            ticket_id  VARCHAR(50)  NOT NULL PRIMARY KEY,
            client_id  VARCHAR(50)  NOT NULL,
            mail_id    VARCHAR(100) NOT NULL,
            subject    TEXT         NOT NULL,
            body       TEXT         NOT NULL,
            status     VARCHAR(50)  NOT NULL,
            created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ticket_record'")
    existing_cols = {row[0] for row in cursor.fetchall()}

    if "user_id" in existing_cols and "client_id" not in existing_cols:
        cursor.execute("ALTER TABLE ticket_record CHANGE user_id client_id VARCHAR(50) NOT NULL")
    if "sentiment" not in existing_cols:
        cursor.execute("ALTER TABLE ticket_record ADD COLUMN sentiment VARCHAR(50) DEFAULT NULL")
    if "priority" not in existing_cols:
        cursor.execute("ALTER TABLE ticket_record ADD COLUMN priority VARCHAR(50) DEFAULT NULL")


_ensure_table = ensure_ticket_record_table


def create_email_record_db(data: dict) -> dict:
    """
    Inserts one email record into ticket_record.
    Args:
        data: dict with keys user_id, mail_id, subject, body, status
    Returns:
        {"success": True,  "ticket_id": "<uuid>"}
        {"success": False, "error": <str>}
    """
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                _ensure_table(cursor)
                ticket_id = str(uuid.uuid4())  # random, unique e.g. "3f2a1b4c-..."
                cursor.execute("""
                    INSERT INTO ticket_record (ticket_id, client_id, mail_id, subject, body, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    ticket_id,
                    data["client_id"],
                    data["mail_id"],
                    data["subject"],
                    data["body"],
                    data["status"],
                ))
            db.commit()
            logger.info(f"✅ Ticket created — ticket_id={ticket_id}")
            return {"success": True, "ticket_id": ticket_id, "client_id": data["client_id"], "mail_id": data["mail_id"]}
    except Exception as e:
        logger.error(f"❌ DB insert failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def ensure_create_payload_table():
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS create_payload_table (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    client_id   VARCHAR(50) NOT NULL,
                    url         VARCHAR(255),
                    paylod      TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            try:
                cursor.execute("ALTER TABLE create_payload_table ADD UNIQUE INDEX (client_id)")
            except Exception:
                pass
        db.commit()
        logger.info("✅ create_payload_table ensured")


def insert_create_payload_ticket(client_id: str, url: str, paylod: dict[str, Any]) -> str:
    ensure_create_payload_table()
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM create_payload_table WHERE client_id = %s LIMIT 1", (client_id,))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE create_payload_table 
                    SET url = %s, paylod = %s
                    WHERE client_id = %s
                """, (url, json.dumps(paylod), client_id))
            else:
                cursor.execute("""
                    INSERT INTO create_payload_table (client_id, url, paylod)
                    VALUES (%s, %s, %s)
                """, (client_id, url, json.dumps(paylod)))
        db.commit()
        logger.info(f"✅ create_payload_table inserted — client_id={client_id}")
        return client_id


def get_create_payload_table(client_id: str) -> dict:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT url, paylod 
                FROM create_payload_table WHERE client_id = %s LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
        if not row:
            logger.warning(f"⚠️ No create_payload_table found for client_id={client_id}")
            return {}
        return {
            "url": row[0],
            "paylod": json.loads(row[1])
        }


def ensure_payload_get_ticket_table():
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payload_get_table (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    client_id   VARCHAR(50) NOT NULL,
                    url         VARCHAR(255),
                    paylod      TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            try:
                cursor.execute("ALTER TABLE payload_get_table ADD UNIQUE INDEX (client_id)")
            except Exception:
                pass
        db.commit()
        logger.info("✅ payload_get_table ensured")


def insert_payload_get_ticket(client_id: str, url: str, paylod: dict[str, Any]) -> str:
    ensure_payload_get_ticket_table()
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM payload_get_table WHERE client_id = %s LIMIT 1", (client_id,))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE payload_get_table 
                    SET url = %s, paylod = %s
                    WHERE client_id = %s
                """, (url, json.dumps(paylod), client_id))
            else:
                cursor.execute("""
                    INSERT INTO payload_get_table (client_id, url, paylod)
                    VALUES (%s, %s, %s)
                """, (client_id, url, json.dumps(paylod)))
        db.commit()
        logger.info(f"✅ payload_get_table inserted — client_id={client_id}")
        return client_id


def get_payload_get_ticket_table(client_id: str) -> dict:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT url, paylod 
                FROM payload_get_table WHERE client_id = %s LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
        if not row:
            logger.warning(f"⚠️ No payload_get_table found for client_id={client_id}")
            return {}
        return {
            "url": row[0],
            "paylod": json.loads(row[1])
        }


def get_budget_status(client_id, cursor):
    """
    Returns current-month spend vs budget for a client.
    status: 'unlimited' (no budget set) | 'ok' | 'warning' (>=90%) | 'exceeded' (>=100%)
    Never blocks anything — purely informational.
    """
    cursor.execute("SELECT monthly_budget_usd FROM email_accounts WHERE client_id=%s", (client_id,))
    row = cursor.fetchone()
    budget = float(row[0]) if row and row[0] is not None else None

    cursor.execute("""
        SELECT COALESCE(SUM(billed_cost), 0) FROM llm_logs
        WHERE client_id=%s AND MONTH(created_at)=MONTH(CURDATE()) AND YEAR(created_at)=YEAR(CURDATE())
    """, (client_id,))
    spent = float(cursor.fetchone()[0] or 0)

    if budget is None:
        return {"budget": None, "spent": round(spent, 4), "percent": None, "status": "unlimited"}

    percent = (spent / budget * 100) if budget > 0 else 0
    if percent >= 100:
        status = "exceeded"
    elif percent >= 90:
        status = "warning"
    else:
        status = "ok"

    return {"budget": budget, "spent": round(spent, 4), "percent": round(percent, 1), "status": status}


def get_all_create_payloads() -> list[dict]:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT p.client_id, p.url, p.paylod, a.email 
                FROM create_payload_table p
                LEFT JOIN email_accounts a 
                  ON p.client_id COLLATE utf8mb4_unicode_ci = a.client_id COLLATE utf8mb4_unicode_ci
            """)
            rows = cursor.fetchall()
        result = []
        for r in rows:
            try:
                pay = json.loads(r[2]) if r[2] else {}
            except Exception:
                pay = r[2]
            result.append({
                "client_id": r[0],
                "url": r[1],
                "paylod": pay,
                "email": r[3] or r[0]
            })
        return result


def get_all_get_payloads() -> list[dict]:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT p.client_id, p.url, p.paylod, a.email 
                FROM payload_get_table p
                LEFT JOIN email_accounts a 
                  ON p.client_id COLLATE utf8mb4_unicode_ci = a.client_id COLLATE utf8mb4_unicode_ci
            """)
            rows = cursor.fetchall()
        result = []
        for r in rows:
            try:
                pay = json.loads(r[2]) if r[2] else {}
            except Exception:
                pay = r[2]
            result.append({
                "client_id": r[0],
                "url": r[1],
                "paylod": pay,
                "email": r[3] or r[0]
            })
        return result