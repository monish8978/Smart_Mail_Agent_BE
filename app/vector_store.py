"""
app/vector_store.py

Thin wrapper around the Qdrant client for the mail_ai_automation system.

Design decisions (see migration spec — not re-litigated here):
  - ONE collection for all clients: `mail_ai_knowledge`
  - client_id is a payload field, used as a Qdrant filter for isolation
  - Embedding is NOT performed here — this module only talks to Qdrant.
    Embeddings are obtained from `app/embed_client.py` (HTTP call to embed_service).
  - Vector size: 384 (intfloat/multilingual-e5-small)
"""

import os
import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

logger = logging.getLogger(__name__)

QDRANT_HOST = os.getenv("QDRANT_HOST", "mail_ai_qdrant")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "mail_ai_knowledge")
VECTOR_SIZE = 384  # intfloat/multilingual-e5-small

_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient | None:
    """
    Returns a singleton QdrantClient, or None if the connection cannot be
    established. Callers MUST treat None as "Qdrant is down" and fall back
    to the JSON fallback store — never let a Qdrant outage propagate as an
    unhandled exception into worker/tasks.py.
    """
    global _qdrant_client
    if _qdrant_client is not None:
        return _qdrant_client

    try:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5)
        # Cheap connectivity check
        client.get_collections()
        _qdrant_client = client
        logger.info(f"✅ Connected to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}")
        return _qdrant_client
    except Exception as e:
        logger.error(f"❌ Failed to connect to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}: {e}")
        return None


def ensure_collection() -> bool:
    """
    Idempotently ensures the single shared collection exists with the
    correct vector size / distance metric. Returns True on success.
    """
    client = get_qdrant_client()
    if client is None:
        return False

    try:
        existing = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=VECTOR_SIZE,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info(f"✅ Created Qdrant collection '{COLLECTION_NAME}'")

        # Payload index on client_id massively speeds up filtered search/delete
        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="client_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
        except Exception:
            # Already exists — fine
            pass

        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="doc_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"❌ Failed to ensure Qdrant collection: {e}")
        return False


def upsert_chunks(
    client_id: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    vectors: list[list[float]],
) -> bool:
    """
    Upserts one document's worth of chunks (+ their pre-computed embeddings)
    into the shared collection, tagged with client_id / doc_id metadata.
    """
    client = get_qdrant_client()
    if client is None:
        return False
    if not ensure_collection():
        return False
    if len(chunks) != len(vectors):
        logger.error("❌ upsert_chunks: chunks/vectors length mismatch")
        return False

    try:
        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_id}_chunk_{i}"))
            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "client_id": client_id,
                        "doc_id": doc_id,
                        "title": title,
                        "chunk_index": i,
                        "total_chunks": len(chunks),
                        "content": chunk,
                    },
                )
            )
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        logger.info(f"✅ Upserted {len(points)} chunks for doc_id={doc_id} client_id={client_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Qdrant upsert failed: {e}")
        return False


def search(client_id: str, query_vector: list[float], top_k: int = 3) -> list[dict[str, Any]]:
    """
    Filtered ANN search scoped to client_id. Returns a list of dicts:
    [{"content": str, "title": str, "doc_id": str, "score": float, "id": str}, ...]
    Returns [] on any failure — callers treat that identically to "no matches".
    """
    client = get_qdrant_client()
    if client is None:
        return []

    try:
        # NOTE: qdrant-client removed the old `.search()` method in favor of
        # `.query_points()` (see qdrant-client changelog, "remove deprecated
        # methods: search, recommend, ..."). Using `.search()` here would
        # hard-fail with AttributeError on current client versions.
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(key="client_id", match=qmodels.MatchValue(value=client_id))]
            ),
            limit=top_k,
        )
        out = []
        for r in response.points:
            payload = r.payload or {}
            out.append({
                "id": str(r.id),
                "content": payload.get("content", ""),
                "title": payload.get("title", "Untitled Document"),
                "doc_id": payload.get("doc_id", ""),
                "score": round(float(r.score), 3),
            })
        return out
    except Exception as e:
        logger.error(f"❌ Qdrant search failed: {e}")
        return []


