import logging
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

def ensure_email_disclaimers_table():
    """
    Ensures the client_email_disclaimers table exists and is seeded with initial defaults if empty.
    """
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_email_disclaimers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50) NOT NULL,
                    disclaimer_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_client (client_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                
                # Check if seeded with standard defaults for SYSTEM/GLOBAL
                cursor.execute("SELECT COUNT(*) FROM client_email_disclaimers WHERE client_id='GLOBAL'")
                if cursor.fetchone()[0] == 0:
                    default_disclaimers = [
                        "DISCLAIMER: This email and its attachments are confidential and intended solely for the recipient(s). Unauthorized use, disclosure, or distribution is prohibited. If you received this email in error, please notify the sender and delete it.",
                        "Towards Vision Technologies Limited is not liable for any damage caused by viruses or malware in this email.",
                        "Confidentiality Notice: This e-mail message, including any attachments, is for the sole use of the intended recipient(s) and may contain confidential and privileged information."
                    ]
                    for d in default_disclaimers:
                        cursor.execute("""
                        INSERT INTO client_email_disclaimers (client_id, disclaimer_text, is_active)
                        VALUES ('GLOBAL', %s, TRUE)
                        """, (d,))
                    logger.info("✅ Seeded default GLOBAL email disclaimers")

                db.commit()
                logger.info("✅ Ensured client_email_disclaimers table exists")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure client_email_disclaimers table: {e}")

def get_client_disclaimers(client_id: str):
    """
    Retrieves all active and configured disclaimer strings for a client,
    plus active GLOBAL defaults.
    """
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if client_id == "ALL":
                    cursor.execute("""
                    SELECT id, client_id, disclaimer_text, is_active, created_at, updated_at
                    FROM client_email_disclaimers
                    ORDER BY client_id ASC, id DESC
                    """)
                else:
                    cursor.execute("""
                    SELECT id, client_id, disclaimer_text, is_active, created_at, updated_at
                    FROM client_email_disclaimers
                    WHERE client_id = %s OR client_id = 'GLOBAL'
                    ORDER BY (client_id = %s) DESC, id DESC
                    """, (client_id, client_id))
                
                rows = cursor.fetchall()
                result = []
                for r in rows:
                    result.append({
                        "id": r[0],
                        "client_id": r[1],
                        "disclaimer_text": r[2],
                        "is_active": bool(r[3]),
                        "created_at": r[4].strftime("%Y-%m-%d %H:%M:%S") if r[4] else None,
                        "updated_at": r[5].strftime("%Y-%m-%d %H:%M:%S") if r[5] else None
                    })
                return result
    except Exception as e:
        logger.error(f"Failed to get client disclaimers for {client_id}: {e}")
        return []

def add_client_disclaimer(client_id: str, disclaimer_text: str) -> int:
    """
    Adds a new disclaimer configuration entry for a client.
    """
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
            INSERT INTO client_email_disclaimers (client_id, disclaimer_text, is_active)
            VALUES (%s, %s, TRUE)
            """, (client_id, disclaimer_text.strip()))
            new_id = cursor.lastrowid
            db.commit()
            return new_id

def delete_client_disclaimer(disclaimer_id: int, client_id: str = None) -> bool:
    """
    Deletes a disclaimer entry.
    """
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if client_id and client_id != "SYSTEM":
                cursor.execute("DELETE FROM client_email_disclaimers WHERE id = %s AND client_id = %s", (disclaimer_id, client_id))
            else:
                cursor.execute("DELETE FROM client_email_disclaimers WHERE id = %s", (disclaimer_id,))
            db.commit()
            return cursor.rowcount > 0

def toggle_client_disclaimer(disclaimer_id: int, is_active: bool, client_id: str = None) -> bool:
    """
    Toggles is_active status of a disclaimer entry.
    """
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if client_id and client_id != "SYSTEM":
                cursor.execute("UPDATE client_email_disclaimers SET is_active = %s WHERE id = %s AND client_id = %s", (is_active, disclaimer_id, client_id))
            else:
                cursor.execute("UPDATE client_email_disclaimers SET is_active = %s WHERE id = %s", (is_active, disclaimer_id))
            db.commit()
            return cursor.rowcount > 0


def get_active_disclaimer_texts(client_id: str) -> list[str]:
    """
    Returns a list of active disclaimer strings for a specific client_id and global defaults.
    """
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                SELECT disclaimer_text
                FROM client_email_disclaimers
                WHERE (client_id = %s OR client_id = 'GLOBAL') AND is_active = TRUE
                ORDER BY (client_id = %s) DESC, id DESC
                """, (client_id, client_id))
                rows = cursor.fetchall()
                return [r[0].strip() for r in rows if r[0] and r[0].strip()]
    except Exception as e:
        logger.error(f"Failed to fetch active disclaimer texts for {client_id}: {e}")
        return []
