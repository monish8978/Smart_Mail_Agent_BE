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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
# 🛠️ FALLBACK JSON DATABASE (unchanged philosophy, now backs Qdrant-down)
# ==========================================
def load_fallback_db() -> dict:
    if os.path.exists(FALLBACK_DB_PATH):
        try:
            with open(FALLBACK_DB_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ Failed to load fallback DB: {e}")
            return {}
    return {}


def save_fallback_db(data: dict):
    try:
        with open(FALLBACK_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Failed to save fallback DB: {e}")


def jaccard_similarity(text1: str, text2: str) -> float:
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    return len(words1.intersection(words2)) / len(words1.union(words2))


# ==========================================
# 🚀 CORE RAG API INTERFACES (USER-ISOLATED)
# ==========================================

def split_text(text: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Unchanged from the Chroma implementation — chunking strategy is not part of this migration."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += chunk_size - overlap
    return chunks


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
                INSERT INTO email_customers (client_id, collect_name, customer_name)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE collect_name = VALUES(collect_name), customer_name = VALUES(customer_name)
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
        db = load_fallback_db()
        if client_id not in db:
            db[client_id] = []
        db[client_id].append({"id": doc_id, "title": title, "content": content})
        save_fallback_db(db)
        logger.info("✅ Saved to Fallback JSON Database")

    _link_client_in_db(client_id)
    return doc_id


def get_knowledge_base(client_id: str) -> list[dict]:
    """Retrieves all knowledge entries for a specific client_id (or ALL clients)."""
    logger.info(f"Fetching knowledge base for client_id={client_id}")

    if client_id == "ALL":
        docs = qdrant_get_all_documents()
        if docs:
            return docs
        # Fallback JSON
        db = load_fallback_db()
        fallback_docs = []
        for cid, cdocs in db.items():
            for doc in cdocs:
                fallback_docs.append({
                    "id": doc["id"], "title": f"[{cid}] {doc['title']}",
                    "content": doc["content"], "client_id": cid
                })
        return fallback_docs

    docs = qdrant_get_client_documents(client_id)
    if docs:
        return docs

    db = load_fallback_db()
    return db.get(client_id, [])


def delete_knowledge(client_id: str, doc_id: str) -> bool:
    logger.info(f"Deleting knowledge for client_id={client_id}, doc_id={doc_id}")

    deleted = qdrant_delete_document(client_id, doc_id)
    if deleted:
        return True

    db = load_fallback_db()
    if client_id in db:
        initial_len = len(db[client_id])
        db[client_id] = [doc for doc in db[client_id] if doc["id"] != doc_id]
        save_fallback_db(db)
        logger.info("✅ Deleted from Fallback JSON successfully")
        return len(db[client_id]) < initial_len
    return False


def query_knowledge(client_id: str, query: str, top_k: int = 3) -> str:
    """
    Queries Qdrant (client_id-filtered) or falls back to Jaccard similarity
    against the JSON store. Returns a single "\\n---\\n" joined context string,
    same contract as the old Chroma implementation.
    """
    logger.info(f"Querying knowledge for client_id={client_id}, query={query[:100]}...")

    query_vector = embed_query(query)
    if query_vector is not None:
        results = qdrant_search(client_id, query_vector, top_k=top_k)
        if results:
            context = "\n---\n".join(r["content"] for r in results if r.get("content"))
            if context:
                logger.info(f"✅ Qdrant RAG retrieved context length: {len(context)}")
                return context
        logger.warning(f"⚠️ Qdrant returned no results for client_id={client_id} — falling back to JSON")
    else:
        logger.warning("⚠️ embed_service unavailable — falling back to JSON")

    # Fallback Jaccard / Keyword matching
    db = load_fallback_db()
    docs = db.get(client_id, [])
    if not docs:
        return ""

    ranked = [(jaccard_similarity(query, doc["content"]), doc["content"]) for doc in docs]
    ranked.sort(key=lambda x: x[0], reverse=True)
    top_matches = [doc[1] for doc in ranked[:top_k] if doc[0] > 0.0]

    if top_matches:
        context = "\n---\n".join(top_matches)
        logger.info(f"✅ Fallback RAG retrieved context length: {len(context)}")
        return context

    context = "\n---\n".join([doc["content"] for doc in docs[:2]])
    logger.info(f"✅ Fallback Default RAG context length: {len(context)}")
    return context


def retrieve_knowledge(client_id: str, query: str, top_k: int = 3) -> list[dict]:
    """
    Structured semantic retriever — returns list of matching chunks with
    metadata + similarity score. Same contract as the old Chroma version.
    """
    logger.info(f"Retrieving structured knowledge for client_id={client_id}, query={query[:50]}")

    query_vector = embed_query(query)

    if client_id == "ALL":
        if query_vector is not None:
            results = qdrant_search_all_clients(query_vector, top_k=top_k)
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
        db = load_fallback_db()
        fallback_results = []
        for cid, docs in db.items():
            for doc in docs:
                score = jaccard_similarity(query, doc["content"])
                fallback_results.append({
                    "id": doc["id"], "title": f"[{cid}] {doc['title']}", "doc_id": doc["id"],
                    "content": doc["content"], "score": round(score, 3), "client_id": cid
                })
        fallback_results.sort(key=lambda x: x["score"], reverse=True)
        return [r for r in fallback_results[:top_k] if r["score"] > 0.0]

    if query_vector is not None:
        results = qdrant_search(client_id, query_vector, top_k=top_k)
        if results:
            return results

    # Fallback JSON Jaccard Search
    db = load_fallback_db()
    docs = db.get(client_id, [])
    if not docs:
        return []

    ranked = [
        {"id": doc["id"], "title": doc["title"], "doc_id": doc["id"],
         "content": doc["content"], "score": round(jaccard_similarity(query, doc["content"]), 3)}
        for doc in docs
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return [r for r in ranked[:top_k] if r["score"] > 0.0]


# ==========================================
# 📦 LEGACY COMPATIBILITY API
# ==========================================
def get_rag_id(client_id: str) -> str:
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
            cursor.execute("SELECT collect_name FROM email_customers WHERE client_id = %s LIMIT 1", (client_id,))
            result = cursor.fetchone()
            if result and result[0]:
                return result[0]
            return f"client_{client_id.replace('-', '_').lower()}"
    except Exception as e:
        logger.error(f"❌ Error fetching collect_name: {str(e)}")
        return f"client_{client_id.replace('-', '_').lower()}"


def query_rag(collect_name: str, query: str) -> dict:
    """
    Legacy wrapper. Resolves collect_name back to a real client_id (same
    lookup the Chroma version did) and delegates to query_knowledge().
    """
    if not collect_name:
        return {"answer": ""}
    try:
        real_client_id = collect_name
        try:
            from app.db import get_db_ctx
            with get_db_ctx() as db:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT client_id FROM email_customers WHERE collect_name = %s OR client_id = %s LIMIT 1",
                    (collect_name, collect_name)
                )
                res = cursor.fetchone()
                if res:
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

    if ext == 'txt':
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
        raise ValueError(f"Unsupported file format: .{ext}. Only .pdf, .doc, .docx, and .txt files are allowed.")
