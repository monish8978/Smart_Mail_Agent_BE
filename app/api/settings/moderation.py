import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.auth_deps import get_current_user, require_client_access
from app.db import get_db_ctx
from app.email_disclaimers import (
    get_client_disclaimers,
    add_client_disclaimer,
    delete_client_disclaimer,
    toggle_client_disclaimer,
)
from app.keyword_filter import _ensure_table, get_blocked_keywords, _ensure_reply_blocked_table

logger = logging.getLogger(__name__)

router = APIRouter()


# ==============================
# 🚫 Blocked Keywords & Policy
# ==============================

class BlockedKeywordRequest(BaseModel):
    client_id: str
    keyword: str


@router.post("/blocked-keywords/add")
def add_blocked_keyword(data: BlockedKeywordRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            _ensure_table(cursor)
            cursor.execute(
                "INSERT IGNORE INTO blocked_keywords (client_id, keyword) VALUES (%s, %s)",
                (data.client_id, data.keyword.strip())
            )
            db.commit()
    return {"status": "success"}


@router.delete("/blocked-keywords/{client_id}/{keyword}")
def remove_blocked_keyword(client_id: str, keyword: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            _ensure_table(cursor)
            cursor.execute("DELETE FROM blocked_keywords WHERE client_id=%s AND keyword=%s", (client_id, keyword))
            db.commit()
    return {"status": "success"}


@router.get("/blocked-keywords/{client_id}")
def list_blocked_keywords(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            return {"keywords": get_blocked_keywords(cursor, client_id)}


# ==============================
# ⚖️ Email Disclaimers Management
# ==============================

class EmailDisclaimerCreateRequest(BaseModel):
    client_id: str
    disclaimer_text: str


class EmailDisclaimerToggleRequest(BaseModel):
    is_active: bool


@router.get("/email-disclaimers/{client_id}")
def get_email_disclaimers_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    return get_client_disclaimers(client_id)


@router.post("/email-disclaimers")
def create_email_disclaimer_endpoint(data: EmailDisclaimerCreateRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    new_id = add_client_disclaimer(data.client_id, data.disclaimer_text)
    return {"status": "success", "id": new_id}


@router.delete("/email-disclaimers/{disclaimer_id}")
def delete_email_disclaimer_endpoint(disclaimer_id: int, client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    if client_id:
        require_client_access(client_id, user)
    deleted = delete_client_disclaimer(disclaimer_id, client_id)
    return {"status": "success", "deleted": deleted}


@router.patch("/email-disclaimers/{disclaimer_id}/toggle")
def toggle_email_disclaimer_endpoint(disclaimer_id: int, data: EmailDisclaimerToggleRequest, client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    if client_id:
        require_client_access(client_id, user)
    toggled = toggle_client_disclaimer(disclaimer_id, data.is_active, client_id)
    return {"status": "success", "toggled": toggled}


# ==============================
# 🛑 Blocked Emails
# ==============================

class UpdateBlockedEmailStatusRequest(BaseModel):
    status: str  # 'ignored' or 'replied'


@router.get("/blocked-emails/{client_id}")
def list_blocked_emails(
    client_id: str,
    status: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            _ensure_reply_blocked_table(cursor)
            if status:
                cursor.execute("""
                    SELECT id, from_email, subject, body, matched_keyword, status, created_at
                    FROM reply_blocked_by_keyword
                    WHERE client_id = %s AND status = %s
                    ORDER BY created_at DESC
                """, (client_id, status))
            else:
                cursor.execute("""
                    SELECT id, from_email, subject, body, matched_keyword, status, created_at
                    FROM reply_blocked_by_keyword
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()
    return [
        {
            "id": r[0],
            "from_email": r[1],
            "subject": r[2],
            "body": r[3],
            "matched_keyword": r[4],
            "status": r[5],
            "created_at": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else ""
        }
        for r in rows
    ]


@router.patch("/blocked-emails/{client_id}/{record_id}")
def update_blocked_email_status(
    client_id: str,
    record_id: int,
    data: UpdateBlockedEmailStatusRequest,
    user: dict = Depends(get_current_user)
):
    require_client_access(client_id, user)
    if data.status not in ("ignored", "replied"):
        raise HTTPException(status_code=400, detail="status must be 'ignored' or 'replied'")
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE reply_blocked_by_keyword
                SET status = %s
                WHERE id = %s AND client_id = %s
            """, (data.status, record_id, client_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Record not found")
            db.commit()
    return {"status": "success"}


@router.patch("/blocked-emails/{client_id}/bulk-ignore")
def bulk_ignore_blocked_emails(
    client_id: str,
    user: dict = Depends(get_current_user)
):
    """Marks all pending_review rows as ignored for this client."""
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE reply_blocked_by_keyword
                SET status = 'ignored'
                WHERE client_id = %s AND status = 'pending_review'
            """, (client_id,))
            affected = cursor.rowcount
            db.commit()
    return {"status": "success", "rows_updated": affected}
