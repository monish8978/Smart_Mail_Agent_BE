"""
app/embed_client.py

HTTP client the Celery worker / API use to call the standalone embed_service.
Embedding is deliberately NOT done in-process (see migration spec).

Prefix handling (intfloat/multilingual-e5-small requires this — silent
retrieval-quality degradation is the failure mode if it's wrong, not an error):
  - documents being STORED  -> "passage: " prefix
  - search QUERIES          -> "query: " prefix

On any failure (timeout, connection error, non-2xx) this returns None/[]
rather than raising — a worker task must NEVER retry because the embed
service is down. Callers treat None/[] identically to "RAG unavailable".
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

EMBED_SERVICE_URL = os.getenv("EMBED_SERVICE_URL", "http://mail_ai_embed_service:8500")
EMBED_TIMEOUT_SECONDS = int(os.getenv("EMBED_TIMEOUT_SECONDS", "25"))


BATCH_SIZE = 32

# In-memory LRU cache fallback (up to 1000 items)
_MEM_QUERY_CACHE: dict[str, list[float]] = {}
_MAX_MEM_CACHE_SIZE = 1000


def _get_query_cache(key: str) -> list[float] | None:
    # 1. Try Redis first
    try:
        from app.redis_pool import get_redis_main
        import json
        r = get_redis_main()
        if r:
            cached_val = r.get(f"embed_cache:query:{key}")
            if cached_val:
                return json.loads(cached_val)
    except Exception:
        pass

    # 2. Try in-memory cache
    return _MEM_QUERY_CACHE.get(key)


def _set_query_cache(key: str, vector: list[float]):
    if not vector:
        return
    # 1. Store in Redis with 24-hour expiration
    try:
        from app.redis_pool import get_redis_main
        import json
        r = get_redis_main()
        if r:
            r.setex(f"embed_cache:query:{key}", 86400, json.dumps(vector))
    except Exception:
        pass

    # 2. Store in in-memory LRU
    if len(_MEM_QUERY_CACHE) >= _MAX_MEM_CACHE_SIZE:
        # Evict oldest entry
        try:
            oldest_key = next(iter(_MEM_QUERY_CACHE))
            _MEM_QUERY_CACHE.pop(oldest_key, None)
        except Exception:
            _MEM_QUERY_CACHE.clear()
    _MEM_QUERY_CACHE[key] = vector


def _post_embed(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        try:
            resp = requests.post(
                f"{EMBED_SERVICE_URL}/embed",
                json={"texts": batch},
                timeout=EMBED_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            vectors = data.get("vectors")
            if not vectors or len(vectors) != len(batch):
                logger.error(f"❌ embed_service returned malformed response for batch: {data}")
                return None
            all_vectors.extend(vectors)
        except requests.exceptions.Timeout:
            logger.error(f"❌ embed_service timed out after {EMBED_TIMEOUT_SECONDS}s — degrading gracefully")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ embed_service connection failed: {e} — degrading gracefully")
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ embed_service returned HTTP error: {e} — degrading gracefully")
            return None
        except Exception as e:
            logger.error(f"❌ embed_service call failed unexpectedly: {e} — degrading gracefully")
            return None
    return all_vectors


def embed_passages(texts: list[str]) -> list[list[float]] | None:
    """Embed documents/chunks for STORAGE. Applies the 'passage: ' prefix."""
    prefixed = [f"passage: {t}" for t in texts]
    return _post_embed(prefixed)


def embed_query(text: str) -> list[float] | None:
    """
    Embed a single search QUERY with the 'query: ' prefix.
    Checks Redis and in-memory LRU cache before hitting the microservice.
    """
    import hashlib
    clean_text = text.strip()
    cache_key = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()

    cached = _get_query_cache(cache_key)
    if cached is not None:
        logger.debug(f"⚡ [Embed Cache Hit] Retrieved query vector for '{clean_text[:40]}...'")
        return cached

    result = _post_embed([f"query: {clean_text}"])
    if result is None:
        return None
    vector = result[0] if result else None
    if vector:
        _set_query_cache(cache_key, vector)
    return vector


def embed_service_healthy() -> bool:
    try:
        resp = requests.get(f"{EMBED_SERVICE_URL}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False

