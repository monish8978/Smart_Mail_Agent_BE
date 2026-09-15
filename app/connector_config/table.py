import logging
from app.db import get_db

logger = logging.getLogger(__name__)


def ensure_connector_configs_table():
    """
    One-time startup call, mirrors ensure_accounts_table_startup /
    _ensure_table conventions elsewhere in this codebase.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS connector_configs (
                    id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
                    client_id             VARCHAR(50) NOT NULL,
                    trigger_type          VARCHAR(100) NOT NULL,
                    http_method           VARCHAR(10) NOT NULL,
                    url                   VARCHAR(500) NOT NULL,
                    headers_template      JSON,
                    request_template      JSON,
                    response_mapping      JSON,
                    auth_type             ENUM('bearer','basic','api_key_header','api_key_query','oauth2_client_credentials') NOT NULL,
                    auth_secret_encrypted TEXT,
                    auth_field_name       VARCHAR(100) NULL,
                    payload_encoding        ENUM('plain','base64_query') NOT NULL DEFAULT 'plain',
                    base64_query_param_name VARCHAR(50) NULL,
                    status                ENUM('draft','pending_approval','live','disabled','pending_deletion') NOT NULL DEFAULT 'draft',
                    version               INT NOT NULL DEFAULT 1,
                    created_by            VARCHAR(50),
                    approved_by           VARCHAR(50) NULL,
                    approved_at           TIMESTAMP NULL,
                    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    live_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'live' THEN trigger_type ELSE NULL END) STORED,
                    pending_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'pending_approval' THEN trigger_type ELSE NULL END) STORED,
                    UNIQUE INDEX uq_client_live_trigger (client_id, live_marker),
                    UNIQUE INDEX uq_client_pending_trigger (client_id, pending_marker),
                    INDEX idx_client_status (client_id, status),
                    INDEX idx_trigger_type (trigger_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # pending_marker / uq_client_pending_trigger — kept here for fresh installs
            # where CREATE TABLE IF NOT EXISTS above won't have run yet.
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    ADD COLUMN pending_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'pending_approval' THEN trigger_type ELSE NULL END) STORED
                """)
            except Exception:
                pass

            try:
                cursor.execute(
                    "ALTER TABLE connector_configs ADD UNIQUE INDEX uq_client_pending_trigger (client_id, pending_marker)"
                )
            except Exception:
                pass

            # OAuth 2.0 ENUM upgrade migration for existing tables
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    MODIFY COLUMN auth_type ENUM('bearer','basic','api_key_header','api_key_query','oauth2_client_credentials') NOT NULL
                """)
            except Exception:
                pass

            # Pending Deletion ENUM upgrade migration for existing tables
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    MODIFY COLUMN status ENUM('draft','pending_approval','live','disabled','pending_deletion') NOT NULL DEFAULT 'draft'
                """)
            except Exception:
                pass

        conn.commit()
        logger.info("✅ connector_configs table ensured")
    except Exception as e:
        logger.error(f"❌ Failed to ensure connector_configs table: {e}", exc_info=True)
        raise
    finally:
        conn.close()
