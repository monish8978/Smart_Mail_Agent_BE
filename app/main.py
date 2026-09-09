from app.url_allowlist import ensure_url_allowlist_table
from app.connector_config import ensure_connector_configs_table
from app.email_disclaimers import (
    ensure_email_disclaimers_table,
    get_client_disclaimers,
    add_client_disclaimer,
    delete_client_disclaimer,
    toggle_client_disclaimer
)

from fastapi import FastAPI, HTTPException, File, UploadFile, Form, Depends, Header, Request
from pydantic import BaseModel, EmailStr, field_validator
from worker.tasks import process_email_task
from app.email_credential import ensure_payload_get_ticket_table, insert_payload_get_ticket, save_email_account, get_email_account, create_email_record_db, ensure_create_payload_table, insert_create_payload_ticket, get_create_payload_table, get_payload_get_ticket_table, get_all_create_payloads, get_all_get_payloads
from app.order_routes import get_order_by_id
from app.auth import ensure_users_table, ensure_admin_seeded, login_user #,register_user
from app.rate_limiter import RedisRateLimiter
from enum import Enum
from contextlib import asynccontextmanager
from typing import Any, Optional, List, Dict, Union
import asyncio
import logging

from app.auth_deps import get_current_user, require_admin, require_client_access
from fastapi import Depends

logger = logging.getLogger(__name__)


def preload_qdrant_collection():
    """
    Startup equivalent of the old Chroma warmup — here it just means
    "make sure the shared collection exists" and log whether embed_service
    is reachable. There's no per-request model warmup to do on this side
    anymore; that lives entirely in embed_service's own startup.
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email_logs_client_created (client_id, created_at),
                    INDEX idx_email_logs_client_status (client_id, status)
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
                ]
                for col_name, col_def in missing_email_log_cols:
                    if col_name not in existing_cols:
                        cursor.execute(f"ALTER TABLE email_logs ADD COLUMN {col_name} {col_def}")
                
                cursor.execute("SELECT id, rag_id FROM email_logs WHERE client_id IS NULL AND rag_id IS NOT NULL")
                rows = cursor.fetchall()
                for row in rows:
                    log_id, rag_id = row
                    if rag_id.startswith("client_"):
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

from fastapi import WebSocket, WebSocketDisconnect
import os

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Active connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Active connections: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"WebSocket send failed: {e}")

manager = ConnectionManager()

async def redis_pubsub_listener(app: FastAPI):
    import redis.asyncio as async_redis
    redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0")
    if not redis_url:
        redis_url = "redis://localhost:6379/0"
    
    while True:
        try:
            logger.info(f"Connecting to Redis pub/sub at {redis_url}...")
            r = async_redis.from_url(redis_url, decode_responses=True, socket_timeout=None)
            pubsub = r.pubsub()
            await pubsub.subscribe("email_updates")
            logger.info("Subscribed to Redis channel 'email_updates'")
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    logger.info(f"Broadcasting Redis pub/sub event: {data}")
                    await manager.broadcast(data)
        except asyncio.CancelledError:
            logger.info("Redis pubsub listener cancelled")
            break
        except Exception as e:
            logger.error(f"Redis pubsub error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

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
                # 1. Create global_default_llm (single-row platform fallback default)
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

                # Ensure is_override_active column exists if table was already created
                cursor.execute("""
                    SELECT COUNT(*) FROM information_schema.columns 
                    WHERE table_schema = DATABASE() AND table_name = 'global_default_llm' AND column_name = 'is_override_active'
                """)
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE global_default_llm ADD COLUMN is_override_active TINYINT(1) NOT NULL DEFAULT 0")
                    logger.info("✅ Added is_override_active column to global_default_llm")

                # 2. Create globally_available_llm_configs (multi-row pool of reusable provider templates)
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

                # Check if legacy table default_global_llm_config or llm_configs exists to migrate data
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_schema = DATABASE() AND table_name IN ('default_global_llm_config', 'llm_configs')
                """)
                legacy_exists = cursor.fetchone()[0] > 0

                # 3. Seed global_default_llm if empty
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

                # 4. Seed globally_available_llm_configs if empty
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

                # 5. Safely drop legacy table default_global_llm_config if present
                if legacy_exists:
                    try:
                        cursor.execute("DROP TABLE IF EXISTS default_global_llm_config")
                        logger.info("🗑️ Safely dropped legacy table default_global_llm_config")
                    except Exception as e:
                        logger.warning(f"Failed to drop legacy table default_global_llm_config: {e}")

                db.commit()
                logger.info("✅ Ensured global_default_llm and globally_available_llm_configs tables exist")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure global LLM tables: {e}")

def ensure_client_llm_config_table():
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # 1. Rename existing legacy table if present
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

                # 2. Ensure table exists with all required columns
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

                # Ensure extra columns exist on migrated tables
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

                # Ensure model_name has a default value on migrated tables
                try:
                    cursor.execute("ALTER TABLE client_llm_config MODIFY COLUMN model_name VARCHAR(255) NOT NULL DEFAULT ''")
                except Exception:
                    pass

                db.commit()
                logger.info("✅ Ensured client_llm_config table exists with refreshed column")
    except Exception as e:
        logger.warning(f"⚠️ Failed to ensure client_llm_config table: {e}")


def _run_ensure_accounts_table():
    from app.db import get_db_ctx
    from app.email_credential import ensure_accounts_table_startup
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
        from app.email_credential import ensure_ticket_record_table
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(ensure_url_allowlist_table)
    await asyncio.to_thread(ensure_connector_configs_table)
    await asyncio.to_thread(_run_ensure_draft_emails_table)
    
    await asyncio.to_thread(_run_ensure_accounts_table)
    await asyncio.to_thread(ensure_create_payload_table)
    await asyncio.to_thread(ensure_payload_get_ticket_table)
    await asyncio.to_thread(ensure_users_table)
    await asyncio.to_thread(ensure_admin_seeded)
    await asyncio.to_thread(preload_qdrant_collection)
    await asyncio.to_thread(backfill_client_ids)
    await asyncio.to_thread(ensure_paused_emails_table)
    await asyncio.to_thread(ensure_global_llm_tables)
    await asyncio.to_thread(ensure_client_llm_config_table)
    await asyncio.to_thread(ensure_email_disclaimers_table)
    await asyncio.to_thread(ensure_llm_logs_table)
    await asyncio.to_thread(_run_ensure_chat_history_table)
    await asyncio.to_thread(_run_ensure_keyword_filter_tables)
    await asyncio.to_thread(_run_ensure_ticket_record_table)
    await asyncio.to_thread(_run_ensure_marketing_senders_table)
    await asyncio.to_thread(_run_ensure_worker_tables)

    await asyncio.to_thread(_run_ensure_paused_email_history_table)
    
    listener_task = asyncio.create_task(redis_pubsub_listener(app))
    yield
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def detailed_error_logging_middleware(request: Request, call_next):
    response = await call_next(request)
    if response.status_code >= 400:
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        logger.warning(
            f"⚠️ [HTTP {response.status_code}] "
            f"{request.method} {request.url.path} | "
            f"Client IP: {client_ip} | "
            f"User-Agent: {user_agent}"
        )
    return response

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WebSocket connection error: {e}")
        manager.disconnect(websocket)

class EmailRequest(BaseModel):
    client_id: str
    from_email: str
    subject: str
    body: str

class CreateAdminRequest(BaseModel):
    email: EmailStr
    password: str

@app.post("/admin/create-admin")
def create_admin(data: CreateAdminRequest, user: dict = Depends(require_admin())):
    from app.auth import register_admin_by_admin
    res = register_admin_by_admin(data.email, data.password, user["client_id"])
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

class AdminResetPasswordRequest(BaseModel):
    client_id: str
    new_password: str

@app.post("/admin/reset-client-password")
def reset_client_password_endpoint(data: AdminResetPasswordRequest, user: dict = Depends(require_admin())):
    from app.auth import admin_reset_client_password
    res = admin_reset_client_password(data.client_id, data.new_password)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

class ApproveRequest(BaseModel):
    email: EmailStr

@app.get("/")
def home():
    return {"status": "mail_ai_automation running"}

