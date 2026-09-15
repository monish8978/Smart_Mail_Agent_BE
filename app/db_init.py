import os
import asyncio
import logging

from app.url_allowlist import ensure_url_allowlist_table
from app.connector_config import ensure_connector_configs_table
from app.email_disclaimers import ensure_email_disclaimers_table
from app.email_credential import (
    ensure_payload_get_ticket_table,
    ensure_create_payload_table,
    ensure_accounts_table_startup,
    ensure_ticket_record_table,
)
from app.auth import ensure_users_table, ensure_admin_seeded
from app.migrations.init_schema import ensure_action_logs_table

logger = logging.getLogger(__name__)


def preload_qdrant_collection():
    """
    Startup check: ensure shared Qdrant collection exists and embed_service is reachable.
    """
    try:
        from app.vector_store import ensure_collection
        from app.embed_client import embed_service_healthy
        if ensure_collection():
            logger.info("✅ Qdrant collection ensured at startup")
        else:
            logger.warning("⚠️ Qdrant not reachable at API startup — RAG will degrade until it recovers")
        if not embed_service_healthy():
            logger.warning("⚠️ embed_service not reachable at API startup — RAG will degrade until it recovers")
    except Exception as e:
        logger.warning(f"⚠️ Qdrant/embed_service startup check failed: {e}")


def backfill_client_ids():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS email_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50) NULL,
                    from_email VARCHAR(255),
                    subject TEXT,
                    body TEXT,
                    body_html LONGTEXT NULL,
                    reply TEXT,
                    score INT,
                    status VARCHAR(50),
                    rag_id VARCHAR(255),
                    sentiment VARCHAR(50) DEFAULT 'Neutral',
                    priority VARCHAR(50) DEFAULT 'Medium',
                    execution_steps TEXT NULL,
                    summary VARCHAR(255) NULL,
                    message_id VARCHAR(255) NULL,
                    in_reply_to VARCHAR(255) NULL,
                    thread_id VARCHAR(100) NULL,
                    is_resolved TINYINT(1) DEFAULT 0,
                    troubleshooting_step INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email_logs_client_created (client_id, created_at),
                    INDEX idx_email_logs_client_status (client_id, status),
                    INDEX idx_email_logs_msg_id (message_id),
                    INDEX idx_email_logs_thread (client_id, thread_id)
                )
                """)

                cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'email_logs'")
                existing_cols = {row[0] for row in cursor.fetchall()}

                missing_email_log_cols = [
                    ("client_id", "VARCHAR(50) NULL"),
                    ("sentiment", "VARCHAR(50) DEFAULT 'Neutral'"),
                    ("priority", "VARCHAR(50) DEFAULT 'Medium'"),
                    ("execution_steps", "TEXT NULL"),
                    ("summary", "VARCHAR(255) NULL"),
                    ("body_html", "LONGTEXT NULL"),
                    ("message_id", "VARCHAR(255) NULL"),
                    ("in_reply_to", "VARCHAR(255) NULL"),
                    ("thread_id", "VARCHAR(100) NULL"),
                    ("is_resolved", "TINYINT(1) DEFAULT 0"),
                    ("troubleshooting_step", "INT DEFAULT 0"),
                ]
                for col_name, col_def in missing_email_log_cols:
                    if col_name not in existing_cols:
                        cursor.execute(f"ALTER TABLE email_logs ADD COLUMN {col_name} {col_def}")
                
                # Check indexes
                cursor.execute("SHOW INDEX FROM email_logs")
                existing_indexes = {row[2] for row in cursor.fetchall()}
                if "idx_email_logs_msg_id" not in existing_indexes:
                    try:
                        cursor.execute("ALTER TABLE email_logs ADD INDEX idx_email_logs_msg_id (message_id)")
                    except Exception:
                        pass
                if "idx_email_logs_thread" not in existing_indexes:
                    try:
                        cursor.execute("ALTER TABLE email_logs ADD INDEX idx_email_logs_thread (client_id, thread_id)")
                    except Exception:
                        pass
                
                cursor.execute("SELECT id, rag_id FROM email_logs WHERE client_id IS NULL AND rag_id IS NOT NULL")
                rows = cursor.fetchall()
                for row in rows:
                    log_id, rag_id = row
                    if rag_id and rag_id.startswith("client_"):
                        parts = rag_id.split("_")
                        if len(parts) >= 2:
                            raw_id = parts[1].upper()
                            if len(parts) > 2:
                                raw_id = raw_id + "-" + "-".join(parts[2:]).upper()
                            cursor.execute("UPDATE email_logs SET client_id = %s WHERE id = %s", (raw_id, log_id))
                db.commit()
                logger.info("✅ Backfilled existing email_logs client_ids successfully")
    except Exception as e:
        logger.warning(f"⚠️ Backfill client_ids failed: {e}")


def _run_ensure_paused_email_history_table():
    from app.db import get_db_ctx
    from app.paused_email_history import ensure_paused_email_history_table
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            ensure_paused_email_history_table(cursor)
        db.commit()
    logger.info("✅ paused_email_history table ensured")


def ensure_paused_emails_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS paused_emails (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50),
                    paused_email VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(client_id, paused_email)
                )
                """)
                db.commit()
                logger.info("✅ Ensured paused_emails table exists")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure paused_emails table: {e}")


