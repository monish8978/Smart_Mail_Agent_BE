import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import pymysql

from app.auth_deps import get_current_user, require_admin, require_client_access
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


# ==============================
# 🎛️ Client Features
# ==============================

class ClientFeaturesRequest(BaseModel):
    client_id: str
    feature_ticket_creation: bool
    feature_auto_send: bool
    feature_rag: bool
    feature_order_tracking: bool
    feature_manual_reply: bool
    feature_strip_disclaimers: bool = True


@router.get("/admin/client-features/{client_id}")
def get_client_features_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT 
                COALESCE(feature_ticket_creation, 1) AS feature_ticket_creation,
                COALESCE(feature_auto_send, 1) AS feature_auto_send,
                COALESCE(feature_rag, 1) AS feature_rag,
                COALESCE(feature_order_tracking, 1) AS feature_order_tracking,
                COALESCE(feature_manual_reply, 1) AS feature_manual_reply,
                COALESCE(feature_strip_disclaimers, 1) AS feature_strip_disclaimers
            FROM email_accounts WHERE client_id=%s LIMIT 1
        """, (client_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "feature_ticket_creation": True,
                "feature_auto_send": True,
                "feature_rag": True,
                "feature_order_tracking": True,
                "feature_manual_reply": True,
                "feature_strip_disclaimers": True
            }
        return {
            "feature_ticket_creation": bool(row["feature_ticket_creation"]),
            "feature_auto_send": bool(row["feature_auto_send"]),
            "feature_rag": bool(row["feature_rag"]),
            "feature_order_tracking": bool(row["feature_order_tracking"]),
            "feature_manual_reply": bool(row["feature_manual_reply"]),
            "feature_strip_disclaimers": bool(row["feature_strip_disclaimers"])
        }


@router.post("/admin/client-features")
def set_client_features(data: ClientFeaturesRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE email_accounts SET feature_ticket_creation=%s, feature_auto_send=%s,
                feature_rag=%s, feature_order_tracking=%s, feature_manual_reply=%s,
                feature_strip_disclaimers=%s
                WHERE client_id=%s
            """, (data.feature_ticket_creation, data.feature_auto_send, data.feature_rag,
                  data.feature_order_tracking, data.feature_manual_reply,
                  data.feature_strip_disclaimers, data.client_id))
            db.commit()
    return {"status": "success"}


# ==============================
# 🛑 Master Automation Flow Control & Admin Kill Switch
# ==============================

class AdminMasterBotToggleRequest(BaseModel):
    client_id: str
    admin_bot_enabled: bool


class ClientMasterBotToggleRequest(BaseModel):
    client_id: str
    client_bot_enabled: bool


@router.get("/master-bot-status/{client_id}")
def get_master_bot_status(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    COALESCE(admin_bot_enabled, 1) AS admin_bot_enabled,
                    COALESCE(client_bot_enabled, 1) AS client_bot_enabled
                FROM email_accounts WHERE client_id=%s LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
            if not row:
                return {
                    "client_id": client_id,
                    "admin_bot_enabled": True,
                    "client_bot_enabled": True,
                    "is_effective_enabled": True,
                    "is_locked_by_admin": False
                }
            admin_enabled = bool(row[0])
            client_enabled = bool(row[1])
            return {
                "client_id": client_id,
                "admin_bot_enabled": admin_enabled,
                "client_bot_enabled": client_enabled,
                "is_effective_enabled": admin_enabled and client_enabled,
                "is_locked_by_admin": not admin_enabled
            }


@router.post("/admin/master-bot-toggle")
def admin_master_bot_toggle(data: AdminMasterBotToggleRequest, user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE email_accounts SET admin_bot_enabled=%s WHERE client_id=%s",
                (data.admin_bot_enabled, data.client_id)
            )
            db.commit()
    return {
        "status": "success",
        "admin_bot_enabled": data.admin_bot_enabled,
        "message": f"Admin Master Switch set to {data.admin_bot_enabled} for client {data.client_id}"
    }


@router.post("/client/master-bot-toggle")
def client_master_bot_toggle(data: ClientMasterBotToggleRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT COALESCE(admin_bot_enabled, 1) FROM email_accounts WHERE client_id=%s", (data.client_id,))
            row = cursor.fetchone()
            admin_enabled = bool(row[0]) if row else True

            if not admin_enabled and data.client_bot_enabled:
                raise HTTPException(
                    status_code=403,
                    detail="Master Bot has been disabled by the Administrator. You cannot turn it on until an administrator re-enables it for your account."
                )

            cursor.execute(
                "UPDATE email_accounts SET client_bot_enabled=%s WHERE client_id=%s",
                (data.client_bot_enabled, data.client_id)
            )
            db.commit()
    return {
        "status": "success",
        "client_bot_enabled": data.client_bot_enabled,
        "message": f"Client Master Switch set to {data.client_bot_enabled}"
    }
