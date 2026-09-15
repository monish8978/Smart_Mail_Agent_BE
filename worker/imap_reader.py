import imaplib
import email
import os
import re
import time
import logging
import sys
import threading
import signal
import concurrent.futures
from email.header import decode_header as _decode_header

# Ensure /app is on sys.path when running the script directly
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.db import get_db
from worker.tasks import process_email_task

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configurable concurrency and polling limits
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_MAX_WORKERS = int(os.getenv("IMAP_MAX_WORKERS", "5"))
IMAP_POLL_INTERVAL = float(os.getenv("IMAP_POLL_INTERVAL", "10.0"))
IMAP_SOCKET_TIMEOUT = float(os.getenv("IMAP_SOCKET_TIMEOUT", "20.0"))
IMAP_AUTH_COOLDOWN = float(os.getenv("IMAP_AUTH_COOLDOWN", "300.0"))

shutdown_event = threading.Event()

# Cooldown tracking for failing accounts (e.g. invalid credentials)
_account_cooldowns: dict[str, float] = {}
_cooldowns_lock = threading.Lock()


def handle_shutdown(signum, frame):
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info(f"🛑 Received {sig_name} ({signum}), initiating graceful shutdown...")
    shutdown_event.set()


def is_in_cooldown(client_id: str) -> bool:
    """Returns True if this client account is currently in error/auth cooldown."""
    with _cooldowns_lock:
        expiry = _account_cooldowns.get(client_id, 0)
        if expiry and time.time() < expiry:
            return True
        _account_cooldowns.pop(client_id, None)
        return False


def set_account_cooldown(client_id: str, duration: float = IMAP_AUTH_COOLDOWN):
    """Sets an error cooldown timestamp for a client account."""
    with _cooldowns_lock:
        _account_cooldowns[client_id] = time.time() + duration


def clear_account_cooldown(client_id: str):
    """Clears any active error cooldown for a client account."""
    with _cooldowns_lock:
        _account_cooldowns.pop(client_id, None)


def decode_subject(raw_subject: str) -> str:
    """Decode MIME-encoded email subjects to plain text."""
    try:
        parts = _decode_header(raw_subject)
        decoded = []
        for part, encoding in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                decoded.append(part)
        return " ".join(decoded)
    except Exception:
        return raw_subject


