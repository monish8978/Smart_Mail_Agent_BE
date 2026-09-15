import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, field_validator
import pymysql

from app.auth_deps import get_current_user, require_admin, require_client_access
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


# ==============================
# 👥 Client & User Management
# ==============================

class CreateClientRequest(BaseModel):
    name: str
    phone_number: str
    login_email: EmailStr
    login_password: str
    imap_email: str = ""
    imap_password: str = ""
    score_threshold: int = 80
    response_tone: str = "Formal"
    agent_type: str = "customer_support_agent"
    department_name: Optional[str] = None
    company_name: Optional[str] = None

    @field_validator("login_password")
    def login_pw_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("imap_password")
    def imap_pw_strength(cls, v):
        if v and len(v) < 8:
            raise ValueError("IMAP password must be at least 8 characters")
        return v


@router.post("/admin/create-client")
def create_client(data: CreateClientRequest, user: dict = Depends(require_admin())):
    from app.auth import create_client_atomic
    res = create_client_atomic(
        data.name, data.phone_number, data.login_email, data.login_password, data.imap_email, data.imap_password,
        data.score_threshold, data.response_tone,
        data.agent_type, data.department_name, data.company_name
    )
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.get("/admin/pending-users")
def list_pending_users(user: dict = Depends(require_admin())):
    from app.auth import get_pending_users
    rows = get_pending_users()
    return [
        {"id": r["id"], "client_id": r["client_id"], "email": r["email"], "role": r["role"],
         "created_at": r["created_at"].strftime("%b %d, %Y %H:%M") if r["created_at"] else ""}
        for r in rows
    ]


@router.get("/admin/users")
def admin_get_all_users(user: dict = Depends(require_admin())):
    from app.auth import get_all_users
    users = get_all_users()
    return {"success": True, "users": users}


class ClientProfileRequest(BaseModel):
    client_id: str
    name: Optional[str] = None
    phone_number: Optional[str] = None
    login_email: Optional[str] = None
    imap_email: Optional[str] = None
    imap_password: Optional[str] = None
    agent_type: Optional[str] = None
    department_name: Optional[str] = None
    company_name: Optional[str] = None


@router.post("/admin/client-profile")
def set_client_profile(data: ClientProfileRequest, user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE users 
                SET name=%s, phone_number=%s, email=%s
                WHERE client_id=%s
            """, (data.name or "", data.phone_number or "", data.login_email or "", data.client_id))
            
            cursor.execute("""
                UPDATE email_accounts 
                SET agent_type=%s, department_name=%s, company_name=%s, email=%s, password=%s
                WHERE client_id=%s
            """, (data.agent_type or "", data.department_name or "", data.company_name or "", data.imap_email or "", data.imap_password or "", data.client_id))
            
            db.commit()
    return {"success": True}


class SelfProfileRequest(BaseModel):
    client_id: str
    department_name: Optional[str] = None
    company_name: Optional[str] = None
    score_threshold: Optional[int] = None
    agent_type: Optional[str] = None
    response_tone: Optional[str] = None


@router.post("/client/profile")
def update_self_profile(data: SelfProfileRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            updates = []
            params = []
            if data.department_name is not None:
                updates.append("department_name = %s")
                params.append(data.department_name)
            if data.company_name is not None:
                updates.append("company_name = %s")
                params.append(data.company_name)
            if data.score_threshold is not None:
                updates.append("score_threshold = %s")
                params.append(data.score_threshold)
            if data.agent_type is not None:
                updates.append("agent_type = %s")
                params.append(data.agent_type)
            if data.response_tone is not None:
                updates.append("response_tone = %s")
                params.append(data.response_tone)
            
            if updates:
                if data.client_id == "ALL" and user.get("role") == "admin":
                    query = f"UPDATE email_accounts SET {', '.join(updates)}"
                    cursor.execute(query, tuple(params))
                else:
                    params.append(data.client_id)
                    query = f"UPDATE email_accounts SET {', '.join(updates)} WHERE client_id = %s"
                    cursor.execute(query, tuple(params))
                db.commit()
    return {"success": True}


@router.delete("/admin/delete-client/{client_id}")
def delete_client(client_id: str, user: dict = Depends(require_admin())):
    from app.auth import delete_client_account
    res = delete_client_account(client_id)
    if not res["success"]:
        raise HTTPException(status_code=404, detail=res["error"])
    return res
