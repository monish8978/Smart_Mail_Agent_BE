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
EMBED_TIMEOUT_SECONDS = 5


def _post_embed(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        resp = requests.post(
            f"{EMBED_SERVICE_URL}/embed",
            json={"texts": texts},
            timeout=EMBED_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        vectors = data.get("vectors")
        if not vectors or len(vectors) != len(texts):
            logger.error(f"❌ embed_service returned malformed response: {data}")
            return None
        return vectors
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


def embed_passages(texts: list[str]) -> list[list[float]] | None:
    """Embed documents/chunks for STORAGE. Applies the 'passage: ' prefix."""
    prefixed = [f"passage: {t}" for t in texts]
    return _post_embed(prefixed)


def embed_query(text: str) -> list[float] | None:
    """Embed a single search QUERY. Applies the 'query: ' prefix."""
    result = _post_embed([f"query: {text}"])
    if result is None:
        return None
    return result[0] if result else None


def embed_service_healthy() -> bool:
    try:
        resp = requests.get(f"{EMBED_SERVICE_URL}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False
