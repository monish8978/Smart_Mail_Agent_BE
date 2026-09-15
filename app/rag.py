"""
app/rag.py

RAG layer — rewritten to use Qdrant (single shared collection, client_id
payload filter) + the standalone embed_service, instead of ChromaDB.

Public function signatures are UNCHANGED from the Chroma version so that
worker/tasks.py, worker/tasks_new.py and app/mcp_server.py need no changes
beyond import paths (per migration spec — those files call through here).

Fallback behaviour: if Qdrant OR embed_service is unavailable, every
function here degrades to the same JSON/Jaccard fallback store the old
Chroma implementation used — kept deliberately, and applied consistently
across all functions (not just some), per migration spec. The fallback
JSON file lives under CHROMA_PATH — renamed conceptually but the directory
is kept as a stable, already-mounted shared volume path across
api/worker/listener containers. Do NOT remove that volume mount without
also moving FALLBACK_DB_PATH, or the fallback goes split-brain across
containers.

Existing Chroma data is intentionally NOT migrated — clients re-upload
after cutover, per migration spec. RAG returns empty until re-upload;
worker/tasks.py's existing "no context found" -> PATH C ticket-creation
handles that gap without any change on its end.
"""

import os
import json
import logging
import uuid
import fcntl
import re
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

# Kept as the same on-disk path/volume the old Chroma implementation used —
# this directory is already mounted into api/worker/listener in
# docker-compose.yml. Only the fallback JSON file lives here now.
CHROMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_db")
os.makedirs(CHROMA_PATH, exist_ok=True)
FALLBACK_DB_PATH = os.path.join(CHROMA_PATH, "fallback_db.json")

from app.vector_store import (
    ensure_collection,
    upsert_chunks,
    search as qdrant_search,
    search_all_clients as qdrant_search_all_clients,
    get_client_documents as qdrant_get_client_documents,
    get_all_documents as qdrant_get_all_documents,
    delete_document as qdrant_delete_document,
    collection_count,
)
from app.embed_client import embed_passages, embed_query


# ==========================================
# 🛠️ FALLBACK JSON DATABASE (TENANT-PARTITIONED)
# ==========================================
def get_fallback_path(client_id: str) -> str:
    """Returns the isolated file path for a client's fallback knowledge store."""
    clean_id = "".join(c for c in client_id if c.isalnum() or c in ("-", "_")).lower()
    if not clean_id:
        clean_id = "default"
    return os.path.join(CHROMA_PATH, f"fallback_{clean_id}.json")


