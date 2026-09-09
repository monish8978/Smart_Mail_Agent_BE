import os
import logging
from celery import Celery
from celery.signals import worker_process_init
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Get Redis URL from environment
redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0")

# Initialize Celery
celery = Celery(
    "mail_ai_worker",
    broker=redis_url,
    backend=redis_url,
    include=["worker.tasks"]
)

# Optional configuration
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Deliberately kept at 1 — embedding itself is now offloaded to
    # embed_service, so this isn't about avoiding duplicate model loads
    # (there's nothing to load here anymore); it's to keep per-process
    # memory low on a 6GB host at current traffic levels. Revisit if
    # traffic grows — see docker-compose.yml `worker` mem_limit for the
    # matching ceiling.
    worker_concurrency=1,
)


@worker_process_init.connect
def warm_embed_service_connection(**kwargs):
    """
    Fires once per worker process on startup (NOT lazily on first task).
    Embedding itself is no longer preloaded here — that lives entirely in
    embed_service. This just does a cheap health check against
    embed_service so a cold/misconfigured embed_service is visible in
    worker logs at boot rather than silently on the first customer email.
    """
    try:
        from app.embed_client import embed_service_healthy
        if embed_service_healthy():
            logger.info("✅ embed_service reachable at worker startup")
        else:
            logger.warning("⚠️ embed_service NOT reachable at worker startup — RAG will degrade to fallback/tickets until it recovers")
    except Exception as e:
        logger.warning(f"⚠️ embed_service startup health check failed: {e}")


if __name__ == "__main__":
    celery.start()
