import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from app.auth_deps import get_current_user, require_admin, require_client_access
from app.rate_limiter import RedisRateLimiter
from app.rag import (
    add_knowledge,
    get_knowledge_base,
    delete_knowledge,
    query_knowledge,
    retrieve_knowledge,
    parse_uploaded_file,
)
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge Base (RAG)"])


class RagUploadRequest(BaseModel):
    client_id: str
    title: str
    content: str


class RagQueryRequest(BaseModel):
    client_id: str
    query: str


class RagRetrieveRequest(BaseModel):
    client_id: str
    query: str
    top_k: int = 3


@router.post("/rag/upload", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
@router.post("/upload-rag", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def upload_rag_data_endpoint(data: RagUploadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        doc_id = add_knowledge(data.client_id, data.title, data.content)
        return {"status": "success", "doc_id": doc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rag/documents/{client_id}")
@router.get("/rag-documents/{client_id}")
def get_rag_documents_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        docs = get_knowledge_base(client_id)
        return docs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/rag/documents/{client_id}/{doc_id}")
@router.delete("/rag-documents/{client_id}/{doc_id}")
def delete_rag_document_endpoint(client_id: str, doc_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        success = delete_knowledge(client_id, doc_id)
        if not success:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/query", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
@router.post("/query-rag", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
def query_rag_endpoint(data: RagQueryRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        context = query_knowledge(data.client_id, data.query)
        return {"context": context}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/retrieve", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
@router.post("/retrieve-rag", dependencies=[Depends(RedisRateLimiter(limit=20, window=60))])
def retrieve_rag_endpoint(data: RagRetrieveRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        results = retrieve_knowledge(data.client_id, data.query, data.top_k)
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/knowledge-stats")
def get_admin_knowledge_stats(user: dict = Depends(require_admin())):
    try:
        with get_db_ctx() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT ea.client_id, COALESCE(u.email, ea.email) AS email 
                FROM email_accounts ea 
                LEFT JOIN users u ON ea.client_id = u.client_id
            """)
            clients = cursor.fetchall()

        stats = []
        for cid, email in clients:
            try:
                docs = get_knowledge_base(cid)
                doc_count = len(docs)
            except Exception:
                doc_count = 0
            stats.append({
                "client_id": cid,
                "email": email,
                "documents_count": doc_count
            })
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/upload-file", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
@router.post("/upload-rag-file", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
async def upload_rag_file_endpoint(
    client_id: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    try:
        ext = file.filename.split('.')[-1].lower() if file.filename else ''
        if ext not in ['md', 'markdown', 'txt', 'docx', 'doc', 'pdf']:
            raise HTTPException(status_code=400, detail="Unsupported file format. Only .md, .txt, .docx, .doc, and .pdf files are allowed.")
        file_bytes = await file.read()
        title, content = parse_uploaded_file(file.filename, file_bytes)
        if not content.strip():
            raise HTTPException(status_code=400, detail="The uploaded file does not contain any readable text content.")
        doc_id = add_knowledge(client_id, title, content)
        return {"status": "success", "doc_id": doc_id, "title": title}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

