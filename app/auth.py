import hashlib
import bcrypt
import uuid
import pymysql
import pymysql.cursors
import redis
import json
import logging
import os
from app.db import get_db, get_db_ctx

logger = logging.getLogger(__name__)

def ensure_users_table():
    with get_db_ctx() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50) UNIQUE NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role ENUM('admin', 'client') DEFAULT 'client',
                    status ENUM('active', 'inactive') NOT NULL DEFAULT 'active',
                    name VARCHAR(255) DEFAULT NULL,
                    phone_number VARCHAR(50) DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'")
            existing_cols = {row[0] for row in cursor.fetchall()}

            if "name" not in existing_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN name VARCHAR(255) DEFAULT NULL")
            if "phone_number" not in existing_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN phone_number VARCHAR(50) DEFAULT NULL")
        conn.commit()

def hash_password(password: str) -> str:
    """Hash password with bcrypt (salted, key-stretched)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Verify password against stored hash.
    Supports both legacy SHA-256 (64-char hex) and bcrypt hashes.
    If SHA-256 match found, returns True (caller should re-hash and update).
    """
    if not stored_hash:
        return False
    # Legacy SHA-256 detection: exactly 64 hex chars
    if len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash):
        if hashlib.sha256(password.encode()).hexdigest() == stored_hash:
            return True  # Legacy match — caller should upgrade hash
        return False
    # bcrypt verification
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except Exception:
        return False


def is_legacy_hash(stored_hash: str) -> bool:
    """Returns True if the stored hash is a legacy SHA-256 (needs upgrade)."""
    return len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash)


def register_admin_by_admin(email, password, creator_client_id):
    """
    The ONLY way to create an admin account: an existing admin creates one.
    No self-service admin registration exists anymore.
    """
    ensure_users_table()
    client_id = "CLI-" + uuid.uuid4().hex[:8].upper()
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
            if cursor.fetchone():
                return {"success": False, "error": "Email already registered"}
            p_hash = hash_password(password)
            cursor.execute(
                "INSERT INTO users (client_id, email, password_hash, role, status) VALUES (%s, %s, %s, 'admin', 'active')",
                (client_id, email, p_hash)
            )
        conn.commit()
        return {"success": True, "client_id": client_id}


def ensure_admin_seeded():
    """
    Auto-seeds or syncs an admin user defined in .env (ADMIN_EMAIL, ADMIN_PASSWORD).
    Runs on startup so any new/blank database gets an admin automatically.
    """
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_email or not admin_password:
        return

    ensure_users_table()
    try:
        with get_db_ctx() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT id, role, password_hash FROM users WHERE email=%s", (admin_email,))
                existing = cursor.fetchone()
                p_hash = hash_password(admin_password)

                if existing:
                    needs_update = (
                        existing.get("role") != "admin"
                        or existing.get("status") != "active"
                        or not verify_password(admin_password, existing.get("password_hash", ""))
                    )
                    if needs_update:
                        cursor.execute(
                            "UPDATE users SET role='admin', status='active', password_hash=%s WHERE id=%s",
                            (p_hash, existing["id"])
                        )
                        conn.commit()
                else:
                    client_id = "CLI-" + uuid.uuid4().hex[:8].upper()
                    cursor.execute(
                        "INSERT INTO users (client_id, email, password_hash, role, status, name) "
                        "VALUES (%s, %s, %s, 'admin', 'active', 'System Admin')",
                        (client_id, admin_email, p_hash)
                    )
                    conn.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"⚠️ Failed to seed admin user from .env: {e}")