@app.post("/process-email", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def process_email(data: EmailRequest):
    process_email_task.delay(data.model_dump())
    return {"status": "queued"}


# following by hyper_is_op

class AcceptEmailRequest(BaseModel):
    client_id: str
    email: EmailStr
    password: str
    score_threshold: int = 80
    response_tone: str = "Formal"
    agent_type: str = "customer_support"


    @field_validator("password")
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v



class LoginRequest(BaseModel):
    email: EmailStr
    password: str

@app.post("/login", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def login(data: LoginRequest):
    res = login_user(data.email, data.password)
    if not res["success"]:
        raise HTTPException(status_code=401, detail=res["error"])
    return res

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordWithOtpRequest(BaseModel):
    email: EmailStr
    otp: str
    new_password: str

@app.post("/forgot-password/send-otp", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def send_otp_endpoint(data: ForgotPasswordRequest):
    from app.auth import send_reset_otp
    res = send_reset_otp(data.email)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

@app.post("/forgot-password/reset", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def reset_password_endpoint(data: ResetPasswordWithOtpRequest):
    from app.auth import reset_password_with_otp
    res = reset_password_with_otp(data.email, data.otp, data.new_password)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class OrderStatusRequest(BaseModel):
    client_id: str
    order_id: str

class EmailStatus(str, Enum):
    done_replied     = "Done_Replied"
    ticket_generated = "Ticket_Generated"

class EmailRecordRequest(BaseModel):
    client_id: str
    mail_id: str
    subject: str
    body: str
    status: EmailStatus

    model_config = {"use_enum_values": True}


@app.post("/accept-email")
def accept_email(data: AcceptEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        save_email_account(data.client_id, data.email, data.password, data.score_threshold, data.response_tone, data.agent_type)
        return {
            "status": "saved",
            "client_id": data.client_id,
            "email": data.email,
            "score_threshold": data.score_threshold,
            "response_tone": data.response_tone,
            "agent_type": data.agent_type
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")
        
        
@app.get("/email-account/{client_id}")
def get_email_account_by_id(client_id: str, request: Request):
    if not (client_id == "ALL" or client_id.startswith("CLI-")):
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        logger.warning(
            f"🚫 [INVALID CLIENT ID FORMAT] "
            f"Rejected client_id='{client_id}' | "
            f"From IP: {client_ip} | "
            f"User-Agent: {user_agent} | "
            f"Expected: 'CLI-XXXXXXXX' or 'ALL'"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid client_id format '{client_id}'. Expected 'CLI-XXXXXXXX' or 'ALL'"
        )
    try:
        account = get_email_account(client_id)
        if not account:
            raise HTTPException(status_code=404, detail=f"No account found for client_id {client_id}")
        return account
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/email-accounts")
def get_all_email_accounts_endpoint(user: dict = Depends(require_admin())):
    try:
        from app.db import get_db_ctx
        import pymysql
        with get_db_ctx() as db:
            cursor = db.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT 
                    ea.client_id, 
                    COALESCE(u.email, ea.email) AS email, 
                    ea.password,
                    u.email AS login_email,
                    ea.email AS imap_email,
                    ea.password AS imap_password,
                    u.name,
                    u.phone_number,
                    ea.agent_type,
                    ea.department_name,
                    ea.company_name,
                    COALESCE(ea.feature_ticket_creation, 1) AS feature_ticket_creation,
                    COALESCE(ea.feature_auto_send, 1) AS feature_auto_send,
                    COALESCE(ea.feature_rag, 1) AS feature_rag,
                    COALESCE(ea.feature_order_tracking, 1) AS feature_order_tracking,
                    COALESCE(ea.feature_manual_reply, 1) AS feature_manual_reply,
                    COALESCE(ea.cost_multiplier, 1.0) AS cost_multiplier,
                    ea.monthly_budget_usd
                FROM email_accounts ea 
                LEFT JOIN users u ON ea.client_id = u.client_id
            """)
            rows = cursor.fetchall()
            # Ensure boolean conversion for features
            for r in rows:
                r["feature_ticket_creation"] = bool(r.get("feature_ticket_creation", 1))
                r["feature_auto_send"] = bool(r.get("feature_auto_send", 1))
                r["feature_rag"] = bool(r.get("feature_rag", 1))
                r["feature_order_tracking"] = bool(r.get("feature_order_tracking", 1))
                r["feature_manual_reply"] = bool(r.get("feature_manual_reply", 1))
            return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/create-ticket", status_code=201)
def create_ticket(data: EmailRecordRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    result = create_email_record_db(data.model_dump())
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "message": "Ticket created successfully",
        "ticket_id": result["ticket_id"],
        "client_id": result["client_id"],
        "mail_id": result["mail_id"],
        "status": data.status,
    }

@app.post("/order-status")
def order_status(data: OrderStatusRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        order = get_order_by_id(data.client_id, data.order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return {"status": "success", "data": order}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")



class PayloadRequest(BaseModel):
    client_id: str
    url: str
    paylod: dict[str, Any]
    

@app.post("/insert-create_payload_ticket")
def create_payload_ticket(data: PayloadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        res_id = insert_create_payload_ticket(client_id=data.client_id, url=data.url, paylod=data.paylod)
        return {"status": "success", "client_id": res_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/insert-payload_get_ticket")
def payload_get_ticket(data: PayloadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        res_id = insert_payload_get_ticket(client_id=data.client_id, url=data.url, paylod=data.paylod)
        return {"status": "success", "client_id": res_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get-create_payload/{client_id}")
def get_create_payload_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        if client_id == "ALL":
            return get_all_create_payloads()
        res = get_create_payload_table(client_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get-get_payload/{client_id}")
def get_payload_get_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        if client_id == "ALL":
            return get_all_get_payloads()
        res = get_payload_get_ticket_table(client_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard/stats/{client_id}")
def get_dashboard_stats_endpoint(client_id: str, range_type: str = "all", start_date: str = None, end_date: str = None, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        import datetime
        
        # Initialize default response
        stats_data = {
            "total_emails": 0,
            "pending_emails": 0,
            "ai_replies": 0,
            "failed_emails": 0,
            "tickets_generated": 0,
            "orders_tracked": 0,
            "active_accounts": 0,
            "avg_confidence": 0.0,
            "chart_data": []
        }
        
        # Determine the date range where clause
        date_filter = ""
        date_params = []
        
        if range_type == "today":
            date_filter = " AND DATE(created_at) = CURDATE()"
        elif range_type == "yesterday":
            date_filter = " AND DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"
        elif range_type == "this_month":
            date_filter = " AND MONTH(created_at) = MONTH(CURDATE()) AND YEAR(created_at) = YEAR(CURDATE())"
        elif range_type == "last_month":
            date_filter = " AND MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))"
        elif range_type == "custom" and start_date and end_date:
            date_filter = " AND DATE(created_at) BETWEEN %s AND %s"
            date_params = [start_date, end_date]

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # Dynamically determine if the column is 'client_id' or 'user_id' in email_logs
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_logs: {ex}")
                
                # Dynamically determine if the column is 'client_id' or 'user_id' in email_accounts
                acct_col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_accounts")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        acct_col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_accounts: {ex}")
                
                # 1. Total Emails
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE 1=1" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s" + date_filter, (client_id, *date_params))
                stats_data["total_emails"] = cursor.fetchone()[0]
                
                # 2. Pending Emails
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE status = 'pending'" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status = 'pending'" + date_filter, (client_id, *date_params))
                stats_data["pending_emails"] = cursor.fetchone()[0]
                
                # 3. AI Replies Sent
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE status IN ('sent', 'ticket_created_and_sent')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('sent', 'ticket_created_and_sent')" + date_filter, (client_id, *date_params))
                stats_data["ai_replies"] = cursor.fetchone()[0]
                
                # 4. Failed Emails
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE status IN ('send_failed', 'ticket_created_send_failed')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('send_failed', 'ticket_created_send_failed')" + date_filter, (client_id, *date_params))
                stats_data["failed_emails"] = cursor.fetchone()[0]
                
                # 5. Tickets Generated (representing processed request tickets)
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE status IN ('ticket_created_and_sent', 'ticket_created_send_failed')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND status IN ('ticket_created_and_sent', 'ticket_created_send_failed')" + date_filter, (client_id, *date_params))
                stats_data["tickets_generated"] = cursor.fetchone()[0]
                
                # 6. Orders Tracked (approx based on queries containing 'order')
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE (subject LIKE '%%order%%' OR body LIKE '%%order%%')" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_logs WHERE {col} = %s AND (subject LIKE '%%order%%' OR body LIKE '%%order%%')" + date_filter, (client_id, *date_params))
                stats_data["orders_tracked"] = cursor.fetchone()[0]
                
                # 7. Active Accounts
                if client_id == "ALL":
                    cursor.execute(f"SELECT COUNT(*) FROM email_accounts")
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM email_accounts WHERE {acct_col} = %s", (client_id,))
                stats_data["active_accounts"] = cursor.fetchone()[0]
                
                # 7b. Average AI Confidence
                if client_id == "ALL":
                    cursor.execute(f"SELECT AVG(score) FROM email_logs WHERE score IS NOT NULL" + date_filter, tuple(date_params))
                else:
                    cursor.execute(f"SELECT AVG(score) FROM email_logs WHERE {col} = %s AND score IS NOT NULL" + date_filter, (client_id, *date_params))
                avg_score = cursor.fetchone()[0]
                stats_data["avg_confidence"] = round(float(avg_score), 1) if avg_score is not None else 0.0
                
                # 8. Dynamic Chart Data
                group_by_format = "%%Y-%%m-%%d"
                group_by_name = "%%b %%d"
                chart_interval = "1=1"
                
                if range_type == "today":
                    group_by_format = "%%H:00"
                    group_by_name = "%%h %%p"
                    chart_interval = "DATE(created_at) = CURDATE()"
                elif range_type == "yesterday":
                    group_by_format = "%%H:00"
                    group_by_name = "%%h %%p"
                    chart_interval = "DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"
                elif range_type == "this_month":
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "MONTH(created_at) = MONTH(CURDATE()) AND YEAR(created_at) = YEAR(CURDATE())"
                elif range_type == "last_month":
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)) AND YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))"
                elif range_type == "custom" and start_date and end_date:
                    group_by_format = "%%Y-%%m-%%d"
                    group_by_name = "%%b %%d"
                    chart_interval = "DATE(created_at) BETWEEN %s AND %s"
                    
                if client_id == "ALL":
                    chart_params = []
                    if range_type == "custom" and start_date and end_date:
                        chart_params.extend([start_date, end_date])
                    
                    cursor.execute(f"""
                        SELECT DATE_FORMAT(created_at, '{group_by_name}') as formatted_time, 
                               COUNT(*) as emails, 
                               SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as ai_replies,
                               DATE_FORMAT(created_at, '{group_by_format}') as sort_key
                        FROM email_logs
                        WHERE {chart_interval}
                        GROUP BY sort_key, formatted_time
                        ORDER BY sort_key ASC
                    """, tuple(chart_params))
                else:
                    chart_params = [client_id]
                    if range_type == "custom" and start_date and end_date:
                        chart_params.extend([start_date, end_date])
                    
                    cursor.execute(f"""
                        SELECT DATE_FORMAT(created_at, '{group_by_name}') as formatted_time, 
                               COUNT(*) as emails, 
                               SUM(CASE WHEN status IN ('sent', 'ticket_created_and_sent') THEN 1 ELSE 0 END) as ai_replies,
                               DATE_FORMAT(created_at, '{group_by_format}') as sort_key
                        FROM email_logs
                        WHERE {col} = %s AND {chart_interval}
                        GROUP BY sort_key, formatted_time
                        ORDER BY sort_key ASC
                    """, tuple(chart_params))
                rows = cursor.fetchall()
                
        chart_data = []
        for row in rows:
            chart_data.append({
                "name": row[0],
                "emails": row[1],
                "aiReplied": int(row[2] or 0)
            })
            
        stats_data["chart_data"] = chart_data
        return stats_data
        
    except Exception as e:
        logger.error(f"❌ Failed to fetch dashboard stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/emails/{client_id}")
def get_emails_logs_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # Dynamically determine column
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE email_logs")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe email_logs: {ex}")
                
                
                if client_id == "ALL":
                    cursor.execute(f"""
                        SELECT id, from_email, subject, body, reply, score, status, created_at, rag_id, sentiment, priority, execution_steps, summary, body_html, {col} as client_id 
                        FROM email_logs 
                        ORDER BY created_at DESC
                    """)
                else:
                    cursor.execute(f"""
                        SELECT id, from_email, subject, body, reply, score, status, created_at, rag_id, sentiment, priority, execution_steps, summary, body_html, {col} as client_id 
                        FROM email_logs 
                        WHERE {col} = %s 
                        ORDER BY created_at DESC
                    """, (client_id,))
                rows = cursor.fetchall()
                
                import json
                emails_list = []
                for r in rows:
                    ui_status = "New"
                    if r[6] in ["sent", "ticket_created_and_sent"]:
                        ui_status = "Replied" if r[6] == "sent" else "Ticket_Generated"
                    elif r[6] in ["send_failed", "ticket_created_send_failed"]:
                        ui_status = "Failed"
                    elif r[6] == "pending":
                        ui_status = "Processing"
                    elif r[6] == "pending_manual_review":
                        ui_status = "Pending Review"
                    elif r[6] in ["no_action_needed", "handled"]:
                        ui_status = "No Action Needed"
                    elif r[6] == "paused":
                        ui_status = "Paused"
                    elif r[6] == "blocked_keyword":
                        ui_status = "Blocked"
                        
                    steps = ["Start"]
                    if len(r) > 11 and r[11]:
                        try:
                            steps = json.loads(r[11])
                        except Exception:
                            pass

                    emails_list.append({
                        "id": r[0],
                        "mailId": f"msg-{r[0]}",
                        "sender": r[1],
                        "subject": r[2],
                        "preview": r[3],
                        "reply": r[4],
                        "confidence": f"{r[5]}%" if r[5] else "90%",
                        "status": ui_status,
                        "category": "Marketing / Promo" if r[6] in ["no_action_needed", "handled"] else "Customer Query",
                        "time": r[7].strftime("%I:%M %p") if r[7] else "Just Now",
                        "date_str": r[7].strftime("%b %d, %Y") if r[7] else "",
                        "raw_status": r[6],
                        "score": r[5] if r[5] is not None else 90,
                        "rag_id": r[8] if len(r) > 8 else None,
                        "sentiment": r[9] if len(r) > 9 and r[9] else "Neutral",
                        "priority": r[10] if len(r) > 10 and r[10] else "Medium",
                        "execution_steps": steps,
                        "summary": r[12] if len(r) > 12 and r[12] else "",
                        "body_html": r[13] if len(r) > 13 and r[13] else None,
                        "client_id": r[14] if len(r) > 14 else None
                    })
                return emails_list
    except Exception as e:
        logger.error(f"❌ Failed to fetch emails logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tickets/{client_id}")
def get_tickets_logs_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                try:
                    from app.email_credential import ensure_ticket_record_table
                    ensure_ticket_record_table(cursor)
                except Exception as tbl_ex:
                    logger.warning(f"⚠️ Could not ensure ticket_record table: {tbl_ex}")

                # Dynamically determine column
                col = "client_id"
                try:
                    cursor.execute("DESCRIBE ticket_record")
                    cols = [r[0] for r in cursor.fetchall()]
                    if "client_id" not in cols and "user_id" in cols:
                        col = "user_id"
                except Exception as ex:
                    logger.warning(f"⚠️ Could not describe ticket_record: {ex}")
                
                if client_id == "ALL":
                    cursor.execute(f"""
                        SELECT ticket_id, mail_id, subject, body, status, created_at, sentiment, priority 
                        FROM ticket_record 
                        ORDER BY created_at DESC
                    """)
                else:
                    cursor.execute(f"""
                        SELECT ticket_id, mail_id, subject, body, status, created_at, sentiment, priority 
                        FROM ticket_record 
                        WHERE {col} = %s 
                        ORDER BY created_at DESC
                    """, (client_id,))
                rows = cursor.fetchall()
                
                tickets_list = []
                for r in rows:
                    tickets_list.append({
                        "id": r[0],
                        "mailId": r[1],
                        "subject": r[2],
                        "preview": r[3],
                        "status": r[4],
                        "priority": r[7] if len(r) > 7 and r[7] else "Medium",
                        "sentiment": r[6] if len(r) > 6 and r[6] else "Neutral",
                        "time": r[5].strftime("%I:%M %p") if r[5] else "Just Now",
                        "date_str": r[5].strftime("%Y-%m-%d %I:%M %p") if r[5] else ""
                    })
                return tickets_list
    except Exception as e:
        logger.error(f"❌ Failed to fetch tickets list: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




class RagUploadRequest(BaseModel):
    client_id: str
    title: str
    content: str

class RagQueryRequest(BaseModel):
    client_id: str
    query: str

@app.post("/rag/upload", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def upload_rag_data_endpoint(data: RagUploadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.rag import add_knowledge
        doc_id = add_knowledge(data.client_id, data.title, data.content)
        return {"status": "success", "doc_id": doc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/rag/documents/{client_id}")
def get_rag_documents_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.rag import get_knowledge_base
        docs = get_knowledge_base(client_id)
        return docs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/rag/documents/{client_id}/{doc_id}")
def delete_rag_document_endpoint(client_id: str, doc_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.rag import delete_knowledge
        success = delete_knowledge(client_id, doc_id)
        if not success:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rag/query", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
def query_rag_endpoint(data: RagQueryRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.rag import query_knowledge
        context = query_knowledge(data.client_id, data.query)
        return {"context": context}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RagRetrieveRequest(BaseModel):
    client_id: str
    query: str
    top_k: int = 3

@app.post("/rag/retrieve", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
def retrieve_rag_endpoint(data: RagRetrieveRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.rag import retrieve_knowledge
        results = retrieve_knowledge(data.client_id, data.query, data.top_k)
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/knowledge-stats")
def get_admin_knowledge_stats(user: dict = Depends(require_admin())):
    try:
        from app.db import get_db_ctx
        from app.rag import get_knowledge_base
        
        with get_db_ctx() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT ea.client_id, COALESCE(u.email, ea.email) AS email 
                FROM email_accounts ea 
                LEFT JOIN users u ON ea.client_id = u.client_id
            """)
            clients = cursor.fetchall()
            
        stats = []
        for cid, email in clients:
            try:
                docs = get_knowledge_base(cid)
                doc_count = len(docs)
            except Exception:
                doc_count = 0
            stats.append({
                "client_id": cid,
                "email": email,
                "documents_count": doc_count
            })
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rag/upload-file", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
async def upload_rag_file_endpoint(
    client_id: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    try:
        from app.rag import parse_uploaded_file, add_knowledge
        ext = file.filename.split('.')[-1].lower()
        if ext not in ['pdf', 'doc', 'docx', 'txt']:
            raise HTTPException(status_code=400, detail="Unsupported file format. Only .pdf, .doc, .docx, and .txt files are allowed.")
        file_bytes = await file.read()
        title, content = parse_uploaded_file(file.filename, file_bytes)
        if not content.strip():
            raise HTTPException(status_code=400, detail="The uploaded file does not contain any readable text content.")
        doc_id = add_knowledge(client_id, title, content)
        return {"status": "success", "doc_id": doc_id, "title": title}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))





# chat history start
# ==============================
# 💬 Chat History Endpoints
# ==============================
from app.chat_history import get_history, clear_history

@app.get("/chat-history/{client_id}/{from_email}")
def get_chat_history(client_id: str, from_email: str, last_n: int = 15, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    """
    Returns last N messages for a customer.
    Checks Redis first, falls back to MySQL.
    """
    history = get_history(client_id, from_email, last_n=last_n)
    if not history:
        raise HTTPException(status_code=404, detail="No history found")
    return {
        "client_id":  client_id,
        "from_email": from_email,
        "count":      len(history),
        "history":    history
    }


@app.delete("/chat-history/{client_id}/{from_email}")
def delete_chat_history(client_id: str, from_email: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    """
    Clears Redis cache for this customer.
    MySQL history is permanent and not deleted.
    """
    clear_history(client_id, from_email)
    return {
        "status":     "redis_cache_cleared",
        "client_id":  client_id,
        "from_email": from_email,
        "note":       "MySQL history is retained"
    }
# chat history end


# ==============================
# 📊 LLM Analytics Endpoints
# ==============================

@app.get("/llm/metrics/{client_id}")
def get_llm_metrics_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        ensure_llm_logs_table()
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                
                # 1. Total statistics
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            COUNT(id) as total_requests,
                            SUM(prompt_tokens) as total_prompt_tokens,
                            SUM(completion_tokens) as total_completion_tokens,
                            SUM(cost) as total_cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            COUNT(id) as total_requests,
                            SUM(prompt_tokens) as total_prompt_tokens,
                            SUM(completion_tokens) as total_completion_tokens,
                            SUM(cost) as total_cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                    """, (client_id,))
                total_row = cursor.fetchone()
                
                totals = {
                    "total_requests": int(total_row[0] or 0),
                    "total_prompt_tokens": int(total_row[1] or 0),
                    "total_completion_tokens": int(total_row[2] or 0),
                    "total_cost": float(total_row[3] or 0.0),
                    "avg_latency": float(total_row[4] or 0.0)
                }

                # 2. Breakdown by Provider
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            COALESCE(NULLIF(provider, ''), 'groq') as provider_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY COALESCE(NULLIF(provider, ''), 'groq')
                        ORDER BY cost DESC, requests DESC
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            COALESCE(NULLIF(provider, ''), 'groq') as provider_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY COALESCE(NULLIF(provider, ''), 'groq')
                        ORDER BY cost DESC, requests DESC
                    """, (client_id,))
                provider_rows = cursor.fetchall()
                provider_breakdown = []
                for row in provider_rows:
                    provider_breakdown.append({
                        "provider": str(row[0]).lower(),
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })
                
                # 3. Breakdown by Model
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            model_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY model_name
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            model_name,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY model_name
                    """, (client_id,))
                model_rows = cursor.fetchall()
                model_breakdown = []
                for row in model_rows:
                    model_breakdown.append({
                        "model_name": row[0],
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })
                    
                # 4. Breakdown by Caller Function
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            caller_function,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        GROUP BY caller_function
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            caller_function,
                            COUNT(id) as requests,
                            SUM(prompt_tokens) as prompt_tokens,
                            SUM(completion_tokens) as completion_tokens,
                            SUM(cost) as cost,
                            AVG(latency_ms) as avg_latency
                        FROM llm_logs
                        WHERE client_id = %s
                        GROUP BY caller_function
                    """, (client_id,))
                caller_rows = cursor.fetchall()
                caller_breakdown = []
                for row in caller_rows:
                    func_name = row[0]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    else:
                        func_display = func_name.replace("_", " ").title()
                        
                    caller_breakdown.append({
                        "caller_function": row[0],
                        "caller_display": func_display,
                        "requests": int(row[1] or 0),
                        "prompt_tokens": int(row[2] or 0),
                        "completion_tokens": int(row[3] or 0),
                        "cost": float(row[4] or 0.0),
                        "avg_latency": float(row[5] or 0.0)
                    })
                    
                # 5. Recent Logs (last 30)
                if client_id == "ALL":
                    cursor.execute("""
                        SELECT 
                            id, COALESCE(NULLIF(provider, ''), 'groq') as provider, model_name, prompt_tokens, completion_tokens, cost, latency_ms, caller_function, created_at
                        FROM llm_logs
                        ORDER BY created_at DESC
                        LIMIT 30
                    """)
                else:
                    cursor.execute("""
                        SELECT 
                            id, COALESCE(NULLIF(provider, ''), 'groq') as provider, model_name, prompt_tokens, completion_tokens, cost, latency_ms, caller_function, created_at
                        FROM llm_logs
                        WHERE client_id = %s
                        ORDER BY created_at DESC
                        LIMIT 30
                    """, (client_id,))
                log_rows = cursor.fetchall()
                recent_logs = []
                for row in log_rows:
                    func_name = row[7]
                    if func_name == "detect_intent_llm":
                        func_display = "Intent Classification"
                    elif func_name == "generate_reply_llm" or "reply" in func_name:
                        func_display = "Reply Draft Generation"
                    else:
                        func_display = func_name.replace("_", " ").title()
                        
                    recent_logs.append({
                        "id": row[0],
                        "provider": row[1],
                        "model_name": row[2],
                        "prompt_tokens": int(row[3]),
                        "completion_tokens": int(row[4]),
                        "cost": float(row[5]),
                        "latency_ms": int(row[6]),
                        "caller_function": row[7],
                        "caller_display": func_display,
                        "created_at": row[8].strftime("%b %d, %H:%M:%S") if row[8] else ""
                    })
                
   
                # 6. Budget Info
                from app.email_credential import get_budget_status
                budget_info = get_budget_status(client_id, cursor)
                
                    
                return {
                    "status": "success",
                    "totals": totals,
                    "providers": provider_breakdown,
                    "models": model_breakdown,
                    "callers": caller_breakdown,
                    "logs": recent_logs,
                    "budget": budget_info
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch LLM analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/approve-registration")
def approve_registration_endpoint(data: ApproveRequest, user: dict = Depends(require_admin())):
    email = data.email
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT client_id, role, status FROM users WHERE email = %s", (email,))
                db_user = cursor.fetchone()
                if not db_user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                client_id, role, status = db_user[0], db_user[1], db_user[2]
                if status == 'active':
                    return {"status": "success", "message": "User is already active"}
                
                cursor.execute("UPDATE users SET status = 'active' WHERE email = %s", (email,))
                db.commit()
                
                try:
                    cursor.execute("SELECT client_id FROM email_accounts LIMIT 1")
                    row = cursor.fetchone()
                    sender_client_id = row[0] if row else "CLI-7AE811F3"
                    
                    from app.mailer import send_email
                    subject = "Your Mail AI Account is Approved!"
                    body = (
                        f"Hello,\n\n"
                        f"Your registration request has been approved by the admin.\n"
                        f"You can now log in to your account at:\n"
                        f"http://172.16.3.215:1947/login\n\n"
                        f"Best regards,\n"
                        f"Mail AI Team"
                    )
                    send_email(sender_client_id, email, subject, body)
                except Exception as mail_err:
                    logger.error(f"Failed to send confirmation email: {mail_err}")
                
                return {"status": "success", "message": f"User {email} has been approved successfully."}
    except Exception as e:
        logger.error(f"Approval error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/logout")
def logout(authorization: str = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        from app.auth import destroy_session
        destroy_session(authorization[7:])
    return {"status": "logged_out"}


# ==============================
# ⏸ Pause Emails & Manual Reply
# ==============================

class PauseEmailRequest(BaseModel):
    client_id: str
    email: str

@app.post("/pause-email")
def pause_email(data: PauseEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("INSERT IGNORE INTO paused_emails (client_id, paused_email) VALUES (%s, %s)", (data.client_id, data.email))
                db.commit()
        return {"status": "success", "message": f"{data.email} is paused."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# unpause-email had NO auth at all — anyone could unpause any client's emails
@app.post("/unpause-email")
def unpause_email(data: PauseEmailRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("DELETE FROM paused_emails WHERE client_id = %s AND paused_email = %s", (data.client_id, data.email))
                db.commit()
        return {"status": "success", "message": f"{data.email} is unpaused."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/paused-emails/{client_id}")
def get_paused_emails_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if client_id == "ALL":
                    cursor.execute("SELECT paused_email FROM paused_emails")
                else:
                    cursor.execute("SELECT paused_email FROM paused_emails WHERE client_id = %s", (client_id,))
                rows = cursor.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# 📢 Marketing & Promotional Senders
# ==============================

class MarketingSenderRequest(BaseModel):
    client_id: str
    sender_email: str

@app.post("/marketing-senders")
def mark_marketing_sender(data: MarketingSenderRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    clean_sender = data.sender_email.strip().lower()
    if not clean_sender:
        raise HTTPException(status_code=400, detail="sender_email cannot be empty")
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    INSERT IGNORE INTO marketing_senders (client_id, sender_email) 
                    VALUES (%s, %s)
                """, (data.client_id, clean_sender))
                db.commit()
        return {"status": "success", "message": f"{clean_sender} marked as Marketing / Promotional."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unmark-marketing-sender")
def unmark_marketing_sender(data: MarketingSenderRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    clean_sender = data.sender_email.strip().lower()
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    DELETE FROM marketing_senders 
                    WHERE client_id = %s AND (LOWER(sender_email) = %s OR sender_email = %s)
                """, (data.client_id, clean_sender, clean_sender))
                db.commit()
        return {"status": "success", "message": f"{clean_sender} unmarked from Marketing / Promotional."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/marketing-senders/{client_id}")
def get_marketing_senders_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if client_id == "ALL":
                    cursor.execute("SELECT sender_email FROM marketing_senders ORDER BY created_at DESC")
                else:
                    cursor.execute("SELECT sender_email FROM marketing_senders WHERE client_id = %s ORDER BY created_at DESC", (client_id,))
                rows = cursor.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/paused-email-history/{client_id}")
def get_paused_email_history(
    client_id: str,
    status: str = None,
    group_by_email: bool = False,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    from app.db import get_db_ctx

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if status:
                if status not in ("pending_review", "ignored", "replied"):
                    raise HTTPException(status_code=400, detail="status must be one of: pending_review, ignored, replied")
                cursor.execute("""
                    SELECT id, from_email, subject, body, status, created_at, updated_at
                    FROM paused_email_history
                    WHERE client_id = %s AND status = %s
                    ORDER BY from_email, created_at DESC
                """, (client_id, status))
            else:
                cursor.execute("""
                    SELECT id, from_email, subject, body, status, created_at, updated_at
                    FROM paused_email_history
                    WHERE client_id = %s
                    ORDER BY from_email, created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()

    records = [{
        "id": r[0], "from_email": r[1], "subject": r[2], "body": r[3],
        "status": r[4], "created_at": str(r[5]), "updated_at": str(r[6]),
    } for r in rows]

    if not group_by_email:
        return records

    grouped = {}
    for rec in records:
        grouped.setdefault(rec["from_email"], []).append(rec)
    return grouped


class PausedEmailHistoryUpdateRequest(BaseModel):
    status: str  # 'ignored' or 'replied'

@app.patch("/paused-email-history/{client_id}/{record_id}")
def update_paused_email_history_status(
    client_id: str,
    record_id: int,
    data: PausedEmailHistoryUpdateRequest,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    if data.status not in ("ignored", "replied"):
        raise HTTPException(status_code=400, detail="status must be 'ignored' or 'replied'")

    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE paused_email_history
                SET status = %s
                WHERE id = %s AND client_id = %s
            """, (data.status, record_id, client_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Record not found")
        db.commit()
    return {"status": "success"}

class ManualReplyRequest(BaseModel):
    client_id: str
    to_email: str
    subject: str
    body: str = ""
    reply_text: str
    blocked_record_id: int | None = None
    paused_history_record_id: int | None = None  # NEW

@app.post("/manual-reply")
def send_manual_reply(data: ManualReplyRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.mailer import send_email
        import json

        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cursor:

                # STEP 1 — validate BOTH record types before anything irreversible
                if data.blocked_record_id is not None:
                    cursor.execute("""
                        SELECT status FROM reply_blocked_by_keyword
                        WHERE id = %s AND client_id = %s
                    """, (data.blocked_record_id, data.client_id))
                    record = cursor.fetchone()
                    if not record:
                        raise HTTPException(
                            status_code=404,
                            detail=f"blocked_record_id={data.blocked_record_id} not found for this client"
                        )
                    if record[0] != 'pending_review':
                        raise HTTPException(
                            status_code=400,
                            detail=f"Record already actioned — current status is '{record[0]}'"
                        )

                if data.paused_history_record_id is not None:
                    cursor.execute("""
                        SELECT status FROM paused_email_history
                        WHERE id = %s AND client_id = %s
                    """, (data.paused_history_record_id, data.client_id))
                    record = cursor.fetchone()
                    if not record:
                        raise HTTPException(
                            status_code=404,
                            detail=f"paused_history_record_id={data.paused_history_record_id} not found for this client"
                        )
                    if record[0] != 'pending_review':
                        raise HTTPException(
                            status_code=400,
                            detail=f"Record already actioned — current status is '{record[0]}'"
                        )

                # STEP 2 — send email, capture result, never let it throw
                send_status = send_email(
                    data.client_id, data.to_email, data.subject, data.reply_text
                )

                # STEP 3 — log regardless of send outcome
                exec_steps = ["Start", "Manual_Reply", "SMTP_Send" if send_status else "SMTP_Failed"]
                cursor.execute("""
                    INSERT INTO email_logs (client_id, from_email, subject, body, reply, score, status, priority, sentiment, execution_steps)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    data.client_id, data.to_email, data.subject, data.body,
                    data.reply_text, 100,
                    'manual_reply' if send_status else 'manual_reply_send_failed',
                    'Medium', 'Neutral',
                    json.dumps(exec_steps)
                ))

                # STEP 4 — only mark replied if SMTP confirmed success, for BOTH record types
                if data.blocked_record_id is not None:
                    if send_status:
                        cursor.execute("""
                            UPDATE reply_blocked_by_keyword
                            SET status = 'replied'
                            WHERE id = %s AND client_id = %s
                        """, (data.blocked_record_id, data.client_id))
                    else:
                        logger.warning(
                            f"⚠️ SMTP failed for blocked_record_id={data.blocked_record_id}"
                            f" — status stays 'pending_review'"
                        )

                if data.paused_history_record_id is not None:
                    if send_status:
                        cursor.execute("""
                            UPDATE paused_email_history
                            SET status = 'replied'
                            WHERE id = %s AND client_id = %s
                        """, (data.paused_history_record_id, data.client_id))
                    else:
                        logger.warning(
                            f"⚠️ SMTP failed for paused_history_record_id={data.paused_history_record_id}"
                            f" — status stays 'pending_review'"
                        )

                db.commit()

                if not send_status:
                    raise HTTPException(
                        status_code=502,
                        detail="Email failed to send — record kept as pending_review, log entry written"
                    )

        # STEP 5 — chat history (unchanged)
        from app.chat_history import push_message
        push_message(
            client_id=data.client_id,
            from_email=data.to_email,
            role="support",
            subject=data.subject,
            body=data.reply_text,
            ticket_id=""
        )

        # STEP 6 — Redis broadcast (unchanged)
        import redis
        import os
        redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0") or "redis://localhost:6379/0"
        try:
            r = redis.from_url(redis_url)
            r.publish("email_updates", json.dumps({
                "type": "NEW_EMAIL",
                "client_id": data.client_id
            }))
        except Exception as ex:
            logger.warning(f"Redis publish failed: {ex}")

        return {"status": "success", "message": "Manual reply sent"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual reply error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ApprovePendingReplyRequest(BaseModel):
    client_id: str
    log_id: int

@app.post("/approve-pending-reply")
def approve_pending_reply(data: ApprovePendingReplyRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT from_email, subject, reply, status FROM email_logs WHERE id=%s AND client_id=%s",
                (data.log_id, data.client_id)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Log entry not found")
            from_email, subject, reply, status = row
            if status != "pending_manual_review":
                raise HTTPException(status_code=400, detail=f"Log is not pending review (status={status})")
            if not reply:
                raise HTTPException(status_code=400, detail="No stored reply — use /manual-reply to compose one instead")

            send_status = send_email(data.client_id, from_email, "Re: " + subject, reply)
            new_status = "sent" if send_status else "send_failed"
            cursor.execute("UPDATE email_logs SET status=%s WHERE id=%s", (new_status, data.log_id))
            db.commit()

    from app.chat_history import push_message
    push_message(client_id=data.client_id, from_email=from_email, role="support",
                 subject="Re: " + subject, body=reply, ticket_id="")
    return {"status": "success", "new_status": new_status}


class ClientFeaturesRequest(BaseModel):
    client_id: str
    feature_ticket_creation: bool
    feature_auto_send: bool
    feature_rag: bool
    feature_order_tracking: bool
    feature_manual_reply: bool
    feature_strip_disclaimers: bool = True

@app.get("/admin/client-features/{client_id}")
def get_client_features_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    import pymysql
    with get_db_ctx() as db:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT 
                COALESCE(feature_ticket_creation, 1) AS feature_ticket_creation,
                COALESCE(feature_auto_send, 1) AS feature_auto_send,
                COALESCE(feature_rag, 1) AS feature_rag,
                COALESCE(feature_order_tracking, 1) AS feature_order_tracking,
                COALESCE(feature_manual_reply, 1) AS feature_manual_reply,
                COALESCE(feature_strip_disclaimers, 1) AS feature_strip_disclaimers
            FROM email_accounts WHERE client_id=%s LIMIT 1
        """, (client_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "feature_ticket_creation": True,
                "feature_auto_send": True,
                "feature_rag": True,
                "feature_order_tracking": True,
                "feature_manual_reply": True,
                "feature_strip_disclaimers": True
            }
        return {
            "feature_ticket_creation": bool(row["feature_ticket_creation"]),
            "feature_auto_send": bool(row["feature_auto_send"]),
            "feature_rag": bool(row["feature_rag"]),
            "feature_order_tracking": bool(row["feature_order_tracking"]),
            "feature_manual_reply": bool(row["feature_manual_reply"]),
            "feature_strip_disclaimers": bool(row["feature_strip_disclaimers"])
        }

@app.post("/admin/client-features")
def set_client_features(data: ClientFeaturesRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE email_accounts SET feature_ticket_creation=%s, feature_auto_send=%s,
                feature_rag=%s, feature_order_tracking=%s, feature_manual_reply=%s,
                feature_strip_disclaimers=%s
                WHERE client_id=%s
            """, (data.feature_ticket_creation, data.feature_auto_send, data.feature_rag,
                  data.feature_order_tracking, data.feature_manual_reply,
                  data.feature_strip_disclaimers, data.client_id))
            db.commit()
    return {"status": "success"}

# ==============================
# 🛑 Master Automation Flow Control & Admin Kill Switch
# ==============================

class AdminMasterBotToggleRequest(BaseModel):
    client_id: str
    admin_bot_enabled: bool

class ClientMasterBotToggleRequest(BaseModel):
    client_id: str
    client_bot_enabled: bool

@app.get("/master-bot-status/{client_id}")
def get_master_bot_status(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    COALESCE(admin_bot_enabled, 1) AS admin_bot_enabled,
                    COALESCE(client_bot_enabled, 1) AS client_bot_enabled
                FROM email_accounts WHERE client_id=%s LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
            if not row:
                return {
                    "client_id": client_id,
                    "admin_bot_enabled": True,
                    "client_bot_enabled": True,
                    "is_effective_enabled": True,
                    "is_locked_by_admin": False
                }
            admin_enabled = bool(row[0])
            client_enabled = bool(row[1])
            return {
                "client_id": client_id,
                "admin_bot_enabled": admin_enabled,
                "client_bot_enabled": client_enabled,
                "is_effective_enabled": admin_enabled and client_enabled,
                "is_locked_by_admin": not admin_enabled
            }

@app.post("/admin/master-bot-toggle")
def admin_master_bot_toggle(data: AdminMasterBotToggleRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE email_accounts SET admin_bot_enabled=%s WHERE client_id=%s",
                (data.admin_bot_enabled, data.client_id)
            )
            db.commit()
    return {
        "status": "success",
        "admin_bot_enabled": data.admin_bot_enabled,
        "message": f"Admin Master Switch set to {data.admin_bot_enabled} for client {data.client_id}"
    }

@app.post("/client/master-bot-toggle")
def client_master_bot_toggle(data: ClientMasterBotToggleRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:


            # Enforce admin lock: If admin turned it off, client CANNOT turn it back on!
            cursor.execute("SELECT COALESCE(admin_bot_enabled, 1) FROM email_accounts WHERE client_id=%s", (data.client_id,))
            row = cursor.fetchone()
            admin_enabled = bool(row[0]) if row else True

            if not admin_enabled and data.client_bot_enabled:
                raise HTTPException(
                    status_code=403,
                    detail="Master Bot has been disabled by the Administrator. You cannot turn it on until an administrator re-enables it for your account."
                )

            cursor.execute(
                "UPDATE email_accounts SET client_bot_enabled=%s WHERE client_id=%s",
                (data.client_bot_enabled, data.client_id)
            )
            db.commit()
    return {
        "status": "success",
        "client_bot_enabled": data.client_bot_enabled,
        "message": f"Client Master Switch set to {data.client_bot_enabled}"
    }

class ClientLlmConfigRequest(BaseModel):
    client_id: str
    caller_function: str
    global_config_id: int | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model_name: str
    api_version: str | None = None

@app.post("/admin/client-llm-config")
def set_client_llm_config(data: ClientLlmConfigRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                INSERT INTO client_llm_config (client_id, caller_function, global_config_id, provider, api_key, base_url, model_name, api_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    global_config_id=%s, provider=%s, api_key=%s, base_url=%s, model_name=%s, api_version=%s
            """, (
                data.client_id, data.caller_function, data.global_config_id, data.provider, data.api_key, data.base_url, data.model_name, data.api_version,
                data.global_config_id, data.provider, data.api_key, data.base_url, data.model_name, data.api_version
            ))
            db.commit()
    return {"status": "success"}

@app.get("/admin/client-llm-config/{client_id}")
def get_client_llm_config(client_id: str, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT caller_function, model_name, global_config_id, provider, api_key, base_url, api_version, created_at, updated_at, refreshed
                FROM client_llm_config 
                WHERE client_id=%s
            """, (client_id,))
            rows = cursor.fetchall()
    return [{
        "caller_function": r[0],
        "model_name": r[1],
        "global_config_id": r[2],
        "provider": r[3],
        "api_key": r[4],
        "base_url": r[5],
        "api_version": r[6],
        "created_at": str(r[7]) if r[7] else None,
        "updated_at": str(r[8]) if r[8] else None,
        "refreshed": str(r[9]) if r[9] else None
    } for r in rows]

class ClientLlmRefreshRequest(BaseModel):
    client_id: str
    caller_function: str
    global_config_id: int | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api_version: str | None = None

@app.post("/admin/client-llm-config/refresh")
def refresh_client_llm_config(data: ClientLlmRefreshRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    
    target_provider = data.provider
    target_api_key = data.api_key
    target_base_url = data.base_url
    target_api_version = data.api_version
    global_config_id = data.global_config_id

    # If referencing a specific globally available configuration template
    if global_config_id:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, api_version, name 
                    FROM globally_available_llm_configs 
                    WHERE id = %s
                """, (global_config_id,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Referenced globally available LLM config not found")
                target_provider = row[1]
                target_api_key = row[2]
                target_base_url = row[3]
                target_api_version = row[4]
    elif not target_provider and not target_api_key:
        # Fallback to single global default in global_default_llm (id=1)
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, api_version, model_name 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    cursor.execute("SELECT id, provider, api_key, base_url, api_version, model_name FROM global_default_llm LIMIT 1")
                    row = cursor.fetchone()
                if row:
                    target_provider = row[1]
                    target_api_key = row[2]
                    target_base_url = row[3]
                    target_api_version = row[4]

    if not target_provider or not target_api_key:
        raise HTTPException(status_code=400, detail="No global default or custom LLM provider credentials configured in the database to perform live refresh.")

    # Live query models from provider
    try:
        models = _query_provider_live_models(target_provider, target_api_key, target_base_url, target_api_version)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying live models from {target_provider}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch live models from {target_provider.upper()}: {str(e)}")

    now_str = None
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                # If referencing a specific globally available config template, update its refreshed timestamp
                if global_config_id:
                    cursor.execute("""
                        UPDATE globally_available_llm_configs 
                        SET refreshed = CURRENT_TIMESTAMP 
                        WHERE id = %s
                    """, (global_config_id,))
                elif not data.provider:
                    # Update global_default_llm refreshed timestamp
                    cursor.execute("UPDATE global_default_llm SET refreshed = CURRENT_TIMESTAMP WHERE id = 1")

                # Update / insert client_llm_config refreshed timestamp for this function
                cursor.execute("""
                    INSERT INTO client_llm_config (client_id, caller_function, model_name, global_config_id, provider, api_key, base_url, api_version, refreshed)
                    VALUES (%s, %s, '', %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON DUPLICATE KEY UPDATE 
                        global_config_id=VALUES(global_config_id),
                        provider=VALUES(provider),
                        api_key=VALUES(api_key),
                        base_url=VALUES(base_url),
                        api_version=VALUES(api_version),
                        refreshed=CURRENT_TIMESTAMP
                """, (
                    data.client_id, data.caller_function, global_config_id,
                    data.provider if not global_config_id else None,
                    data.api_key if not global_config_id else None,
                    data.base_url if not global_config_id else None,
                    data.api_version if not global_config_id else None
                ))
                
                cursor.execute("""
                    SELECT refreshed FROM client_llm_config WHERE client_id=%s AND caller_function=%s
                """, (data.client_id, data.caller_function))
                r = cursor.fetchone()
                if r and r[0]:
                    now_str = str(r[0])
                db.commit()
    except Exception as e:
        logger.error(f"Database error during client LLM refresh: {e}")
        raise HTTPException(status_code=400, detail=f"Database update failed: {str(e)}")

    return {
        "status": "success",
        "client_id": data.client_id,
        "caller_function": data.caller_function,
        "global_config_id": global_config_id,
        "provider": target_provider,
        "refreshed": now_str,
        "count": len(models),
        "models": models
    }

# Backward compatibility alias endpoints
@app.post("/admin/client-model-config")
def set_client_model_config_legacy(data: ClientLlmConfigRequest, user: dict = Depends(require_admin())):
    return set_client_llm_config(data, user)

@app.get("/admin/client-model-config/{client_id}")
def get_client_model_config_legacy(client_id: str, user: dict = Depends(require_admin())):
    return get_client_llm_config(client_id, user)

class ClientCostConfigRequest(BaseModel):
    client_id: str
    cost_multiplier: float
    monthly_budget_usd: float | None = None

@app.post("/admin/client-cost-config")
def set_client_cost_config(data: ClientCostConfigRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE email_accounts SET cost_multiplier=%s, monthly_budget_usd=%s WHERE client_id=%s",
                (data.cost_multiplier, data.monthly_budget_usd, data.client_id)
            )
            db.commit()
    return {"status": "success"}


# =========================================================================
# 1. Global Default LLM (Single-row platform default table: global_default_llm)
# =========================================================================

class GlobalDefaultLlmRequest(BaseModel):
    provider: str
    api_key: str
    base_url: str | None = None
    model_name: str
    api_version: str | None = None
    is_override_active: bool | None = None

class ToggleOverrideRequest(BaseModel):
    is_override_active: bool

@app.get("/admin/global-default-llm")
def get_global_default_llm_endpoint(user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed, is_override_active 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    cursor.execute("SELECT id, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed, is_override_active FROM global_default_llm LIMIT 1")
                    row = cursor.fetchone()
                if not row:
                    return {
                        "id": 1,
                        "provider": "groq",
                        "api_key": os.getenv("GROQ_API_KEY", ""),
                        "base_url": "https://api.groq.com/openai/v1",
                        "model_name": os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b"),
                        "api_version": None,
                        "created_at": None,
                        "updated_at": None,
                        "refreshed": None,
                        "is_override_active": False
                    }
                return {
                    "id": row[0],
                    "provider": row[1],
                    "api_key": row[2],
                    "base_url": row[3],
                    "model_name": row[4],
                    "api_version": row[5],
                    "created_at": str(row[6]) if row[6] else None,
                    "updated_at": str(row[7]) if row[7] else None,
                    "refreshed": str(row[8]) if row[8] else None,
                    "is_override_active": bool(row[9]) if len(row) > 9 and row[9] is not None else False
                }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/global-default-llm")
def set_global_default_llm_endpoint(data: GlobalDefaultLlmRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO global_default_llm (id, provider, api_key, base_url, model_name, api_version)
                    VALUES (1, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                        provider=VALUES(provider),
                        api_key=VALUES(api_key),
                        base_url=VALUES(base_url),
                        model_name=VALUES(model_name),
                        api_version=VALUES(api_version)
                """, (data.provider, data.api_key, data.base_url, data.model_name, data.api_version))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/global-default-llm/toggle-override")
def toggle_global_override_endpoint(data: ToggleOverrideRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    UPDATE global_default_llm 
                    SET is_override_active = %s 
                    WHERE id = 1
                """, (1 if data.is_override_active else 0,))
            db.commit()
        logger.info(f"⚙️ Global Default LLM Emergency Override toggled to: {data.is_override_active}")
        return {"status": "success", "is_override_active": data.is_override_active}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/global-default-llm/refresh")
def refresh_global_default_llm_endpoint(user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT provider, api_key, base_url, api_version 
                    FROM global_default_llm 
                    WHERE id = 1
                """)
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Global Default LLM configuration not found")
                
                provider, api_key, base_url, api_version = row[0], row[1], row[2], row[3]
                models = _query_provider_live_models(provider, api_key, base_url, api_version)
                
                cursor.execute("UPDATE global_default_llm SET refreshed = CURRENT_TIMESTAMP WHERE id = 1")
                db.commit()

                cursor.execute("SELECT refreshed FROM global_default_llm WHERE id = 1")
                refreshed_val = cursor.fetchone()[0]

                return {
                    "status": "success",
                    "provider": provider,
                    "refreshed": str(refreshed_val),
                    "count": len(models),
                    "models": models
                }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to refresh global default LLM models: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# 2. Globally Available LLM Configs (Multiple entries: globally_available_llm_configs)
# =========================================================================

class GloballyAvailableLlmConfigRequest(BaseModel):
    id: int | None = None
    name: str
    provider: str
    api_key: str
    base_url: str | None = None
    model_name: str
    api_version: str | None = None

@app.get("/admin/globally-available-llm-configs")
def get_globally_available_llm_configs_endpoint(user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, name, provider, api_key, base_url, model_name, api_version, created_at, updated_at, refreshed 
                    FROM globally_available_llm_configs 
                    ORDER BY id DESC
                """)
                rows = cursor.fetchall()
                configs = []
                for r in rows:
                    configs.append({
                        "id": r[0],
                        "name": r[1],
                        "provider": r[2],
                        "api_key": r[3],
                        "base_url": r[4],
                        "model_name": r[5],
                        "api_version": r[6],
                        "created_at": str(r[7]) if r[7] else None,
                        "updated_at": str(r[8]) if r[8] else None,
                        "refreshed": str(r[9]) if r[9] else None
                    })
                return configs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/globally-available-llm-configs")
def save_globally_available_llm_config_endpoint(data: GloballyAvailableLlmConfigRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                if data.id:
                    cursor.execute("""
                        UPDATE globally_available_llm_configs 
                        SET name=%s, provider=%s, api_key=%s, base_url=%s, model_name=%s, api_version=%s
                        WHERE id=%s
                    """, (data.name, data.provider, data.api_key, data.base_url, data.model_name, data.api_version, data.id))
                else:
                    cursor.execute("""
                        INSERT INTO globally_available_llm_configs (name, provider, api_key, base_url, model_name, api_version)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (data.name, data.provider, data.api_key, data.base_url, data.model_name, data.api_version))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/admin/globally-available-llm-configs/{config_id}")
def delete_globally_available_llm_config_endpoint(config_id: int, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("DELETE FROM globally_available_llm_configs WHERE id=%s", (config_id,))
            db.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/globally-available-llm-configs/{config_id}/refresh")
def refresh_globally_available_llm_config_endpoint(config_id: int, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT provider, api_key, base_url, api_version, name 
                    FROM globally_available_llm_configs 
                    WHERE id=%s
                """, (config_id,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Globally available LLM configuration not found")
                
                provider, api_key, base_url, api_version, name = row[0], row[1], row[2], row[3], row[4]
                models = _query_provider_live_models(provider, api_key, base_url, api_version)
                
                cursor.execute("""
                    UPDATE globally_available_llm_configs 
                    SET refreshed = CURRENT_TIMESTAMP 
                    WHERE id = %s
                """, (config_id,))
                db.commit()

                cursor.execute("SELECT refreshed FROM globally_available_llm_configs WHERE id=%s", (config_id,))
                refreshed_val = cursor.fetchone()[0]

                return {
                    "status": "success",
                    "config_id": config_id,
                    "name": name,
                    "provider": provider,
                    "refreshed": str(refreshed_val),
                    "count": len(models),
                    "models": models
                }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to refresh models for globally available config {config_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# Backward compatibility alias for legacy /admin/llm-configs
@app.get("/admin/llm-configs")
def get_llm_configs_endpoint_legacy(user: dict = Depends(require_admin())):
    return get_globally_available_llm_configs_endpoint(user)

@app.post("/admin/llm-configs")
def save_llm_config_endpoint_legacy(data: GloballyAvailableLlmConfigRequest, user: dict = Depends(require_admin())):
    return save_globally_available_llm_config_endpoint(data, user)

@app.delete("/admin/llm-configs/{config_id}")
def delete_llm_config_endpoint_legacy(config_id: int, user: dict = Depends(require_admin())):
    return delete_globally_available_llm_config_endpoint(config_id, user)

@app.post("/admin/llm-configs/{config_id}/refresh")
def refresh_llm_config_endpoint_legacy(config_id: int, user: dict = Depends(require_admin())):
    return refresh_globally_available_llm_config_endpoint(config_id, user)

def _query_provider_live_models(provider: str, api_key: str, base_url: str | None = None, api_version: str | None = None) -> list[str]:
    import requests
    provider = (provider or "groq").lower().strip()
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required to query models from provider.")

    models = []
    if provider == "groq":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.groq.com/openai/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"Groq error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id") and m.get("active", True)]

    elif provider == "openai":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.openai.com/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"OpenAI error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        excluded = ("whisper", "dall-e", "tts", "embedding", "moderation", "davinci", "babbage", "curie", "text-search")
        models = [
            m.get("id") for m in raw_list 
            if m.get("id") and not any(ex in m.get("id").lower() for ex in excluded)
        ]

    elif provider in ("claude", "anthropic"):
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.anthropic.com/v1'}/models"
        resp = requests.get(target_url, headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }, timeout=12)
        if resp.ok:
            raw_list = resp.json().get("data", [])
            models = [m.get("id") for m in raw_list if m.get("id")]
        else:
            models = ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]

    elif provider == "gemini":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://generativelanguage.googleapis.com/v1beta/openai'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if resp.ok:
            raw_list = resp.json().get("data", [])
            models = [m.get("id") for m in raw_list if m.get("id")]
        else:
            native_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            native_resp = requests.get(native_url, timeout=12)
            if native_resp.ok:
                raw_models = native_resp.json().get("models", [])
                models = [
                    m.get("name", "").replace("models/", "")
                    for m in raw_models
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
            else:
                raise Exception(f"Google Gemini error ({native_resp.status_code}): {native_resp.text}")

    elif provider == "grok":
        target_url = f"{base_url.rstrip('/') if base_url else 'https://api.x.ai/v1'}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
        if not resp.ok:
            raise Exception(f"xAI Grok error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    elif provider == "azure":
        target_url = f"{base_url.rstrip('/')}/openai/models?api-version={api_version or '2024-02-15-preview'}"
        resp = requests.get(target_url, headers={"api-key": api_key}, timeout=12)
        if not resp.ok:
            raise Exception(f"Azure OpenAI error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    else: # custom gateway
        target_url = f"{base_url.rstrip('/')}/models"
        resp = requests.get(target_url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=12)
        if not resp.ok:
            raise Exception(f"Custom gateway error ({resp.status_code}): {resp.text}")
        raw_list = resp.json().get("data", [])
        models = [m.get("id") for m in raw_list if m.get("id")]

    return sorted(list(set(models)))

class FetchProviderModelsRequest(BaseModel):
    provider: str
    api_key: str
    base_url: str | None = None
    api_version: str | None = None

@app.post("/admin/llm/fetch-models")
def fetch_provider_models_endpoint(data: FetchProviderModelsRequest, user: dict = Depends(require_admin())):
    try:
        clean_models = _query_provider_live_models(data.provider, data.api_key, data.base_url, data.api_version)
        return {
            "status": "success", 
            "provider": (data.provider or "groq").lower().strip(), 
            "count": len(clean_models), 
            "models": clean_models
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch live models for {data.provider}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/budget-status/{client_id}")
def get_budget_status_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    from app.email_credential import get_budget_status
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            return get_budget_status(client_id, cursor)

@app.get("/admin/budget-status")
def get_all_budget_statuses(user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    from app.email_credential import get_budget_status
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id FROM email_accounts")
            client_ids = [r[0] for r in cursor.fetchall()]
            results = []
            for cid in client_ids:
                status = get_budget_status(cid, cursor)
                status["client_id"] = cid
                results.append(status)
    return results


class CreateClientRequest(BaseModel):
    name: str
    phone_number: str
    login_email: EmailStr
    login_password: str
    imap_email: str = ""
    imap_password: str = ""
    score_threshold: int = 80
    response_tone: str = "Formal"
    agent_type: str = "customer_support_agent"
    department_name: str = None
    company_name: str = None

    @field_validator("login_password")
    def login_pw_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("imap_password")
    def imap_pw_strength(cls, v):
        if v and len(v) < 8:
            raise ValueError("IMAP password must be at least 8 characters")
        return v

@app.post("/admin/create-client")
def create_client(data: CreateClientRequest, user: dict = Depends(require_admin())):
    from app.auth import create_client_atomic
    res = create_client_atomic(
        data.name, data.phone_number, data.login_email, data.login_password, data.imap_email, data.imap_password,
        data.score_threshold, data.response_tone,
        data.agent_type, data.department_name, data.company_name
    )
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

@app.get("/admin/pending-users")
def list_pending_users(user: dict = Depends(require_admin())):
    from app.auth import get_pending_users
    rows = get_pending_users()
    return [
        {"id": r["id"], "client_id": r["client_id"], "email": r["email"], "role": r["role"],
         "created_at": r["created_at"].strftime("%b %d, %Y %H:%M") if r["created_at"] else ""}
        for r in rows
    ]

# also fix approve_registration_endpoint signature — was a GET with ?email= query param,
# api.ts now POSTs a body. Update the existing route:


@app.get("/admin/users")
def admin_get_all_users(user: dict = Depends(require_admin())):
    from app.auth import get_all_users
    users = get_all_users()
    return {"success": True, "users": users}



class SetUserStatusRequest(BaseModel):
    client_id: str
    status: str  # 'active' or 'inactive'

@app.post("/admin/set-user-status")
def set_user_status_endpoint(data: SetUserStatusRequest, user: dict = Depends(require_admin())):
    from app.auth import set_user_status
    res = set_user_status(data.client_id, data.status)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class ClientProfileRequest(BaseModel):
    client_id: str
    name: str = None
    phone_number: str = None
    login_email: str = None
    imap_email: str = None
    imap_password: str = None
    agent_type: str = None
    department_name: str = None
    company_name: str = None

@app.post("/admin/client-profile")
def set_client_profile(data: ClientProfileRequest, user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            # Update users table if exists
            cursor.execute("""
                UPDATE users 
                SET name=%s, phone_number=%s, email=%s
                WHERE client_id=%s
            """, (data.name or "", data.phone_number or "", data.login_email or "", data.client_id))
            
            # Update email_accounts table
            cursor.execute("""
                UPDATE email_accounts 
                SET agent_type=%s, department_name=%s, company_name=%s, email=%s, password=%s
                WHERE client_id=%s
            """, (data.agent_type or "", data.department_name or "", data.company_name or "", data.imap_email or "", data.imap_password or "", data.client_id))
            
            db.commit()
    return {"success": True}


class SelfProfileRequest(BaseModel):
    client_id: str
    department_name: str = None
    company_name: str = None
    score_threshold: int = None
    agent_type: str = None
    response_tone: str = None

@app.post("/client/profile")
def update_self_profile(data: SelfProfileRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            updates = []
            params = []
            if data.department_name is not None:
                updates.append("department_name = %s")
                params.append(data.department_name)
            if data.company_name is not None:
                updates.append("company_name = %s")
                params.append(data.company_name)
            if data.score_threshold is not None:
                updates.append("score_threshold = %s")
                params.append(data.score_threshold)
            if data.agent_type is not None:
                updates.append("agent_type = %s")
                params.append(data.agent_type)
            if data.response_tone is not None:
                updates.append("response_tone = %s")
                params.append(data.response_tone)
            
            if updates:
                if data.client_id == "ALL" and user.get("role") == "admin":
                    query = f"UPDATE email_accounts SET {', '.join(updates)}"
                    cursor.execute(query, tuple(params))
                else:
                    params.append(data.client_id)
                    query = f"UPDATE email_accounts SET {', '.join(updates)} WHERE client_id = %s"
                    cursor.execute(query, tuple(params))
                db.commit()
    return {"success": True}



@app.delete("/admin/delete-client/{client_id}")
def delete_client(client_id: str, user: dict = Depends(require_admin())):
    from app.auth import delete_client_account
    res = delete_client_account(client_id)
    if not res["success"]:
        raise HTTPException(status_code=404, detail=res["error"])
    return res



class BlockedKeywordRequest(BaseModel):
    client_id: str
    keyword: str

@app.post("/blocked-keywords/add")
def add_blocked_keyword(data: BlockedKeywordRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            from app.keyword_filter import _ensure_table
            _ensure_table(cursor)
            cursor.execute(
                "INSERT IGNORE INTO blocked_keywords (client_id, keyword) VALUES (%s, %s)",
                (data.client_id, data.keyword.strip())
            )
            db.commit()
    return {"status": "success"}

@app.delete("/blocked-keywords/{client_id}/{keyword}")
def remove_blocked_keyword(client_id: str, keyword: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    from app.keyword_filter import _ensure_table
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            _ensure_table(cursor)
            cursor.execute("DELETE FROM blocked_keywords WHERE client_id=%s AND keyword=%s", (client_id, keyword))
            db.commit()
    return {"status": "success"}

@app.get("/blocked-keywords/{client_id}")
def list_blocked_keywords(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    from app.keyword_filter import get_blocked_keywords
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            return {"keywords": get_blocked_keywords(cursor, client_id)}


# ===== Email Disclaimers Management =====
class EmailDisclaimerCreateRequest(BaseModel):
    client_id: str
    disclaimer_text: str

class EmailDisclaimerToggleRequest(BaseModel):
    is_active: bool

@app.get("/email-disclaimers/{client_id}")
def get_email_disclaimers_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    return get_client_disclaimers(client_id)

@app.post("/email-disclaimers")
def create_email_disclaimer_endpoint(data: EmailDisclaimerCreateRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    new_id = add_client_disclaimer(data.client_id, data.disclaimer_text)
    return {"status": "success", "id": new_id}

@app.delete("/email-disclaimers/{disclaimer_id}")
def delete_email_disclaimer_endpoint(disclaimer_id: int, client_id: str = None, user: dict = Depends(get_current_user)):
    if client_id:
        require_client_access(client_id, user)
    deleted = delete_client_disclaimer(disclaimer_id, client_id)
    return {"status": "success", "deleted": deleted}

@app.patch("/email-disclaimers/{disclaimer_id}/toggle")
def toggle_email_disclaimer_endpoint(disclaimer_id: int, data: EmailDisclaimerToggleRequest, client_id: str = None, user: dict = Depends(get_current_user)):
    if client_id:
        require_client_access(client_id, user)
    toggled = toggle_client_disclaimer(disclaimer_id, data.is_active, client_id)
    return {"status": "success", "toggled": toggled}





class UpdateBlockedEmailStatusRequest(BaseModel):
    status: str  # 'ignored' or 'replied'

@app.get("/blocked-emails/{client_id}")
def list_blocked_emails(
    client_id: str,
    status: str = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    from app.keyword_filter import _ensure_reply_blocked_table
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            _ensure_reply_blocked_table(cursor)
            if status:
                cursor.execute("""
                    SELECT id, from_email, subject, body, matched_keyword, status, created_at
                    FROM reply_blocked_by_keyword
                    WHERE client_id = %s AND status = %s
                    ORDER BY created_at DESC
                """, (client_id, status))
            else:
                cursor.execute("""
                    SELECT id, from_email, subject, body, matched_keyword, status, created_at
                    FROM reply_blocked_by_keyword
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()
    return [
        {
            "id": r[0],
            "from_email": r[1],
            "subject": r[2],
            "body": r[3],
            "matched_keyword": r[4],
            "status": r[5],
            "created_at": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else ""
        }
        for r in rows
    ]


@app.patch("/blocked-emails/{client_id}/{record_id}")
def update_blocked_email_status(
    client_id: str,
    record_id: int,
    data: UpdateBlockedEmailStatusRequest,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    if data.status not in ("ignored", "replied"):
        raise HTTPException(status_code=400, detail="status must be 'ignored' or 'replied'")
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE reply_blocked_by_keyword
                SET status = %s
                WHERE id = %s AND client_id = %s
            """, (data.status, record_id, client_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Record not found")
            db.commit()
    return {"status": "success"}


@app.patch("/blocked-emails/{client_id}/bulk-ignore")
def bulk_ignore_blocked_emails(
    client_id: str,
    user: dict = Depends(get_current_user)
):
    """Marks all pending_review rows as ignored for this client."""
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE reply_blocked_by_keyword
                SET status = 'ignored'
                WHERE client_id = %s AND status = 'pending_review'
            """, (client_id,))
            affected = cursor.rowcount
            db.commit()
    return {"status": "success", "rows_updated": affected}



class UrlAllowlistRequest(BaseModel):
    url: str

@app.post("/admin/url-allowlist", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def add_url_allowlist_endpoint(data: UrlAllowlistRequest, user: dict = Depends(require_admin())):
    from app.url_allowlist import add_url_to_allowlist
    try:
        entry_id = add_url_to_allowlist(data.url, user.get("client_id", "admin"))
        return {"status": "success", "id": entry_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/admin/url-allowlist")
def list_url_allowlist_endpoint(user: dict = Depends(require_admin())):
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id, scheme, netloc, path, added_by, created_at FROM url_allowlist ORDER BY created_at DESC")
            rows = cursor.fetchall()
    return [{"id": r[0], "scheme": r[1], "netloc": r[2], "path": r[3], "added_by": r[4], "created_at": str(r[5])} for r in rows]





# ==============================
# 🔌 Connector Config Endpoints
# ==============================

class ConnectorConfigCreateRequest(BaseModel):
    client_id: str
    trigger_type: str
    http_method: str
    url: str
    headers_template: str | None = None
    request_template: str | None = None
    response_mapping: str | None = None
    auth_type: str
    auth_secret: str | None = None
    auth_field_name: str | None = None
    payload_encoding: str = "plain"
    base64_query_param_name: str | None = None
    status: str = "pending_approval"  # "draft" or "pending_approval" only — never "live" via this endpoint

@app.post("/admin/connector-configs", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def create_connector_config(data: ConnectorConfigCreateRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    if data.status not in ("draft", "pending_approval"):
        raise HTTPException(status_code=400, detail="status must be 'draft' or 'pending_approval'")

    from app.connector_config import insert_connector_config_checked, CapExceededError, CreationRaceError
    from app.email_credential import get_connector_cap
    from app.secrets_crypto import encrypt_secret

    cap = get_connector_cap(data.client_id)

    payload = {
        "http_method": data.http_method,
        "url": data.url,
        "headers_template": data.headers_template,
        "request_template": data.request_template,
        "response_mapping": data.response_mapping,
        "auth_type": data.auth_type,
        "auth_secret_encrypted": encrypt_secret(data.auth_secret) if data.auth_secret else None,
        "auth_field_name": data.auth_field_name,
        "payload_encoding": data.payload_encoding,
        "base64_query_param_name": data.base64_query_param_name,
        "created_by": user.get("client_id", user.get("email", "unknown")),
    }

    try:
        row_id = insert_connector_config_checked(data.client_id, data.trigger_type, data.status, payload, cap)
        return {"status": "success", "id": row_id}
    except CapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CreationRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/admin/connector-configs/{client_id}")
def list_connector_configs(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.db import get_db_ctx
    import json as _json
    import logging
    logger = logging.getLogger(__name__)

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if client_id == "ALL":
                cursor.execute("""
                    SELECT id, client_id, trigger_type, http_method, url, response_mapping,
                           auth_type, status, version, created_by, approved_by, approved_at, created_at,
                           headers_template, request_template, auth_field_name, payload_encoding, base64_query_param_name,
                           auth_secret_encrypted
                    FROM connector_configs ORDER BY created_at DESC
                """)
            else:
                cursor.execute("""
                    SELECT id, client_id, trigger_type, http_method, url, response_mapping,
                           auth_type, status, version, created_by, approved_by, approved_at, created_at,
                           headers_template, request_template, auth_field_name, payload_encoding, base64_query_param_name,
                           auth_secret_encrypted
                    FROM connector_configs WHERE client_id=%s ORDER BY created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()

    configs = []
    for r in rows:
        has_regex = False
        if r[5]:
            try:
                mapping = _json.loads(r[5])
                has_regex = any(f.get("extract_regex") for f in mapping.get("fields", []))
            except Exception:
                pass

        oauth_meta = {}
        auth_secret_enc = r[18]
        if auth_secret_enc and r[6] == "oauth2_client_credentials":
            try:
                from app.secrets_crypto import decrypt_secret
                decrypted = decrypt_secret(auth_secret_enc)
                oauth_json = _json.loads(decrypted)
                oauth_meta = {
                    "oauth_token_url": oauth_json.get("token_url", ""),
                    "oauth_client_id": oauth_json.get("client_id", ""),
                    "oauth_grant_type": oauth_json.get("grant_type", "client_credentials"),
                    "oauth_header_prefix": oauth_json.get("header_prefix", "Bearer"),
                    "oauth_scope": oauth_json.get("scope", ""),
                    "oauth_token_auth_method": oauth_json.get("token_auth_method", "client_secret_post"),
                    "oauth_has_secret": bool(oauth_json.get("client_secret")),
                    "oauth_has_refresh_token": bool(oauth_json.get("refresh_token")),
                }
            except Exception as e:
                logger.warning(f"Failed to decrypt OAuth metadata for connector id={r[0]}: {e}")

        configs.append({
            "id": r[0], "client_id": r[1], "trigger_type": r[2], "http_method": r[3],
            "url": r[4], "response_mapping": r[5], "auth_type": r[6], "status": r[7],
            "version": r[8], "created_by": r[9], "approved_by": r[10],
            "approved_at": str(r[11]) if r[11] else None, "created_at": str(r[12]),
            "headers_template": r[13], "request_template": r[14],
            "auth_field_name": r[15], "payload_encoding": r[16], "base64_query_param_name": r[17],
            "requires_regex_review": has_regex,  # flagged for reviewer, per spec
            **oauth_meta,
        })
    return configs


class ConnectorConfigApproveRequest(BaseModel):
    client_id: str

@app.post("/admin/connector-configs/{config_id}/approve")
def approve_connector_config_endpoint(config_id: int, data: ConnectorConfigApproveRequest, user: dict = Depends(require_admin())):
    require_client_access(data.client_id, user)
    from app.db import get_db_ctx
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT trigger_type FROM connector_configs WHERE id=%s AND client_id=%s AND status='pending_approval'",
                (config_id, data.client_id)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Config not found or not pending_approval")
            server_trigger_type = row[0]

    from app.connector_config import (
        approve_connector_config,
        AllowlistViolationError,
        SwapRaceError,
        CapExceededError,
        TemplateValidationError,
        ResponseMappingValidationError,
    )
    from app.email_credential import get_connector_cap
    admin_id = user.get("client_id", "admin")
    cap = get_connector_cap(data.client_id)

    try:
        approve_connector_config(data.client_id, server_trigger_type, config_id, admin_id, cap)
        return {"status": "success", "id": config_id, "approved": True}
    except AllowlistViolationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (TemplateValidationError, ResponseMappingValidationError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CapExceededError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SwapRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))


class ConnectorConfigRejectRequest(BaseModel):
    client_id: str
    reason: str | None = None

@app.post("/admin/connector-configs/{config_id}/reject")
def reject_connector_config_endpoint(config_id: int, data: ConnectorConfigRejectRequest, user: dict = Depends(require_admin())):
    from app.connector_config import reject_connector_config, SwapRaceError
    try:
        reject_connector_config(config_id, data.client_id, user.get("client_id", "admin"), data.reason)
        return {"status": "success", "id": config_id, "rejected": True}
    except SwapRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/admin/connector-configs/{config_id}")
def delete_connector_config_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    from app.db import get_db_ctx
    from app.connector_config import (
        delete_draft_connector_config,
        delete_pending_connector_config,
        request_delete_disabled_connector,
        approve_delete_connector,
    )

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status == 'draft':
        deleted = delete_draft_connector_config(config_id, cfg_client_id)
        if not deleted:
            raise HTTPException(status_code=400, detail="Failed to delete draft connector.")
        return {"status": "success", "id": config_id, "deleted": True, "message": "Draft connector deleted."}

    elif cfg_status == 'pending_approval':
        deleted = delete_pending_connector_config(config_id, cfg_client_id)
        if not deleted:
            raise HTTPException(status_code=400, detail="Failed to delete pending approval connector.")
        return {"status": "success", "id": config_id, "deleted": True, "message": "Pending approval connector permanently deleted."}

    elif cfg_status in ('live', 'disabled', 'pending_deletion'):
        if is_admin:
            deleted = approve_delete_connector(config_id, cfg_client_id)
            if not deleted:
                raise HTTPException(status_code=400, detail="Failed to delete connector.")
            return {"status": "success", "id": config_id, "deleted": True, "message": f"{cfg_status.capitalize()} connector permanently deleted by admin."}
        else:
            if cfg_status == 'pending_deletion':
                return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Connector takedown / deletion is already pending admin approval."}
            requested = request_delete_disabled_connector(config_id, cfg_client_id)
            if not requested:
                raise HTTPException(status_code=400, detail="Failed to request takedown/deletion for connector.")
            return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Takedown and deletion requested. Awaiting administrator approval."}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete a connector in '{cfg_status}' status."
        )


@app.post("/admin/connector-configs/{config_id}/takedown")
def takedown_connector_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    """
    Take down a live connector:
    - If admin: immediately disables it (takes offline).
    - If client: marks pending_deletion (requests admin to take it down).
    """
    from app.db import get_db_ctx
    from app.connector_config import disable_connector_config, request_delete_disabled_connector

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status != 'live':
        raise HTTPException(status_code=400, detail=f"Connector is not live (current status: '{cfg_status}')")

    if is_admin:
        disabled = disable_connector_config(config_id, cfg_client_id)
        if not disabled:
            raise HTTPException(status_code=400, detail="Could not take down connector.")
        return {"status": "success", "id": config_id, "disabled": True, "message": "Live connector taken down (disabled) by admin."}
    else:
        requested = request_delete_disabled_connector(config_id, cfg_client_id)
        if not requested:
            raise HTTPException(status_code=400, detail="Could not request takedown.")
        return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Takedown requested. Awaiting administrator review."}


@app.post("/admin/connector-configs/{config_id}/request-deletion")
def request_deletion_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    from app.db import get_db_ctx
    from app.connector_config import request_delete_disabled_connector, approve_delete_connector

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status not in ('live', 'disabled', 'pending_deletion'):
        raise HTTPException(
            status_code=400,
            detail=f"Only live or disabled connectors can have deletion requested. Current status: '{cfg_status}'"
        )

    if is_admin:
        deleted = approve_delete_connector(config_id, cfg_client_id)
        return {"status": "success", "id": config_id, "deleted": True, "message": "Connector deleted by admin."}

    requested = request_delete_disabled_connector(config_id, cfg_client_id)
    if not requested:
        raise HTTPException(status_code=400, detail="Could not request deletion.")
    return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Deletion request submitted. Awaiting admin approval."}


@app.post("/admin/connector-configs/{config_id}/approve-deletion")
def approve_deletion_endpoint(config_id: int, user: dict = Depends(require_admin())):
    from app.connector_config import approve_delete_connector
    deleted = approve_delete_connector(config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connector not found or not in live/disabled/pending_deletion status")
    return {"status": "success", "id": config_id, "deleted": True, "message": "Connector deletion approved and permanently deleted."}


@app.post("/admin/connector-configs/{config_id}/reject-deletion")
def reject_deletion_endpoint(config_id: int, user: dict = Depends(require_admin())):
    from app.connector_config import reject_delete_connector
    reverted = reject_delete_connector(config_id)
    if not reverted:
        raise HTTPException(status_code=404, detail="Connector not found or not in pending_deletion status")
    return {"status": "success", "id": config_id, "rejected": True, "message": "Deletion request rejected. Connector remains disabled."}


@app.post("/admin/connector-configs/{config_id}/cancel-deletion")
def cancel_deletion_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    from app.db import get_db_ctx
    from app.connector_config import cancel_delete_request

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    cancelled = cancel_delete_request(config_id, cfg_client_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Could not cancel deletion request (connector may not be pending deletion).")
    return {"status": "success", "id": config_id, "cancelled": True, "message": "Deletion request cancelled."}




class ConnectorConfigEditRequest(BaseModel):
    client_id: str
    trigger_type: str
    http_method: str
    url: str
    headers_template: str | None = None
    request_template: str | None = None
    response_mapping: str | None = None
    auth_type: str
    auth_secret: str | None = None
    auth_field_name: str | None = None
    payload_encoding: str = "plain"
    base64_query_param_name: str | None = None
    status: str = "pending_approval"

@app.post("/admin/connector-configs/regenerate", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def regenerate_connector_config(data: ConnectorConfigEditRequest, user: dict = Depends(get_current_user)):
    """
    Mechanical 'edit' — creates a new pending_approval (or draft) row for the same (client_id,
    trigger_type). The existing live row, if any, keeps serving
    unaffected until this new row is explicitly approved via
    swap_to_live — this endpoint does NOT touch the current live row.
    """
    require_client_access(data.client_id, user)
    if data.status not in ("draft", "pending_approval"):
        raise HTTPException(status_code=400, detail="status must be 'draft' or 'pending_approval'")

    from app.connector_config import insert_connector_config_checked, CapExceededError, CreationRaceError
    from app.email_credential import get_connector_cap
    from app.secrets_crypto import encrypt_secret, decrypt_secret
    from app.db import get_db_ctx
    import json as _json

    cap = get_connector_cap(data.client_id)

    auth_secret_enc = None
    if data.auth_secret:
        if data.auth_type == "oauth2_client_credentials":
            try:
                oauth_data = _json.loads(data.auth_secret)
                # Check if we need to backfill existing secrets
                if oauth_data.get("client_secret") == "__KEEP_EXISTING__" or oauth_data.get("refresh_token") == "__KEEP_EXISTING__":
                    with get_db_ctx() as db:
                        with db.cursor() as cursor:
                            cursor.execute(
                                "SELECT auth_secret_encrypted FROM connector_configs WHERE client_id=%s AND trigger_type=%s AND auth_secret_encrypted IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                                (data.client_id, data.trigger_type)
                            )
                            prev_row = cursor.fetchone()
                            if prev_row and prev_row[0]:
                                try:
                                    prev_plain = decrypt_secret(prev_row[0])
                                    prev_oauth = _json.loads(prev_plain)
                                    if oauth_data.get("client_secret") == "__KEEP_EXISTING__":
                                        oauth_data["client_secret"] = prev_oauth.get("client_secret", "")
                                    if oauth_data.get("refresh_token") == "__KEEP_EXISTING__":
                                        oauth_data["refresh_token"] = prev_oauth.get("refresh_token", "")
                                except Exception:
                                    pass
                auth_secret_enc = encrypt_secret(_json.dumps(oauth_data))
            except Exception:
                auth_secret_enc = encrypt_secret(data.auth_secret)
        else:
            auth_secret_enc = encrypt_secret(data.auth_secret)
    else:
        # Fallback to existing if updating
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT auth_secret_encrypted FROM connector_configs WHERE client_id=%s AND trigger_type=%s AND auth_secret_encrypted IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                    (data.client_id, data.trigger_type)
                )
                prev_row = cursor.fetchone()
                auth_secret_enc = prev_row[0] if prev_row else None

    payload = {
        "http_method": data.http_method,
        "url": data.url,
        "headers_template": data.headers_template,
        "request_template": data.request_template,
        "response_mapping": data.response_mapping,
        "auth_type": data.auth_type,
        "auth_secret_encrypted": auth_secret_enc,
        "auth_field_name": data.auth_field_name,
        "payload_encoding": data.payload_encoding,
        "base64_query_param_name": data.base64_query_param_name,
        "created_by": user.get("client_id", user.get("email", "unknown")),
    }

    try:
        row_id = insert_connector_config_checked(
            data.client_id, data.trigger_type, data.status, payload, cap
        )
        msg_suffix = "waiting for approval" if data.status == "pending_approval" else "saved as draft"
        return {
            "status": "success",
            "id": row_id,
            "message": f"New version ({msg_suffix}) created for trigger_type='{data.trigger_type}'. "
                       f"Existing live config (if any) continues serving until this is approved."
        }
    except CapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CreationRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class OAuthTestRequest(BaseModel):
    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str | None = None
    grant_type: str | None = None
    scope: str | None = None
    token_auth_method: str = "client_secret_post"  # or client_secret_basic
    header_prefix: str | None = "Bearer"

@app.post("/admin/connector-configs/test-oauth", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def test_oauth_endpoint(data: OAuthTestRequest, user: dict = Depends(get_current_user)):
    import time
    import requests
    start_time = time.time()

    effective_grant = data.grant_type or ("refresh_token" if data.refresh_token else "client_credentials")
    post_data = {"grant_type": effective_grant}
    if effective_grant == "refresh_token":
        if not data.refresh_token:
            return {
                "success": False,
                "error": "grant_type=refresh_token requires refresh_token to be provided",
                "duration_ms": 0
            }
        post_data["refresh_token"] = data.refresh_token

    if data.scope:
        post_data["scope"] = data.scope

    auth = None
    if data.token_auth_method == "client_secret_basic":
        auth = (data.client_id, data.client_secret)
    else:
        post_data["client_id"] = data.client_id
        post_data["client_secret"] = data.client_secret

    headers = {"Accept": "application/json"}

    try:
        res = requests.post(data.token_url, data=post_data, headers=headers, auth=auth, timeout=10)
        duration_ms = round((time.time() - start_time) * 1000)
        
        if res.status_code != 200:
            return {
                "success": False,
                "status_code": res.status_code,
                "error": f"Token endpoint returned HTTP {res.status_code}: {res.text[:300]}",
                "duration_ms": duration_ms
            }

        res_json = res.json()
        access_token = res_json.get("access_token")
        if not access_token:
            return {
                "success": False,
                "status_code": res.status_code,
                "error": f"No 'access_token' found in JSON response: {res_json}",
                "duration_ms": duration_ms
            }

        return {
            "success": True,
            "token_type": res_json.get("token_type", data.header_prefix or "Bearer"),
            "expires_in": res_json.get("expires_in", 3600),
            "scope": res_json.get("scope", data.scope or ""),
            "duration_ms": duration_ms,
            "message": f"OAuth 2.0 ({effective_grant}) token handshake successful!"
        }
    except Exception as e:
        duration_ms = round((time.time() - start_time) * 1000)
        return {
            "success": False,
            "error": str(e),
            "duration_ms": duration_ms
        }


class GenerateTemplatePreviewRequest(BaseModel):
    client_id: str
    trigger_type: str
    crm_schema_description: str
    sample_response: str = ""

@app.post("/admin/connector-configs/generate-preview", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def generate_connector_template_preview(data: GenerateTemplatePreviewRequest, user: dict = Depends(get_current_user)):
    """
    Preview-only: calls the LLM to draft a request_template + response_mapping
    for review. Does NOT write to the database. Client must review (and may
    edit) the returned template, then submit it via POST /admin/connector-configs
    to actually persist it as pending_approval.
    """
    require_client_access(data.client_id, user)

    from app.connector_config import generate_connector_template

    result = generate_connector_template(
        trigger_type=data.trigger_type,
        crm_schema_description=data.crm_schema_description,
        sample_response=data.sample_response,
    )

    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error", "Template generation failed"))

    return {
        "status": "success",
        "trigger_type": data.trigger_type,
        "request_template": result["request_template"],
        "response_mapping": result["response_mapping"],
        "note": "This is a preview only — nothing has been saved. Review/edit as needed, "
                "then POST to /admin/connector-configs to submit for approval."
    }


# ─────────────────────────────────────────────────────────────
# DRAFTS & MANUAL REVIEW ENDPOINTS
# ─────────────────────────────────────────────────────────────

class UpdateDraftRequest(BaseModel):
    subject: Optional[str] = None
    draft_reply: Optional[str] = None
    to_email: Optional[str] = None


class DiscardDraftRequest(BaseModel):
    rejection_reason: Optional[str] = "Manually discarded"


class BatchSendDraftsRequest(BaseModel):
    draft_ids: list[int]


class BatchSendByFilterRequest(BaseModel):
    search: Optional[str] = None
    intent: Optional[str] = None
    sentiment: Optional[str] = None
    from_email: Optional[str] = None
    min_score: Optional[int] = None
    max_score: Optional[int] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


@app.get("/drafts")
def get_drafts_endpoint(
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
    user: dict = Depends(get_current_user)
):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")

    from app.draft_service import list_drafts
    return list_drafts(
        client_id=target_client,
        status=status,
        search=search,
        intent=intent,
        sentiment=sentiment,
        from_email=from_email,
        min_score=min_score,
        max_score=max_score,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size
    )


@app.get("/drafts/count")
def get_pending_drafts_count_endpoint(client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")
    from app.draft_service import get_pending_drafts_count
    count = get_pending_drafts_count(target_client)
    return {"pending_count": count}


@app.get("/drafts/metrics")
def get_draft_metrics_endpoint(client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")
    from app.draft_service import get_draft_metrics
    return get_draft_metrics(target_client)


@app.get("/drafts/{draft_id}")
def get_single_draft_endpoint(draft_id: int, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import get_draft
    draft = get_draft(draft_id, client_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@app.put("/drafts/{draft_id}")
def update_draft_endpoint(draft_id: int, data: UpdateDraftRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import update_draft
    ok = update_draft(
        draft_id=draft_id,
        client_id=client_id,
        subject=data.subject,
        draft_reply=data.draft_reply,
        to_email=data.to_email,
        reviewed_by=user.get("username", "Admin")
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to update draft or draft not pending")
    return {"status": "success", "message": "Draft updated successfully"}


@app.post("/drafts/{draft_id}/send")
def send_single_draft_endpoint(draft_id: int, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import send_single_draft
    res = send_single_draft(draft_id, client_id=client_id, reviewed_by=user.get("username", "Admin"))
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to dispatch draft"))
    return res


@app.post("/drafts/{draft_id}/discard")
def discard_draft_endpoint(draft_id: int, data: DiscardDraftRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import discard_draft
    ok = discard_draft(draft_id, client_id=client_id, rejection_reason=data.rejection_reason, reviewed_by=user.get("username", "Admin"))
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to discard draft or draft not pending")
    return {"status": "success", "message": f"Draft #{draft_id} discarded"}


@app.post("/drafts/batch-send")
def batch_send_drafts_endpoint(data: BatchSendDraftsRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import batch_send_drafts
    return batch_send_drafts(data.draft_ids, client_id=client_id, reviewed_by=user.get("username", "Admin"))


@app.post("/drafts/batch-send-filter")
def batch_send_by_filter_endpoint(data: BatchSendByFilterRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    from app.draft_service import batch_send_by_filter
    return batch_send_by_filter(data.dict(), client_id=client_id, reviewed_by=user.get("username", "Admin"))