def is_bot_enabled_for_client(client_id: str) -> tuple[bool, str]:
    """Check if bot automation is enabled for this client (both admin and client level)."""
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        COALESCE(admin_bot_enabled, 1),
                        COALESCE(client_bot_enabled, 1)
                    FROM email_accounts WHERE client_id = %s LIMIT 1
                """, (client_id,))
                row = cursor.fetchone()
        if not row:
            return True, "Enabled (Default)"
        admin_en = bool(row[0])
        client_en = bool(row[1])
        if not admin_en:
            return False, "Disabled by Administrator"
        if not client_en:
            return False, "Paused by Client"
        return True, "Active"
    except Exception as e:
        logger.warning(f"⚠️ Error checking bot status for client {client_id}: {e}")
        return True, "Active (Fallback)"


def is_message_duplicate(client_id: str, message_id: str) -> bool:
    """
    Checks if this Message-ID has already been queued for this client in the last 24h.
    Returns True if duplicate, False if new.
    Falls back to False on Redis failure so emails are never dropped.
    """
    if not message_id or not message_id.strip():
        return False
    try:
        from app.redis_pool import get_redis_main
        r = get_redis_main()
        key = f"imap_dedup:{client_id}:{message_id.strip()}"
        was_set = r.set(key, "1", nx=True, ex=86400)
        return not bool(was_set)
    except Exception as e:
        logger.warning(f"⚠️ Redis deduplication check failed for {client_id}: {e} (proceeding without dedup)")
        return False


def check_client_mailbox(client_id: str, email_user: str, email_pass: str) -> int:
    """
    Connects to the client's IMAP mailbox, processes any unseen emails,
    enqueues Celery tasks, and cleanly logs out.
    Returns count of emails successfully queued.
    """
    if shutdown_event.is_set():
        return 0

    # Master Bot Switch Check — Complete Stop
    bot_enabled, reason = is_bot_enabled_for_client(client_id)
    if not bot_enabled:
        logger.info(f"🛑 [Client {client_id}] Master Bot Switch is OFF ({reason}) — skipping inbox check")
        return 0

    if not email_user or not email_pass:
        logger.debug(f"⏭️ [Client {client_id}] Incomplete credentials (missing user or password) — skipping inbox check")
        return 0

    if is_in_cooldown(client_id):
        logger.debug(f"⏳ [Client {client_id}] Skipping inbox check (account in cooldown)")
        return 0

    mail = None
    queued_count = 0
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_SOCKET_TIMEOUT)
        try:
            mail.login(email_user, email_pass)
            clear_account_cooldown(client_id)
        except imaplib.IMAP4.error as auth_err:
            err_str = str(auth_err)
            if 'AUTHENTICATIONFAILED' in err_str or 'Invalid credentials' in err_str:
                logger.error(
                    f"❌ [Client {client_id}] Authentication failed for {email_user}. "
                    f"Setting {IMAP_AUTH_COOLDOWN}s cooldown: {auth_err}"
                )
                set_account_cooldown(client_id)
                return 0
            raise

        mail.select("inbox")
        status, messages = mail.search(None, 'UNSEEN')
        if status == "OK" and messages and messages[0]:
            nums = messages[0].split()
            logger.info(f"📬 [Client {client_id}] Found {len(nums)} unseen email(s)")

            for num in nums:
                if shutdown_event.is_set():
                    break
                try:
                    f_status, data = mail.fetch(num, "(RFC822)")
                    if f_status != "OK" or not data:
                        continue

                    raw_email = next((item[1] for item in data if isinstance(item, tuple)), None)
                    if raw_email is None:
                        continue

                    msg = email.message_from_bytes(raw_email)

                    raw_message_id = msg.get("Message-ID", "")
                    raw_in_reply_to = msg.get("In-Reply-To", "")
                    raw_references = msg.get("References", "")

                    # Deduplication check: if seen within 24h, mark seen and skip
                    if raw_message_id and is_message_duplicate(client_id, raw_message_id):
                        logger.warning(f"⏩ [Client {client_id}] Duplicate message {raw_message_id} already queued — marking \\Seen and skipping")
                        mail.store(num, '+FLAGS', '\\Seen')
                        continue

                    subject = decode_subject(msg.get("subject", ""))
                    from_email = msg.get("from", "")

                    # Extract actual email address
                    match = re.search(r'<([^>]+)>', from_email)
                    if match:
                        from_email = match.group(1)
                    else:
                        match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', from_email)
                        if match:
                            from_email = match.group(0)

                    plain_text = ""
                    html_text = ""

                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            if ctype == "text/plain" and not plain_text:
                                try:
                                    plain_text = part.get_payload(decode=True).decode(errors="ignore")
                                except Exception:
                                    pass
                            elif ctype == "text/html" and not html_text:
                                try:
                                    html_text = part.get_payload(decode=True).decode(errors="ignore")
                                except Exception:
                                    pass
                    else:
                        try:
                            payload = msg.get_payload(decode=True).decode(errors="ignore")
                        except Exception:
                            payload = ""
                        if msg.get_content_type() == "text/html":
                            html_text = payload
                        else:
                            plain_text = payload

                    from app.text_cleaning import extract_clean_text_from_html, is_html_content

                    if not plain_text and html_text:
                        plain_text = extract_clean_text_from_html(html_text)
                    elif plain_text and is_html_content(plain_text):
                        if not html_text:
                            html_text = plain_text
                        plain_text = extract_clean_text_from_html(plain_text)

                    logger.info(f"📧 [Client {client_id}] NEW EMAIL: From={from_email} Subject={subject} (HTML={bool(html_text)}) [In-Reply-To={raw_in_reply_to}]")

                    # Enqueue task to Celery FIRST
                    task_result = process_email_task.delay({
                        "client_id": client_id,
                        "from_email": from_email,
                        "subject": subject,
                        "body": plain_text or "",
                        "body_html": html_text or "",
                        "message_id": raw_message_id,
                        "in_reply_to": raw_in_reply_to,
                        "references": raw_references,
                    })
                    logger.info(f"✅ [Client {client_id}] Task queued: {task_result.id}")

                    # ONLY mark \Seen once task is successfully queued
                    mail.store(num, '+FLAGS', '\\Seen')
                    queued_count += 1

                except Exception as email_err:
                    num_str = num.decode() if isinstance(num, bytes) else str(num)
                    logger.error(f"❌ [Client {client_id}] Error processing email #{num_str} (left UNSEEN for retry): {email_err}", exc_info=True)

    except Exception as conn_err:
        logger.error(f"⚠️ [Client {client_id}] IMAP sweep error: {conn_err}")
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

    return queued_count


def poll_inbox(client_id, email_user, email_pass, stop_event=None):
    """Backward-compatible single-mailbox check wrapper."""
    return check_client_mailbox(client_id, email_user, email_pass)


def fetch_db_accounts():
    """Queries all email accounts from the database."""
    try:
        from app.db import get_db_ctx
        from app.email_credential import _decrypt_imap_password
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT client_id, email, password FROM email_accounts")
                rows = cursor.fetchall()
                return {row[0]: {"email": row[1], "password": _decrypt_imap_password(row[2], client_id=row[0])} for row in rows}
    except Exception as e:
        logger.error(f"Failed to fetch accounts from DB: {e}", exc_info=True)
        return {}


def manage_listeners():
    """
    Bounded concurrent listener loop.
    Sweeps active client mailboxes using a fixed ThreadPoolExecutor pool,
    preventing IMAP provider connection throttling and persistent socket leaks.
    """
    try:
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    except (ValueError, AttributeError):
        pass  # In case called from non-main thread

    logger.info(f"🚀 Starting Bounded Email Listener Manager (max_workers={IMAP_MAX_WORKERS}, poll_interval={IMAP_POLL_INTERVAL}s)...")
    
    try:
        from app.email_credential import ensure_accounts_table_startup
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                ensure_accounts_table_startup(cursor)
            db.commit()
    except Exception as e:
        logger.warning(f"⚠️ Initial accounts table check warning: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=IMAP_MAX_WORKERS, thread_name_prefix="imap_worker") as executor:
        while not shutdown_event.is_set():
            db_accounts = fetch_db_accounts()

            # Clean up cooldowns for accounts deleted from DB
            with _cooldowns_lock:
                stale_keys = [cid for cid in _account_cooldowns if cid not in db_accounts]
                for cid in stale_keys:
                    _account_cooldowns.pop(cid, None)

            if db_accounts:
                futures = {
                    executor.submit(check_client_mailbox, cid, info["email"], info["password"]): cid
                    for cid, info in db_accounts.items()
                    if not shutdown_event.is_set()
                }

                for f in concurrent.futures.as_completed(futures):
                    if shutdown_event.is_set():
                        break
                    cid = futures[f]
                    try:
                        f.result()
                    except Exception as exc:
                        logger.warning(f"⚠️ [Client {cid}] Mailbox sweep task failed: {exc}")

            shutdown_event.wait(IMAP_POLL_INTERVAL)

    logger.info("👋 Bounded listener shutdown complete.")


if __name__ == "__main__":
    manage_listeners()