def login_user(email, password):
    ensure_users_table()
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT u.id, u.client_id, u.email, u.role, u.password_hash, u.status, u.name,
                       ea.company_name, ea.department_name
                FROM users u
                LEFT JOIN email_accounts ea ON u.client_id = ea.client_id
                WHERE u.email=%s
            """, (email,))
            user = cursor.fetchone()
            if not user:
                return {"success": False, "error": "Invalid email or password"}
            if user.get("status") != "active":
                return {"success": False, "error": "Your account is inactive. Contact admin."}
            if not verify_password(password, user["password_hash"]):
                return {"success": False, "error": "Invalid email or password"}

            # Auto-upgrade legacy SHA-256 hash to bcrypt on successful login
            if is_legacy_hash(user["password_hash"]):
                new_hash = hash_password(password)
                cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, user["id"]))
                conn.commit()
                logger.info(f"🔐 Upgraded password hash to bcrypt for user {user['email']}")

            user_payload = {
                "id": user["id"], 
                "client_id": user["client_id"], 
                "email": user["email"], 
                "role": user["role"],
                "name": user.get("name") or "",
                "company_name": user.get("company_name") or "",
                "department_name": user.get("department_name") or ""
            }
            token = create_session(user_payload)
            return {"success": True, "user": user_payload, "token": token}


def _session_redis():
    from app.redis_pool import get_redis_sessions
    return get_redis_sessions()


def create_session(user_payload: dict, ttl_seconds: int = 86400) -> str:
    r = _session_redis()
    token = str(uuid.uuid4())
    key = f"session:{token}"
    r.set(key, json.dumps(user_payload), ex=ttl_seconds)
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    r = _session_redis()
    raw = r.get(f"session:{token}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def destroy_session(token: str):
    r = _session_redis()
    r.delete(f"session:{token}")


def create_client_atomic(name, phone_number, login_email, login_password, imap_email, imap_password,
                          score_threshold=80, response_tone="Formal",
                          agent_type="customer_support_agent",
                          department_name=None, company_name=None):
    """
    Admin-only: creates the login account (users, role=client, status=active)
    AND the IMAP/feature config (email_accounts) under one new client_id,
    in a single transaction. No separate approval step needed.
    """
    ensure_users_table()
    client_id = "CLI-" + uuid.uuid4().hex[:8].upper()
    try:
        with get_db_ctx() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT id, client_id FROM users WHERE email=%s", (login_email,))
                existing_user = cursor.fetchone()
                if existing_user:
                    # Check if this user is an orphan (missing from email_accounts)
                    cursor.execute("SELECT id FROM email_accounts WHERE client_id=%s", (existing_user["client_id"],))
                    if cursor.fetchone():
                        return {"success": False, "error": "Login email already registered"}
                    
                    # Self-heal orphan: update profile/password and populate email_accounts
                    client_id = existing_user["client_id"]
                    p_hash = hash_password(login_password)
                    cursor.execute("""
                        UPDATE users SET password_hash=%s, name=%s, phone_number=%s, status='active'
                        WHERE id=%s
                    """, (p_hash, name, phone_number, existing_user["id"]))
                else:
                    p_hash = hash_password(login_password)
                    cursor.execute(
                        "INSERT INTO users (client_id, email, password_hash, role, status, name, phone_number) VALUES (%s, %s, %s, 'client', 'active', %s, %s)",
                        (client_id, login_email, p_hash, name, phone_number)
                    )
                
                actual_imap = imap_email if imap_email else login_email
                from app.secrets_crypto import encrypt_secret
                encrypted_imap = encrypt_secret(imap_password) if imap_password and not imap_password.startswith("gAAAAA") else (imap_password or "")
                cursor.execute("""
                    INSERT INTO email_accounts (client_id, email, password, score_threshold, response_tone, agent_type, department_name, company_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        email = VALUES(email),
                        password = VALUES(password),
                        score_threshold = VALUES(score_threshold),
                        response_tone = VALUES(response_tone),
                        agent_type = VALUES(agent_type),
                        department_name = VALUES(department_name),
                        company_name = VALUES(company_name)
                """, (client_id, actual_imap, encrypted_imap, score_threshold, response_tone,
                    agent_type, department_name, company_name))
            conn.commit()

        try:
            from app.mailer import send_email
            subject = "Your Mail AI Account Has Been Created"
            body = (
                f"Hello,\n\nAn administrator has created your account.\n\n"
                f"Login email: {login_email}\nPassword: {login_password}\n\n"
                f"Log in at: http://172.16.3.215:1947/login\n\nThanks,\nMail AI Team"
            )
            send_email("registration", login_email, subject, body)
        except Exception as mail_err:
            print(f"Error sending new-client credentials email: {mail_err}")

        return {"success": True, "client_id": client_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_pending_users():
    ensure_users_table()
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT id, client_id, email, role, created_at FROM users WHERE status='inactive' ORDER BY created_at ASC")
            return cursor.fetchall()


def send_reset_otp(email: str):
    ensure_users_table()
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT id, client_id, email, status FROM users WHERE email=%s", (email,))
            user = cursor.fetchone()
            if not user:
                return {"success": False, "error": "Email address not found"}
            if user.get("status") != "active":
                return {"success": False, "error": "Your account is inactive. Contact admin."}
            
            import secrets
            otp = "".join(secrets.choice("0123456789") for _ in range(6))
            
            r = _session_redis()
            r.set(f"reset_otp:{email}", otp, ex=900)
            
            try:
                from app.mailer import send_email
                subject = "Your Password Reset OTP"
                body = (
                    f"Hello,\n\nYou requested a password reset. Use the following verification code to set your new password:\n\n"
                    f"Verification Code: {otp}\n\n"
                    f"This code will expire in 15 minutes.\n\n"
                    f"Thanks,\nMail AI Team"
                )
                sent = send_email("registration", email, subject, body)
                if not sent:
                    return {"success": False, "error": "Failed to send the email. Please check server SMTP configurations."}
            except Exception as mail_err:
                print(f"Error sending password reset OTP email: {mail_err}")
                return {"success": False, "error": "Failed to send reset email due to internal SMTP error."}
                
            return {"success": True, "message": "Verification code sent to your email address."}


def reset_password_with_otp(email: str, otp: str, new_password: str):
    if len(new_password) < 8:
        return {"success": False, "error": "Password must be at least 8 characters"}
        
    ensure_users_table()
    r = _session_redis()
    stored_otp = r.get(f"reset_otp:{email}")
    
    if not stored_otp:
        return {"success": False, "error": "OTP has expired or email is invalid"}
        
    if stored_otp != otp.strip():
        return {"success": False, "error": "Invalid verification code"}
        
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            p_hash = hash_password(new_password)
            cursor.execute("UPDATE users SET password_hash=%s WHERE email=%s", (p_hash, email))
            conn.commit()
            
        r.delete(f"reset_otp:{email}")
        return {"success": True, "message": "Password updated successfully. You can now log in."}


def admin_reset_client_password(client_id: str, new_password: str):
    if len(new_password) < 8:
        return {"success": False, "error": "Password must be at least 8 characters"}
    with get_db_ctx() as conn:
        with conn.cursor() as cursor:
            p_hash = hash_password(new_password)
            cursor.execute("UPDATE users SET password_hash=%s WHERE client_id=%s", (p_hash, client_id))
            conn.commit()
        return {"success": True, "message": "Client password updated successfully"}


def set_user_status(client_id: str, status: str):
    if status not in ('active', 'inactive'):
        return {"success": False, "error": "Invalid status"}
    with get_db_ctx() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET status=%s WHERE client_id=%s", (status, client_id))
        conn.commit()
        return {"success": True}


def get_all_users():
    with get_db_ctx() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT id, client_id, email, role, status, created_at 
                FROM users 
                ORDER BY created_at DESC
            """)
            return cursor.fetchall()


