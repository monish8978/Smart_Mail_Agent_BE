import logging
from typing import Optional
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
    inactivity_timeout_hours: Optional[int] = 72


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
                COALESCE(feature_strip_disclaimers, 1) AS feature_strip_disclaimers,
                COALESCE(inactivity_timeout_hours, 72) AS inactivity_timeout_hours
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
                "feature_strip_disclaimers": True,
                "inactivity_timeout_hours": 72
            }
        return {
            "feature_ticket_creation": bool(row["feature_ticket_creation"]),
            "feature_auto_send": bool(row["feature_auto_send"]),
            "feature_rag": bool(row["feature_rag"]),
            "feature_order_tracking": bool(row["feature_order_tracking"]),
            "feature_manual_reply": bool(row["feature_manual_reply"]),
            "feature_strip_disclaimers": bool(row["feature_strip_disclaimers"]),
            "inactivity_timeout_hours": int(row.get("inactivity_timeout_hours") or 72)
        }


@router.post("/admin/client-features")
def set_client_features(data: ClientFeaturesRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            timeout_h = max(1, min(720, data.inactivity_timeout_hours or 72))
            cursor.execute("""
                UPDATE email_accounts SET feature_ticket_creation=%s, feature_auto_send=%s,
                feature_rag=%s, feature_order_tracking=%s, feature_manual_reply=%s,
                feature_strip_disclaimers=%s, inactivity_timeout_hours=%s
                WHERE client_id=%s
            """, (data.feature_ticket_creation, data.feature_auto_send, data.feature_rag,
                  data.feature_order_tracking, data.feature_manual_reply,
                  data.feature_strip_disclaimers, timeout_h, data.client_id))
            db.commit()
    return {"status": "success", "inactivity_timeout_hours": timeout_h}


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


# ==============================
# 🚨 Proactive Webhook Alerts
# ==============================

class AlertSettingsRequest(BaseModel):
    client_id: str
    webhook_url: Optional[str] = None
    alert_email: Optional[str] = None
    alert_on_sla_breach: bool = True
    alert_on_worker_stall: bool = True
    alert_sla_hours: int = 4


@router.get("/admin/alert-settings/{client_id}")
def get_alert_settings_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    from app.alert_service import get_alert_settings
    return get_alert_settings(client_id)


@router.put("/admin/alert-settings")
def update_alert_settings_endpoint(data: AlertSettingsRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.alert_service import update_alert_settings
    update_alert_settings(data.client_id, data.model_dump())
    return {"status": "success", "message": "Alert and webhook settings updated"}


@router.post("/admin/alert-settings/test")
def test_alert_webhook_endpoint(data: AlertSettingsRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    from app.alert_service import dispatch_webhook
    if not data.webhook_url:
        raise HTTPException(status_code=400, detail="Webhook URL is required to send a test alert")
    ok = dispatch_webhook(data.webhook_url, {
        "event_type": "test_ping",
        "client_id": data.client_id,
        "title": "Test Sentinel Ping",
        "description": "Your Mail AI automated alert webhook is active and verified.",
        "severity": "INFO",
        "details": {
            "Triggered By": user.get("username", "Operator"),
            "Client ID": data.client_id,
            "SLA Threshold": f"{data.alert_sla_hours} hours",
            "SLA Alerts": "Enabled" if data.alert_on_sla_breach else "Disabled",
            "Worker Alerts": "Enabled" if data.alert_on_worker_stall else "Disabled"
        }
    })
    if not ok:
        raise HTTPException(status_code=400, detail="Webhook delivery failed. Verify URL endpoint connectivity.")
    return {"status": "success", "message": "Test alert successfully sent to webhook endpoint"}