def search_all_clients(query_vector: list[float], top_k: int = 3) -> list[dict[str, Any]]:
    """Unfiltered search across all clients — used for the ALL/admin views."""
    client = get_qdrant_client()
    if client is None:
        return []
    try:
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=top_k,
        )
        out = []
        for r in response.points:
            payload = r.payload or {}
            out.append({
                "id": str(r.id),
                "content": payload.get("content", ""),
                "title": payload.get("title", "Untitled Document"),
                "doc_id": payload.get("doc_id", ""),
                "client_id": payload.get("client_id", ""),
                "score": round(float(r.score), 3),
            })
        return out
    except Exception as e:
        logger.error(f"❌ Qdrant search_all_clients failed: {e}")
        return []


def get_client_documents(client_id: str) -> list[dict[str, Any]]:
    """
    Scrolls all points for a client and groups them by doc_id, mirroring
    the old Chroma get_knowledge_base() grouping behaviour.
    """
    client = get_qdrant_client()
    if client is None:
        return []

    try:
        unique_docs: dict[str, dict] = {}
        next_offset = None
        while True:
            points, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="client_id", match=qmodels.MatchValue(value=client_id))]
                ),
                limit=256,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                doc_id = payload.get("doc_id", str(p.id))
                title = payload.get("title", "Untitled Document")
                content = payload.get("content", "")
                if doc_id not in unique_docs:
                    unique_docs[doc_id] = {
                        "id": doc_id,
                        "title": title,
                        "chunks_count": 1,
                        "content": content,
                    }
                else:
                    unique_docs[doc_id]["chunks_count"] += 1
                    if len(unique_docs[doc_id]["content"]) < 300:
                        unique_docs[doc_id]["content"] += "\n" + content
            if next_offset is None:
                break
        return list(unique_docs.values())
    except Exception as e:
        logger.error(f"❌ Qdrant get_client_documents failed: {e}")
        return []


def get_all_documents() -> list[dict[str, Any]]:
    """Same as get_client_documents but across all clients (ALL view)."""
    client = get_qdrant_client()
    if client is None:
        return []
    try:
        unique_docs: dict[str, dict] = {}
        next_offset = None
        while True:
            points, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                limit=256,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                doc_id = payload.get("doc_id", str(p.id))
                cid = payload.get("client_id", "")
                title = payload.get("title", "Untitled Document")
                content = payload.get("content", "")
                if doc_id not in unique_docs:
                    unique_docs[doc_id] = {
                        "id": doc_id,
                        "title": f"[{cid}] {title}",
                        "chunks_count": 1,
                        "content": content,
                        "client_id": cid,
                    }
                else:
                    unique_docs[doc_id]["chunks_count"] += 1
                    if len(unique_docs[doc_id]["content"]) < 300:
                        unique_docs[doc_id]["content"] += "\n" + content
            if next_offset is None:
                break
        return list(unique_docs.values())
    except Exception as e:
        logger.error(f"❌ Qdrant get_all_documents failed: {e}")
        return []


def delete_document(client_id: str, doc_id: str) -> bool:
    """Deletes all chunks for a given doc_id, scoped to client_id for safety."""
    client = get_qdrant_client()
    if client is None:
        return False
    try:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(key="client_id", match=qmodels.MatchValue(value=client_id)),
                        qmodels.FieldCondition(key="doc_id", match=qmodels.MatchValue(value=doc_id)),
                    ]
                )
            ),
        )
        logger.info(f"✅ Deleted doc_id={doc_id} for client_id={client_id} from Qdrant")
        return True
    except Exception as e:
        logger.error(f"❌ Qdrant delete_document failed: {e}")
        return False


def delete_client_data(client_id: str) -> bool:
    """Deletes ALL points for a client — used by delete_client_account()."""
    client = get_qdrant_client()
    if client is None:
        return False
    try:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="client_id", match=qmodels.MatchValue(value=client_id))]
                )
            ),
        )
        logger.info(f"✅ Purged all Qdrant data for client_id={client_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Qdrant delete_client_data failed: {e}")
        return False


def collection_count(client_id: str | None = None) -> int:
    """Returns point count, optionally filtered by client_id. Returns 0 on failure."""
    client = get_qdrant_client()
    if client is None:
        return 0
    try:
        if client_id:
            result = client.count(
                collection_name=COLLECTION_NAME,
                count_filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="client_id", match=qmodels.MatchValue(value=client_id))]
                ),
                exact=True,
            )
        else:
            result = client.count(collection_name=COLLECTION_NAME, exact=True)
        return result.count
    except Exception as e:
        logger.error(f"❌ Qdrant collection_count failed: {e}")
        return 0
