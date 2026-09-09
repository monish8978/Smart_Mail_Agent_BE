import os
import logging
import redis
from fastapi import Request, HTTPException, status

logger = logging.getLogger(__name__)

class RedisRateLimiter:
    """
    Production-grade Redis-backed request rate limiter for FastAPI.
    Tracks requests per client IP and endpoint path.
    Defends against DoS, brute-forcing, and high LLM API cost surges.
    """
    def __init__(self, limit: int = 60, window: int = 60):
        """
        Args:
            limit: Maximum requests allowed in the time window.
            window: Time window in seconds.
        """
        self.limit = limit
        self.window = window
        self.redis_client = None
        self._init_redis()

    def _init_redis(self):
        try:
            redis_url = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0")
            # Parse fallback if local development or other environment uses standard redis
            if not redis_url:
                redis_url = "redis://localhost:6379/0"
            self.redis_client = redis.from_url(redis_url, decode_responses=True)
            # Test ping
            self.redis_client.ping()
            logger.info(f"🔌 Rate Limiter successfully connected to Redis: {redis_url}")
        except Exception as e:
            logger.warning(f"⚠️ Redis connection for Rate Limiter failed: {e}. Rate limiter is in fallback mode (passthrough).")
            self.redis_client = None

    async def __call__(self, request: Request):
        if not self.redis_client:
            # Safe Fallback: if Redis is offline, do not block the app. Log a warning and allow.
            logger.debug("Rate Limiter running in passthrough mode (Redis offline).")
            return

        # Define unique request key by client IP and endpoint path
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        key = f"rate_limit:{client_ip}:{path}"

        try:
            # Use Redis transactional pipeline for an atomic increment and expire command
            current = self.redis_client.get(key)
            if current is not None and int(current) >= self.limit:
                ttl = self.redis_client.ttl(key)
                logger.warning(f"🚨 Rate limit exceeded for IP={client_ip} on path={path}. TTL={ttl}s")
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error": "Too Many Requests",
                        "message": f"Rate limit exceeded. Maximum {self.limit} requests per {self.window}s window.",
                        "retry_after_seconds": ttl if ttl > 0 else self.window
                    }
                )

            pipeline = self.redis_client.pipeline()
            pipeline.incr(key)
            if current is None:
                pipeline.expire(key, self.window)
            pipeline.execute()

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"❌ Redis Rate Limiter execution error: {e}. Passing request to avoid blocking.")
