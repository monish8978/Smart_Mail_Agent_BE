"""
embed_service.py

Standalone microservice that loads intfloat/multilingual-e5-small ONCE at
startup and serves embeddings over HTTP. Deliberately separate from the
Celery worker/API containers — see migration spec (1 replica, no LB,
lightweight Dockerfile.embed).

Callers (app/embed_client.py) are responsible for adding the "query: " /
"passage: " prefixes required by e5 models — this service does not add
them itself, so that prefix logic lives in one place and is easy to audit.
"""

import logging
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("embed_service")

MODEL_NAME = "intfloat/multilingual-e5-small"
EXPECTED_DIM = 384
MAX_BATCH_SIZE = 64

app = FastAPI(title="mail_ai_embed_service")

_model = None


@app.on_event("startup")
def load_model():
    global _model
    from sentence_transformers import SentenceTransformer
    logger.info(f"🔄 Loading embedding model '{MODEL_NAME}' — this happens ONCE at startup...")
    _model = SentenceTransformer(MODEL_NAME)
    dim = _model.get_embedding_dimension()
    if dim != EXPECTED_DIM:
        logger.warning(f"⚠️ Loaded model dim={dim}, expected {EXPECTED_DIM}. Qdrant collection config must match.")
    logger.info(f"✅ Model loaded. Embedding dim={dim}")

    # Cold-start warmup: on CPU, the FIRST inference call after model load
    # pays a one-off thread-pool/BLAS warmup cost that can exceed the
    # caller's 5s HTTP timeout (observed: ~5.2s on first call, <1s on every
    # call after). Without this, whichever real request happens to be first
    # silently gets dropped into the JSON fallback store instead of Qdrant.
    try:
        warmup_start = time.time()
        _model.encode(["passage: warmup"], normalize_embeddings=True)
        logger.info(f"✅ Warmup inference complete in {time.time() - warmup_start:.2f}s — cold-start cost paid before serving real traffic")
    except Exception as e:
        logger.warning(f"⚠️ Warmup inference failed (non-fatal, will retry on first real request): {e}")


class EmbedRequest(BaseModel):
    texts: list[str]

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, v):
        if not v:
            raise ValueError("texts must be a non-empty list")
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch too large: max {MAX_BATCH_SIZE} texts per request")
        return v


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dim: int
    model: str


@app.get("/health")
def health():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    try:
        # NOTE: caller (app/embed_client.py) is responsible for the
        # "query: " / "passage: " prefix — this service embeds exactly
        # what it's given, no implicit prefixing here.
        vectors = _model.encode(req.texts, normalize_embeddings=True).tolist()
        return {"vectors": vectors, "dim": len(vectors[0]) if vectors else 0, "model": MODEL_NAME}
    except Exception as e:
        logger.error(f"❌ Embedding failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("embed_service:app", host="0.0.0.0", port=8500)