def ensure_global_llm_tables():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS global_default_llm (
                    id INT PRIMARY KEY DEFAULT 1,
                    provider VARCHAR(50) NOT NULL DEFAULT 'groq',
                    api_key TEXT NOT NULL,
                    base_url VARCHAR(255) NULL,
                    model_name VARCHAR(255) NOT NULL DEFAULT '',
                    api_version VARCHAR(50) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    refreshed TIMESTAMP NULL,
                    is_override_active TINYINT(1) NOT NULL DEFAULT 0
                )
                """)

                cursor.execute("""
                    SELECT COUNT(*) FROM information_schema.columns 
                    WHERE table_schema = DATABASE() AND table_name = 'global_default_llm' AND column_name = 'is_override_active'
                """)
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE global_default_llm ADD COLUMN is_override_active TINYINT(1) NOT NULL DEFAULT 0")
                    logger.info("✅ Added is_override_active column to global_default_llm")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS globally_available_llm_configs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    provider VARCHAR(50) NOT NULL,
                    api_key TEXT NOT NULL,
                    base_url VARCHAR(255) NULL,
                    model_name VARCHAR(255) NOT NULL DEFAULT '',
                    api_version VARCHAR(50) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    refreshed TIMESTAMP NULL
                )
                """)

                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_schema = DATABASE() AND table_name IN ('default_global_llm_config', 'llm_configs')
                """)
                legacy_exists = cursor.fetchone()[0] > 0

                cursor.execute("SELECT COUNT(*) FROM global_default_llm WHERE id=1")
                if cursor.fetchone()[0] == 0:
                    migrated_default = False
                    if legacy_exists:
                        try:
                            cursor.execute("""
                                SELECT provider, api_key, base_url, model_name, api_version, refreshed 
                                FROM default_global_llm_config 
                                WHERE client_id='SYSTEM' 
                                ORDER BY id ASC LIMIT 1
                            """)
                            row = cursor.fetchone()
                            if not row:
                                cursor.execute("SELECT provider, api_key, base_url, model_name, api_version, refreshed FROM default_global_llm_config ORDER BY id ASC LIMIT 1")
                                row = cursor.fetchone()
                            if row:
                                cursor.execute("""
                                    INSERT INTO global_default_llm (id, provider, api_key, base_url, model_name, api_version, refreshed)
                                    VALUES (1, %s, %s, %s, %s, %s, %s)
                                """, row)
                                migrated_default = True
                                logger.info("✅ Migrated system default into global_default_llm (id=1)")
                        except Exception as e:
                            logger.warning(f"Failed to migrate default from legacy table: {e}")

                    if not migrated_default:
                        default_groq_key = os.getenv("GROQ_API_KEY", "").strip()
                        default_groq_model = os.getenv("GROQ_MODEL", "").strip()
                        cursor.execute("""
                            INSERT INTO global_default_llm (id, provider, api_key, base_url, model_name)
                            VALUES (1, 'groq', %s, 'https://api.groq.com/openai/v1', %s)
                            ON DUPLICATE KEY UPDATE provider=VALUES(provider)
                        """, (default_groq_key, default_groq_model))
                        logger.info("✅ Seeded global_default_llm from environment variables")

                cursor.execute("SELECT COUNT(*) FROM globally_available_llm_configs")
                if cursor.fetchone()[0] == 0 and legacy_exists:
                    try:
                        cursor.execute("""
                            SELECT id, name, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed 
                            FROM default_global_llm_config
                        """)
                        legacy_rows = cursor.fetchall()
                        for r in legacy_rows:
                            cursor.execute("""
                                INSERT INTO globally_available_llm_configs (id, name, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """, r)
                        logger.info(f"✅ Migrated {len(legacy_rows)} configs into globally_available_llm_configs")
                    except Exception as e:
                        logger.warning(f"Failed to migrate legacy rows to globally_available_llm_configs: {e}")

                if legacy_exists:
                    try:
                        cursor.execute("DROP TABLE IF EXISTS default_global_llm_config")
                        logger.info("🗑️ Safely dropped legacy table default_global_llm_config")
                    except Exception as e:
                        logger.warning(f"Failed to drop legacy table default_global_llm_config: {e}")

                # --- Encrypt any plaintext LLM API keys ---
                try:
                    from app.secrets_crypto import encrypt_secret
                    cursor.execute("SELECT id, api_key FROM global_default_llm WHERE api_key IS NOT NULL AND api_key != ''")
                    for r_id, r_key in cursor.fetchall():
                        if r_key and not r_key.startswith("gAAAAA"):
                            cursor.execute("UPDATE global_default_llm SET api_key = %s WHERE id = %s", (encrypt_secret(r_key), r_id))
                    cursor.execute("SELECT id, api_key FROM globally_available_llm_configs WHERE api_key IS NOT NULL AND api_key != ''")
                    for r_id, r_key in cursor.fetchall():
                        if r_key and not r_key.startswith("gAAAAA"):
                            cursor.execute("UPDATE globally_available_llm_configs SET api_key = %s WHERE id = %s", (encrypt_secret(r_key), r_id))
                except Exception as e:
                    logger.warning(f"⚠️ Failed to migrate plaintext LLM API keys: {e}")

                db.commit()
                logger.info("✅ Ensured global_default_llm and globally_available_llm_configs tables exist")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure global LLM tables: {e}")


def ensure_client_llm_config_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_schema = DATABASE() AND table_name = 'client_model_config'
                """)
                legacy_exists = cursor.fetchone()[0] > 0

                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_schema = DATABASE() AND table_name = 'client_llm_config'
                """)
                new_exists = cursor.fetchone()[0] > 0

                if legacy_exists and not new_exists:
                    logger.info("🔄 Migrating table client_model_config -> client_llm_config...")
                    cursor.execute("RENAME TABLE client_model_config TO client_llm_config")
                    db.commit()

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_llm_config (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    caller_function VARCHAR(100) NOT NULL,
                    global_config_id INT NULL,
                    provider VARCHAR(50) NULL,
                    api_key TEXT NULL,
                    base_url VARCHAR(255) NULL,
                    model_name VARCHAR(255) NOT NULL DEFAULT '',
                    api_version VARCHAR(50) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    refreshed TIMESTAMP NULL,
                    UNIQUE KEY uk_client_caller (client_id, caller_function)
                )
                """)

                for col_name, col_type in [
                    ("global_config_id", "INT NULL"),
                    ("provider", "VARCHAR(50) NULL"),
                    ("api_key", "TEXT NULL"),
                    ("base_url", "VARCHAR(255) NULL"),
                    ("api_version", "VARCHAR(50) NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
                    ("refreshed", "TIMESTAMP NULL")
                ]:
                    cursor.execute(f"""
                        SELECT COUNT(*) 
                        FROM information_schema.columns 
                        WHERE table_schema = DATABASE() 
                          AND table_name = 'client_llm_config' 
                          AND column_name = '{col_name}'
                    """)
                    if cursor.fetchone()[0] == 0:
                        cursor.execute(f"ALTER TABLE client_llm_config ADD COLUMN {col_name} {col_type}")

                try:
                    cursor.execute("ALTER TABLE client_llm_config MODIFY COLUMN model_name VARCHAR(255) NOT NULL DEFAULT ''")
                except Exception:
                    pass

                # --- Encrypt any plaintext client LLM API keys ---
                try:
                    from app.secrets_crypto import encrypt_secret
                    cursor.execute("SELECT client_id, caller_function, api_key FROM client_llm_config WHERE api_key IS NOT NULL AND api_key != ''")
                    for c_id, c_fn, r_key in cursor.fetchall():
                        if r_key and not r_key.startswith("gAAAAA"):
                            cursor.execute("UPDATE client_llm_config SET api_key = %s WHERE client_id = %s AND caller_function = %s", (encrypt_secret(r_key), c_id, c_fn))
                except Exception as e:
                    logger.warning(f"⚠️ Failed to migrate plaintext client LLM API keys: {e}")

                db.commit()
                logger.info("✅ Ensured client_llm_config table exists with refreshed column")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure client_llm_config table: {e}")


def _run_ensure_accounts_table():
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            ensure_accounts_table_startup(cursor)
        db.commit()
    logger.info("✅ email_accounts table ensured at startup")


def _run_ensure_draft_emails_table():
    from app.draft_service import ensure_draft_emails_table
    ensure_draft_emails_table()
    logger.info("✅ draft_emails table ensured at startup")


def ensure_llm_logs_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(50) NOT NULL,
                    provider VARCHAR(50) NOT NULL DEFAULT 'groq',
                    model_name VARCHAR(100) NOT NULL,
                    prompt_tokens INT NOT NULL,
                    completion_tokens INT NOT NULL,
                    cost DECIMAL(10, 6) NOT NULL,
                    billed_cost DECIMAL(10, 6) DEFAULT NULL,
                    latency_ms INT NOT NULL,
                    caller_function VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN provider VARCHAR(50) NOT NULL DEFAULT 'groq'")
                except Exception:
                    pass
                try:
                    cursor.execute("ALTER TABLE llm_logs ADD COLUMN billed_cost DECIMAL(10, 6) DEFAULT NULL")
                except Exception:
                    pass
                db.commit()
        logger.info("✅ llm_logs table ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure llm_logs table: {e}")


def _run_ensure_chat_history_table():
    try:
        from app.db import get_db_ctx
        from app.chat_history import ensure_chat_history_table
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                ensure_chat_history_table(cursor)
            db.commit()
        logger.info("✅ chat_history table ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure chat_history table: {e}")


def _run_ensure_keyword_filter_tables():
    try:
        from app.db import get_db_ctx
        from app.keyword_filter import ensure_keyword_filter_tables
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                ensure_keyword_filter_tables(cursor)
            db.commit()
        logger.info("✅ keyword_filter tables ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure keyword_filter tables: {e}")


def _run_ensure_ticket_record_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                ensure_ticket_record_table(cursor)
            db.commit()
        logger.info("✅ ticket_record table ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure ticket_record table: {e}")


def _run_ensure_marketing_senders_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS marketing_senders (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        client_id VARCHAR(50) NOT NULL,
                        sender_email VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY unique_client_sender (client_id, sender_email)
                    )
                """)
            db.commit()
        logger.info("✅ marketing_senders table ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure marketing_senders table: {e}")


def _run_ensure_worker_tables():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS celery_task_log (
                    task_id     VARCHAR(255) NOT NULL PRIMARY KEY,
                    client_id   VARCHAR(50)  NOT NULL,
                    from_email  VARCHAR(255) NOT NULL,
                    status      VARCHAR(50)  NOT NULL DEFAULT 'processing',
                    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS email_customers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id VARCHAR(255) UNIQUE,
                    rag_id VARCHAR(255),
                    customer_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
            db.commit()
        logger.info("✅ celery_task_log and email_customers tables ensured at startup")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure worker tables: {e}")


async def initialize_database_and_services():
    """
    Runs all startup table migrations, seedings, and vector store preloading in parallel batches.
    """
    logger.info("🚀 Starting database and service initialization...")

    # Batch 1: Independent schema creations executed concurrently
    await asyncio.gather(
        asyncio.to_thread(ensure_url_allowlist_table),
        asyncio.to_thread(ensure_connector_configs_table),
        asyncio.to_thread(_run_ensure_draft_emails_table),
        asyncio.to_thread(_run_ensure_accounts_table),
        asyncio.to_thread(ensure_create_payload_table),
        asyncio.to_thread(ensure_payload_get_ticket_table),
        asyncio.to_thread(ensure_users_table),
        asyncio.to_thread(ensure_paused_emails_table),
        asyncio.to_thread(ensure_global_llm_tables),
        asyncio.to_thread(ensure_client_llm_config_table),
        asyncio.to_thread(ensure_email_disclaimers_table),
        asyncio.to_thread(ensure_llm_logs_table),
        asyncio.to_thread(_run_ensure_chat_history_table),
        asyncio.to_thread(_run_ensure_keyword_filter_tables),
        asyncio.to_thread(_run_ensure_ticket_record_table),
        asyncio.to_thread(_run_ensure_marketing_senders_table),
        asyncio.to_thread(_run_ensure_worker_tables),
        asyncio.to_thread(_run_ensure_paused_email_history_table),
        asyncio.to_thread(ensure_action_logs_table),
    )

    # Batch 2: Admin seeding (depends on users table)
    await asyncio.to_thread(ensure_admin_seeded)

    # Batch 3: External service preloading & client backfilling
    await asyncio.gather(
        asyncio.to_thread(preload_qdrant_collection),
        asyncio.to_thread(backfill_client_ids),
    )

    logger.info("✅ Database and service initialization complete!")

