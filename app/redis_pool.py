import os
import redis
import logging

logger = logging.getLogger(__name__)

_pools = {}


def _get_pool(url: str) -> redis.ConnectionPool:
    if url not in _pools:
        _pools[url] = redis.ConnectionPool.from_url(url, decode_responses=True)
        logger.info(f"🔌 Created Redis connection pool for {url}")
    return _pools[url]


def get_redis_main() -> redis.Redis:
    """DB 0 — pub/sub, dedup, rate limiting, action outbox."""
    url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0")
    return redis.Redis(connection_pool=_get_pool(url))


def get_redis_history() -> redis.Redis:
    """DB 1 — chat history."""
    url = os.getenv("REDIS_HISTORY_URL", "redis://mail_ai_redis:6379/1")
    return redis.Redis(connection_pool=_get_pool(url))


def get_redis_sessions() -> redis.Redis:
    """DB 2 — sessions & OTP."""
    url = os.getenv("REDIS_SESSION_URL", "redis://mail_ai_redis:6379/2")
    return redis.Redis(connection_pool=_get_pool(url))
