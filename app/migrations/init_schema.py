import logging
from app.db import get_db_ctx

logger = logging.getLogger(__name__)


def ensure_action_logs_table():
    """
    Creates action_logs table for the Idempotent Action Outbox.
    Ensures external CRM and SMTP actions cannot be duplicated upon worker task retries.
    """
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS action_logs (
                    idempotency_key VARCHAR(255) PRIMARY KEY,
                    client_id       VARCHAR(50)  NOT NULL,
                    action_type     VARCHAR(50)  NOT NULL,
                    status          VARCHAR(20)  NOT NULL, -- 'pending', 'completed', 'failed'
                    external_ref    VARCHAR(255) NULL,
                    error_message   TEXT         NULL,
                    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_client_action (client_id, action_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
            db.commit()
            logger.info("✅ Ensured action_logs table exists")
    except Exception as e:
        logger.error(f"❌ Failed to ensure action_logs table: {e}")
        raise


def init_all_tables():
    """
    Consolidated master schema initializer executed at API startup lifespan.
    Centralizes all DDL so worker tasks never run DDL in runtime.
    """
    from app.url_allowlist import ensure_url_allowlist_table
    from app.connector_config import ensure_connector_configs_table
    from app.email_disclaimers import ensure_email_disclaimers_table
    from app.paused_email_history import ensure_paused_email_history_table
    from app.auth import ensure_users_table, ensure_admin_seeded
    from app.email_credential import (
        ensure_create_payload_table,
        ensure_payload_get_ticket_table,
    )

    logger.info("🛠️ Initializing all database schemas and tables...")
    
    # 1. Allowlist and Connectors
    ensure_url_allowlist_table()
    ensure_connector_configs_table()
    
    # 2. Users and Auth
    ensure_users_table()
    ensure_admin_seeded()

    # 3. Action logs & Outbox
    ensure_action_logs_table()

    # 4. Disclaimers & Paused Emails History
    ensure_email_disclaimers_table()
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            ensure_paused_email_history_table(cursor)
        db.commit()

    # 5. Legacy tables (if still required)
    ensure_create_payload_table()
    ensure_payload_get_ticket_table()

    logger.info("✅ Database schema initialization completed successfully.")