def delete_client_account(client_id: str) -> dict:
    with get_db_ctx() as conn:
        with conn.cursor() as cursor:
            # verify client exists
            cursor.execute("SELECT email FROM users WHERE client_id = %s", (client_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "Client not found"}
            email = row[0]

            # delete all MySQL data
            cursor.execute("DELETE FROM celery_task_log WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM chat_history WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM client_llm_config WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM create_payload_table WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM email_accounts WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM email_customers WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM llm_logs WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM paused_emails WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM payload_get_table WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM ticket_record WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM users WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM blocked_keywords WHERE client_id = %s", (client_id,))
            cursor.execute("DELETE FROM reply_blocked_by_keyword WHERE client_id = %s", (client_id,))

        conn.commit()

        # purge Redis
        try:
            import redis as _redis_lib

            # DB 2 — sessions
            r_sessions = _session_redis()
            for key in r_sessions.scan_iter("session:*"):
                raw = r_sessions.get(key)
                if raw:
                    try:
                        data = json.loads(raw)
                        if data.get("client_id") == client_id:
                            r_sessions.delete(key)
                    except Exception:
                        pass

            # DB 0 — reset OTP
            from app.redis_pool import get_redis_main, get_redis_history
            r_main = get_redis_main()
            r_main.delete(f"reset_otp:{email}")

            # DB 1 — chat history
            r_history = get_redis_history()
            for key in r_history.scan_iter(f"chat_history:{client_id}:*"):
                r_history.delete(key)

        except Exception as redis_err:
            print(f"Redis purge error: {redis_err}")

        # purge Qdrant RAG data — single shared collection, filtered delete by client_id
        try:
            from app.vector_store import delete_client_data
            if not delete_client_data(client_id):
                print(f"Qdrant purge returned False for client_id={client_id} (Qdrant may be down — fallback JSON entries, if any, are not purged by this path)")
        except Exception as rag_err:
            print(f"Qdrant purge error: {rag_err}")

        return {"success": True, "message": f"Client {client_id} deleted successfully"}