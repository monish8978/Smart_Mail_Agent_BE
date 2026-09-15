import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

from app.db_init import initialize_database_and_services
from app.api import (
    auth_router,
    emails_router,
    drafts_router,
    knowledge_router,
    connectors_router,
    analytics_router,
    settings_router,
)


# =====================================================================
# WebSocket Connection Management & Redis Pub/Sub Listener
# =====================================================================

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

    async def broadcast(self, message: str, target_client_id: str = None):
        dead_connections = []
        for connection in list(self.active_connections):
            try:
                # Admins receive everything; clients receive only their own events
                conn_role = getattr(connection.state, "role", None)
                conn_cid = getattr(connection.state, "client_id", None)
                if target_client_id and conn_role != "admin" and conn_cid != target_client_id:
                    continue
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"WebSocket send failed: {e}")
                dead_connections.append(connection)
        for dc in dead_connections:
            self.disconnect(dc)

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
                    # Extract client_id for tenant-filtered broadcast
                    try:
                        import json as _json
                        parsed = _json.loads(data)
                        cid = parsed.get("client_id")
                    except Exception:
                        cid = None
                    await manager.broadcast(data, target_client_id=cid)
        except asyncio.CancelledError:
            logger.info("Redis pubsub listener cancelled")
            break
        except Exception as e:
            logger.error(f"Redis pubsub error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)


# =====================================================================
# Application Lifespan
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialize_database_and_services()
    listener_task = asyncio.create_task(redis_pubsub_listener(app))
    yield
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass


# =====================================================================
# FastAPI Application & Middlewares
# =====================================================================

app = FastAPI(
    title="Mail AI Automation API",
    description="Enterprise Email AI Automation and Customer Support Platform",
    version="2.0.0",
    lifespan=lifespan,
)

_CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:1947,http://localhost,http://172.16.3.215:1947,http://172.16.3.215,http://127.0.0.1:1947,http://127.0.0.1",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip().rstrip("/") for o in _CORS_ORIGINS if o.strip()],
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


# =====================================================================
# Root Endpoints & WebSocket
# =====================================================================

@app.get("/")
def home():
    return {"status": "mail_ai_automation running"}


@app.get("/health")
async def health_check():
    """
    Deep liveness and dependency readiness probe for container orchestrators.
    Verifies MySQL pool, Redis broker, Qdrant vector DB, and Embeddings microservice.
    Returns HTTP 200 if all dependencies are healthy, or HTTP 503 if any dependency fails.
    """
    components = {}
    is_healthy = True

    # 1. Database Connectivity
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        components["database"] = {"status": "healthy"}
    except Exception as e:
        is_healthy = False
        components["database"] = {"status": "unhealthy", "error": str(e)}

    # 2. Redis Broker Connectivity
    try:
        from app.redis_pool import get_redis_main
        r = get_redis_main()
        if r and r.ping():
            components["redis"] = {"status": "healthy"}
        else:
            is_healthy = False
            components["redis"] = {"status": "unhealthy", "error": "Redis ping returned False"}
    except Exception as e:
        is_healthy = False
        components["redis"] = {"status": "unhealthy", "error": str(e)}

    # 3. Qdrant Vector DB
    try:
        import httpx
        q_host = os.getenv("QDRANT_HOST", "mail_ai_qdrant")
        q_port = os.getenv("QDRANT_PORT", "6333")
        q_url = f"http://{q_host}:{q_port}/collections"
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(q_url)
            if resp.status_code == 200:
                components["qdrant"] = {"status": "healthy"}
            else:
                is_healthy = False
                components["qdrant"] = {"status": "unhealthy", "http_status": resp.status_code}
    except Exception as e:
        is_healthy = False
        components["qdrant"] = {"status": "unhealthy", "error": str(e)}

    # 4. Embeddings Microservice
    try:
        import httpx
        emb_url = os.getenv("EMBED_SERVICE_URL", "http://mail_ai_embed_service:8500")
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{emb_url}/health")
            if resp.status_code == 200:
                components["embed_service"] = {"status": "healthy"}
            else:
                is_healthy = False
                components["embed_service"] = {"status": "unhealthy", "http_status": resp.status_code}
    except Exception as e:
        is_healthy = False
        components["embed_service"] = {"status": "unhealthy", "error": str(e)}

    payload = {
        "status": "healthy" if is_healthy else "degraded",
        "version": "2.0.0",
        "components": components
    }
    return JSONResponse(status_code=200 if is_healthy else 503, content=payload)



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = None):
    # Validate session before accepting connection
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return
    from app.auth import get_session
    session = get_session(token)
    if not session:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    # Store session info on the websocket for tenant-filtered broadcasts
    websocket.state.client_id = session.get("client_id")
    websocket.state.role = session.get("role")
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


# =====================================================================
# Include Modular Domain Routers
# =====================================================================

app.include_router(auth_router)
app.include_router(emails_router)
app.include_router(drafts_router)
app.include_router(knowledge_router)
app.include_router(connectors_router)
app.include_router(analytics_router)
app.include_router(settings_router)