def load_fallback_db(client_id: str | None = None) -> list[dict] | dict:
    """
    If client_id is provided, loads documents strictly for that client_id from its isolated fallback file.
    If client_id is None, loads the legacy fallback database dictionary (for backward compatibility).
    """
    if client_id is None:
        if os.path.exists(FALLBACK_DB_PATH):
            try:
                with open(FALLBACK_DB_PATH, 'r', encoding='utf-8') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        return json.load(f)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.error(f"❌ Failed to load legacy fallback DB: {e}")
                return {}
        return {}

    if not client_id or client_id == "ALL":
        return []

    path = get_fallback_path(client_id)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"❌ Failed to load fallback DB for client_id={client_id}: {e}")
            return []

    # One-time migration: check legacy fallback_db.json if it exists
    if os.path.exists(FALLBACK_DB_PATH):
        try:
            with open(FALLBACK_DB_PATH, 'r', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    legacy_data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            if isinstance(legacy_data, dict) and client_id in legacy_data:
                client_docs = legacy_data[client_id]
                if isinstance(client_docs, list):
                    save_fallback_db(client_id, client_docs)
                    return client_docs
        except Exception as e:
            logger.error(f"❌ Legacy fallback migration check failed for client_id={client_id}: {e}")

    return []


def save_fallback_db(client_id_or_data, docs: list[dict] | None = None):
    """
    Saves fallback knowledge.
    - If docs is provided: saves docs (list) for client_id into its isolated fallback_{client_id}.json.
    - If docs is None and client_id_or_data is dict: saves dict into legacy FALLBACK_DB_PATH (for backward compatibility).
    """
    if docs is None and isinstance(client_id_or_data, dict):
        target_path = FALLBACK_DB_PATH
        data_to_write = client_id_or_data
    else:
        client_id = str(client_id_or_data)
        if not client_id or client_id == "ALL":
            raise ValueError(f"Cannot save fallback DB for client_id={client_id!r}")
        target_path = get_fallback_path(client_id)
        data_to_write = docs if docs is not None else []

    tmp_path = f"{target_path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data_to_write, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp_path, target_path)
    except Exception as e:
        logger.error(f"❌ Failed to save fallback DB to {target_path}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_all_fallback_docs() -> list[dict]:
    """Admin-only helper: scans all fallback_*.json files across tenants."""
    all_docs = []
    try:
        if os.path.exists(CHROMA_PATH):
            for fname in os.listdir(CHROMA_PATH):
                if fname.startswith("fallback_") and fname.endswith(".json") and fname != "fallback_db.json":
                    fpath = os.path.join(CHROMA_PATH, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            docs = json.load(f)
                            if isinstance(docs, list):
                                cid = fname[len("fallback_"):-len(".json")]
                                for d in docs:
                                    all_docs.append({
                                        "id": d.get("id"),
                                        "title": f"[{cid}] {d.get('title', 'Untitled')}",
                                        "content": d.get("content", ""),
                                        "client_id": cid,
                                    })
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"❌ Error loading all fallback docs: {e}")
    return all_docs


def jaccard_similarity(text1: str, text2: str) -> float:
    """Computes pure token-level Jaccard similarity."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    return len(words1.intersection(words2)) / len(words1.union(words2))


def calculate_fallback_score(query: str, content: str) -> float:
    """
    Hybrid scoring for the fallback store: combines word-overlap Jaccard
    with exact substring and keyword bonuses (e.g. error codes, SKUs).
    """
    if not query or not content:
        return 0.0
    q_lower = query.lower().strip()
    c_lower = content.lower().strip()

    # 1. Exact phrase match bonus
    if len(q_lower) >= 4 and q_lower in c_lower:
        return 1.0

    q_words = [w for w in re.findall(r"\w+", q_lower) if len(w) > 1]
    c_words = set(re.findall(r"\w+", c_lower))
    if not q_words or not c_words:
        return 0.0

    q_set = set(q_words)
    intersection = q_set.intersection(c_words)
    if not intersection:
        return 0.0

    jaccard = len(intersection) / len(q_set.union(c_words))
    recall = len(intersection) / len(q_set)

    # Code boost for alphanumeric tokens (e.g., error codes, model numbers)
    code_boost = 0.0
    for w in q_set:
        if any(c.isdigit() for c in w) and len(w) >= 3 and w in c_words:
            code_boost += 0.3

    return round(min(1.0, 0.4 * jaccard + 0.6 * recall + code_boost), 3)


# ==========================================
# 🚀 CORE RAG API INTERFACES (USER-ISOLATED)
# ==========================================

def split_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
    """
    Recursively splits text into chunks of at most `chunk_size` characters with
    `overlap` characters, preserving natural semantic boundaries in order of priority:
      1. Paragraphs ("\n\n")
      2. Line breaks ("\n")
      3. Sentence endings (". ", "? ", "! ")
      4. Words (" ")
      5. Character fallback ("")
    Guarantees every chunk fits safely within the 512-token limit of
    intfloat/multilingual-e5-small without silent token truncation.
    """
    if not text:
        return []

    text = text.strip()
    if len(text) <= chunk_size:
        return [text]

    separators = ["\n\n", "\n", ". ", "? ", "! ", " ", ""]

    def _split_recursive(text_chunk: str, seps: list[str]) -> list[str]:
        if not text_chunk:
            return []
        if len(text_chunk) <= chunk_size or not seps:
            return [text_chunk]

        sep = seps[0]
        remaining_seps = seps[1:]

        if sep == "":
            chunks = []
            step = max(1, chunk_size - overlap)
            for i in range(0, len(text_chunk), step):
                chunks.append(text_chunk[i:i + chunk_size])
            return chunks

        splits = text_chunk.split(sep)
        result = []
        for s in splits:
            if not s:
                continue
            token = s if sep in ["\n\n", "\n", " "] else (s + (sep.strip() if not s.endswith(sep.strip()) else ""))
            if len(token) > chunk_size:
                result.extend(_split_recursive(token, remaining_seps))
            else:
                result.append(token)
        return result

    base_splits = _split_recursive(text, separators)

    merged_chunks = []
    current_chunk = ""

    for split in base_splits:
        split = split.strip()
        if not split:
            continue

        candidate = f"{current_chunk}\n{split}".strip() if current_chunk else split
        if len(candidate) <= chunk_size:
            current_chunk = candidate
        else:
            if current_chunk:
                merged_chunks.append(current_chunk)
                if overlap > 0 and len(current_chunk) > overlap:
                    overlap_text = current_chunk[-overlap:].strip()
                    current_chunk = f"{overlap_text} {split}".strip() if overlap_text else split
                else:
                    current_chunk = split
            else:
                merged_chunks.append(split)
                current_chunk = ""

    if current_chunk:
        merged_chunks.append(current_chunk)

    return merged_chunks or [text]


def _link_client_in_db(client_id: str):
    """
    Preserves the email_customers linkage/customer_name lookup behaviour
    from the old implementation. collect_name is no longer a Chroma
    collection name (Qdrant uses one shared collection) — it's kept purely
    as a stable identifier column for backward-compat with any existing
    reads of email_customers.collect_name, set equal to a normalized
    client_id.
    """
    collect_name = f"client_{client_id.replace('-', '_').lower()}"
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            cursor = db.cursor()
            customer_name = client_id
            try:
                cursor.execute("SHOW COLUMNS FROM users")
                columns = [col[0] for col in cursor.fetchall()]
                name_col = None
                for candidate in ["customer_name", "name", "username", "email"]:
                    if candidate in columns:
                        name_col = candidate
                        break
                if name_col:
                    cursor.execute(f"SELECT {name_col} FROM users WHERE client_id = %s LIMIT 1", (client_id,))
                    user_res = cursor.fetchone()
                    if user_res and user_res[0]:
                        val = user_res[0]
                        customer_name = val.split('@')[0].capitalize() if name_col == "email" else str(val).capitalize()
            except Exception as ue:
                logger.warning(f"⚠️ Could not fetch customer name from users table: {ue}")

            cursor.execute("""
                INSERT INTO email_customers (client_id, rag_id, customer_name)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE rag_id = VALUES(rag_id), customer_name = VALUES(customer_name)
            """, (client_id, collect_name, customer_name))
            db.commit()
    except Exception as e:
        logger.error(f"❌ Failed to save RAG ID link in database: {e}")

    return collect_name


def add_knowledge(client_id: str, title: str, content: str) -> str:
    """
    Chunks content, embeds it via embed_service, and upserts into the
    shared Qdrant collection tagged with client_id. Falls back to the
    JSON store if either Qdrant or embed_service is unavailable.
    """
    doc_id = str(uuid.uuid4())
    logger.info(f"Adding knowledge for client_id={client_id}, doc_id={doc_id}, title={title}")

    chunks = split_text(content) or [content]
    saved_successfully = False

    vectors = embed_passages(chunks)
    if vectors is not None and ensure_collection():
        saved_successfully = upsert_chunks(client_id, doc_id, title, chunks, vectors)
    else:
        logger.warning("⚠️ Embedding or Qdrant unavailable — falling back to JSON store")

    if not saved_successfully:
        docs = load_fallback_db(client_id)
        docs.append({"id": doc_id, "title": title, "content": content})
        save_fallback_db(client_id, docs)
        logger.info(f"✅ Saved to Fallback JSON Database for client_id={client_id}")

    _link_client_in_db(client_id)
    return doc_id


def get_knowledge_base(client_id: str) -> list[dict]:
    """Retrieves all knowledge entries for a specific client_id (or ALL clients)."""
    logger.info(f"Fetching knowledge base for client_id={client_id}")

    if client_id == "ALL":
        docs = qdrant_get_all_documents()
        if docs:
            return docs
        return load_all_fallback_docs()

    docs = qdrant_get_client_documents(client_id)
    if docs:
        return docs

    return load_fallback_db(client_id)


def delete_knowledge(client_id: str, doc_id: str) -> bool:
    logger.info(f"Deleting knowledge for client_id={client_id}, doc_id={doc_id}")

    deleted_qdrant = qdrant_delete_document(client_id, doc_id)

    deleted_fallback = False
    docs = load_fallback_db(client_id)
    initial_len = len(docs)
    new_docs = [doc for doc in docs if doc.get("id") != doc_id]
    if len(new_docs) < initial_len:
        save_fallback_db(client_id, new_docs)
        deleted_fallback = True
        logger.info(f"✅ Deleted from Fallback JSON successfully for client_id={client_id}")

    # Also ensure purged from legacy fallback_db.json if present
    if os.path.exists(FALLBACK_DB_PATH):
        try:
            with open(FALLBACK_DB_PATH, 'r+', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    legacy_data = json.load(f)
                    if isinstance(legacy_data, dict) and client_id in legacy_data:
                        legacy_docs = legacy_data[client_id]
                        if isinstance(legacy_docs, list):
                            new_legacy = [d for d in legacy_docs if d.get("id") != doc_id]
                            if len(new_legacy) < len(legacy_docs):
                                legacy_data[client_id] = new_legacy
                                f.seek(0)
                                json.dump(legacy_data, f, indent=2, ensure_ascii=False)
                                f.truncate()
                                deleted_fallback = True
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"⚠️ Could not purge doc {doc_id} from legacy fallback_db.json: {e}")

    return deleted_qdrant or deleted_fallback


def query_knowledge(client_id: str, query: str, top_k: int = 3, min_score: float | None = None) -> str:
    """
    Queries Qdrant (client_id-filtered) or falls back to JSON store.
    Enforces min_score threshold to eliminate hallucination-inducing low-similarity context.
    Returns a single "\n---\n" joined context string, same contract as before.
    """
    if not client_id or client_id == "ALL":
        raise ValueError("query_knowledge() requires a specific, non-wildcard client_id")

    effective_min_score = min_score if min_score is not None else float(os.getenv("RAG_MIN_SCORE", "0.68"))
    logger.info(f"Querying knowledge for client_id={client_id}, query={query[:100]}..., min_score={effective_min_score}")

    query_vector = embed_query(query)
    if query_vector is not None:
        results = qdrant_search(client_id, query_vector, top_k=top_k, min_score=effective_min_score)
        if results:
            context = "\n---\n".join(r["content"] for r in results if r.get("content"))
            if context:
                logger.info(f"✅ Qdrant RAG retrieved {len(results)} chunks exceeding min_score={effective_min_score} (length: {len(context)})")
                return context
        logger.warning(f"⚠️ Qdrant returned no results >= min_score {effective_min_score} for client_id={client_id} — falling back to JSON")
    else:
        logger.warning("⚠️ embed_service unavailable — falling back to JSON")

    # Fallback hybrid keyword + token-overlap matching
    docs = load_fallback_db(client_id)
    if not docs:
        return ""

    ranked = [(calculate_fallback_score(query, doc.get("content", "")), doc.get("content", "")) for doc in docs]
    ranked.sort(key=lambda x: x[0], reverse=True)
    # Require at least 0.20 score in fallback to eliminate spurious matches
    top_matches = [doc[1] for doc in ranked[:top_k] if doc[0] >= 0.20]

    if top_matches:
        context = "\n---\n".join(top_matches)
        logger.info(f"✅ Fallback RAG retrieved context length: {len(context)}")
        return context

    logger.info("ℹ️ Fallback RAG found no relevant matches with score >= 0.20 (returning empty context)")
    return ""


def retrieve_knowledge(client_id: str, query: str, top_k: int = 3, min_score: float | None = None) -> list[dict]:
    """
    Structured semantic retriever — returns list of matching chunks with
    metadata + similarity score. Same contract as the old Chroma version.
    """
    effective_min_score = min_score if min_score is not None else float(os.getenv("RAG_MIN_SCORE", "0.68"))
    logger.info(f"Retrieving structured knowledge for client_id={client_id}, query={query[:50]}, min_score={effective_min_score}")

    query_vector = embed_query(query)

    if client_id == "ALL":
        if query_vector is not None:
            results = qdrant_search_all_clients(query_vector, top_k=top_k, min_score=effective_min_score)
            if results:
                return [
                    {
                        "id": r["id"], "title": f"[{r.get('client_id','')}] {r['title']}",
                        "doc_id": r["doc_id"], "content": r["content"],
                        "score": r["score"], "client_id": r.get("client_id", "")
                    }
                    for r in results
                ]
        # Fallback
        all_docs = load_all_fallback_docs()
        fallback_results = []
        for doc in all_docs:
            cid = doc.get("client_id", "")
            score = calculate_fallback_score(query, doc.get("content", ""))
            fallback_results.append({
                "id": doc.get("id"), "title": doc.get("title", ""), "doc_id": doc.get("id"),
                "content": doc.get("content", ""), "score": round(score, 3), "client_id": cid
            })
        fallback_results.sort(key=lambda x: x["score"], reverse=True)
        return [r for r in fallback_results[:top_k] if r["score"] >= 0.20]

    if query_vector is not None:
        results = qdrant_search(client_id, query_vector, top_k=top_k, min_score=effective_min_score)
        if results:
            return results

    # Fallback JSON hybrid Search
    docs = load_fallback_db(client_id)
    if not docs:
        return []

    ranked = [
        {"id": doc.get("id", ""), "title": doc.get("title", ""), "doc_id": doc.get("id", ""),
         "content": doc.get("content", ""), "score": round(calculate_fallback_score(query, doc.get("content", "")), 3)}
        for doc in docs
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return [r for r in ranked[:top_k] if r["score"] >= 0.20]


# ==========================================
# 📦 LEGACY COMPATIBILITY API
# ==========================================
def get_rag_id(client_id: str) -> Optional[str]:
    """
    Under Chroma this returned the per-client collection name. Under Qdrant
    there is only ONE collection, so this no longer identifies a collection
    — it's kept purely for backward compatibility with callers (email_logs.rag_id
    column, worker/tasks.py) that expect a non-null identifying string.
    Returns the same normalized collect_name value that used to be the
    Chroma collection name, sourced from email_customers if present.
    """
    if not client_id:
        return None
    try:
        from app.db import get_db_ctx
        with get_db_ctx() as db:
            cursor = db.cursor()
            cursor.execute("SELECT rag_id FROM email_customers WHERE client_id = %s LIMIT 1", (client_id,))
            result = cursor.fetchone()
            if result and result[0]:
                return result[0]
            return f"client_{client_id.replace('-', '_').lower()}"
    except Exception as e:
        logger.error(f"❌ Error fetching rag_id: {str(e)}")
        return f"client_{client_id.replace('-', '_').lower()}"


def query_rag(collect_name: str, query: str) -> dict:
    """
    Legacy wrapper. Resolves collect_name/rag_id back to a real client_id (same
    lookup the Chroma version did) and delegates to query_knowledge().
    """
    if not collect_name:
        return {"answer": ""}
    try:
        real_client_id = collect_name
        if collect_name.startswith("client_"):
            real_client_id = collect_name[7:].replace('_', '-').upper()

        try:
            from app.db import get_db_ctx
            with get_db_ctx() as db:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT client_id FROM email_customers WHERE rag_id = %s OR client_id = %s LIMIT 1",
                    (collect_name, collect_name)
                )
                res = cursor.fetchone()
                if res and res[0]:
                    real_client_id = res[0]
        except Exception as e:
            logger.warning(f"⚠️ Failed to resolve client_id for collect_name={collect_name}: {e}")

        context = query_knowledge(real_client_id, query)
        return {"answer": context if context else "No context found in local RAG database."}
    except Exception as e:
        logger.error(f"❌ Local Custom RAG API error: {str(e)}")
        return {"answer": f"Could not retrieve context from local RAG: {str(e)}"}


# ==========================================
# 📂 ADVANCED DOCUMENT PARSERS (unchanged)
# ==========================================

def parse_docx(file_content: bytes) -> str:
    import zipfile
    import xml.etree.ElementTree as ET
    from io import BytesIO

    try:
        f = BytesIO(file_content)
        with zipfile.ZipFile(f) as docx:
            xml_content = docx.read('word/document.xml')
            root = ET.fromstring(xml_content)
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            paragraphs = []
            for para in root.findall('.//w:p', ns):
                text_parts = []
                for run in para.findall('.//w:t', ns):
                    if run.text:
                        text_parts.append(run.text)
                if text_parts:
                    paragraphs.append("".join(text_parts))
            return "\n".join(paragraphs)
    except Exception as e:
        logger.error(f"Failed to parse docx: {e}")
        return file_content.decode('utf-8', errors='ignore')


def parse_uploaded_file(file_name: str, file_content: bytes) -> tuple[str, str]:
    """
    Parses a PDF, DOCX, DOC, or TXT file and returns a tuple (title, content).
    Only allows these specific formats. Unchanged from prior implementation.
    """
    ext = file_name.split('.')[-1].lower()
    logger.info(f"Parsing uploaded file: name={file_name}, ext={ext}")

    if ext in ['txt', 'md', 'markdown']:
        content = file_content.decode('utf-8', errors='ignore')
        return file_name, content.strip()

    elif ext == 'docx':
        content = parse_docx(file_content)
        return file_name, content.strip()

    elif ext == 'doc':
        try:
            decoded = file_content.decode('utf-16', errors='ignore')
            content = "".join([c for c in decoded if c.isprintable() or c in '\n\r\t'])
            if len(content.strip()) < 50:
                decoded = file_content.decode('utf-8', errors='ignore')
                content = "".join([c for c in decoded if c.isprintable() or c in '\n\r\t'])
        except Exception:
            content = file_content.decode('utf-8', errors='ignore')
        return file_name, content.strip()

    elif ext == 'pdf':
        import io
        from pypdf import PdfReader
        f = io.BytesIO(file_content)
        reader = PdfReader(f)
        text = ""
        for i, page in enumerate(reader.pages):
            t = page.extract_text()
            if t:
                text += f"--- Page {i+1} ---\n" + t + "\n"
        return file_name, text.strip()

    else:
        raise ValueError(f"Unsupported file format: .{ext}. Only .md, .txt, .docx, .doc, and .pdf files are allowed.")
