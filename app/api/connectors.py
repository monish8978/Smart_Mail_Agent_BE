import logging
import json
import time
from typing import Optional, List, Dict, Any
from enum import Enum

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from app.auth_deps import get_current_user, require_admin, require_client_access
from app.rate_limiter import RedisRateLimiter
from app.db import get_db_ctx
from app.email_credential import (
    get_email_account,
    create_email_record_db,
    insert_create_payload_ticket,
    insert_payload_get_ticket,
    get_create_payload_table,
    get_payload_get_ticket_table,
    get_all_create_payloads,
    get_all_get_payloads,
    get_connector_cap,
)
from app.order_routes import get_order_by_id
from app.url_allowlist import add_url_to_allowlist
from app.secrets_crypto import encrypt_secret, decrypt_secret
from app.connector_config import (
    insert_connector_config_checked,
    approve_connector_config,
    reject_connector_config,
    delete_draft_connector_config,
    delete_pending_connector_config,
    disable_connector_config,
    request_delete_disabled_connector,
    approve_delete_connector,
    reject_delete_connector,
    cancel_delete_request,
    generate_connector_template,
    CapExceededError,
    CreationRaceError,
    AllowlistViolationError,
    SwapRaceError,
    TemplateValidationError,
    ResponseMappingValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Connectors & Integrations"])


# ─────────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────────

class EmailStatus(str, Enum):
    done_replied = "Done_Replied"
    ticket_generated = "Ticket_Generated"


class EmailRecordRequest(BaseModel):
    client_id: str
    mail_id: str
    subject: str
    body: str
    status: EmailStatus

    model_config = {"use_enum_values": True}


class OrderStatusRequest(BaseModel):
    client_id: str
    order_id: str


class PaymentStatusRequest(BaseModel):
    client_id: str
    payment_id: str


class TicketStatusRequest(BaseModel):
    client_id: str
    ticket_id: str


class PayloadRequest(BaseModel):
    client_id: str
    url: str
    paylod: Dict[str, Any]


class UrlAllowlistRequest(BaseModel):
    url: str


class ConnectorConfigCreateRequest(BaseModel):
    client_id: str
    trigger_type: str
    http_method: str
    url: str
    headers_template: Optional[str] = None
    request_template: Optional[str] = None
    response_mapping: Optional[str] = None
    auth_type: str
    auth_secret: Optional[str] = None
    auth_field_name: Optional[str] = None
    payload_encoding: str = "plain"
    base64_query_param_name: Optional[str] = None
    status: str = "pending_approval"


class ConnectorConfigEditRequest(BaseModel):
    client_id: str
    trigger_type: str
    http_method: str
    url: str
    headers_template: Optional[str] = None
    request_template: Optional[str] = None
    response_mapping: Optional[str] = None
    auth_type: str
    auth_secret: Optional[str] = None
    auth_field_name: Optional[str] = None
    payload_encoding: str = "plain"
    base64_query_param_name: Optional[str] = None
    status: str = "pending_approval"


class ConnectorConfigApproveRequest(BaseModel):
    client_id: str


class ConnectorConfigRejectRequest(BaseModel):
    client_id: str
    reason: Optional[str] = None


class OAuthTestRequest(BaseModel):
    token_url: str
    client_id: str
    client_secret: str
    refresh_token: Optional[str] = None
    grant_type: Optional[str] = None
    scope: Optional[str] = None
    token_auth_method: str = "client_secret_post"
    header_prefix: Optional[str] = "Bearer"


class GenerateTemplatePreviewRequest(BaseModel):
    client_id: str
    trigger_type: str
    crm_schema_description: str
    sample_response: str = ""


# ─────────────────────────────────────────────────────────────
# Email Account Configuration Endpoints
# ─────────────────────────────────────────────────────────────

@router.get("/email-account/{client_id}")
def get_email_account_by_id(client_id: str, request: Request):
    if not (client_id == "ALL" or client_id.startswith("CLI-")):
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        logger.warning(
            f"🚫 [INVALID CLIENT ID FORMAT] Rejected client_id='{client_id}' | "
            f"From IP: {client_ip} | User-Agent: {user_agent}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid client_id format '{client_id}'. Expected 'CLI-XXXXXXXX' or 'ALL'"
        )
    try:
        account = get_email_account(client_id)
        if not account:
            raise HTTPException(status_code=404, detail=f"No account found for client_id {client_id}")
        return account
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/email-accounts")
def get_all_email_accounts_endpoint(user: dict = Depends(require_admin())):
    try:
        import pymysql
        with get_db_ctx() as db:
            cursor = db.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT 
                    u.client_id, 
                    u.email AS email, 
                    COALESCE(ea.password, '') AS password,
                    u.email AS login_email,
                    COALESCE(ea.email, u.email) AS imap_email,
                    COALESCE(ea.password, '') AS imap_password,
                    u.name,
                    u.phone_number,
                    COALESCE(ea.agent_type, 'customer_support_agent') AS agent_type,
                    COALESCE(ea.department_name, '') AS department_name,
                    COALESCE(ea.company_name, '') AS company_name,
                    COALESCE(ea.feature_ticket_creation, 1) AS feature_ticket_creation,
                    COALESCE(ea.feature_auto_send, 1) AS feature_auto_send,
                    COALESCE(ea.feature_rag, 1) AS feature_rag,
                    COALESCE(ea.feature_order_tracking, 1) AS feature_order_tracking,
                    COALESCE(ea.feature_manual_reply, 1) AS feature_manual_reply,
                    COALESCE(ea.cost_multiplier, 1.0) AS cost_multiplier,
                    ea.monthly_budget_usd,
                    u.status
                FROM users u 
                LEFT JOIN email_accounts ea ON u.client_id = ea.client_id
                WHERE u.role = 'client'
                ORDER BY u.id DESC
            """)
            rows = cursor.fetchall()
            for r in rows:
                r["feature_ticket_creation"] = bool(r.get("feature_ticket_creation", 1))
                r["feature_auto_send"] = bool(r.get("feature_auto_send", 1))
                r["feature_rag"] = bool(r.get("feature_rag", 1))
                r["feature_order_tracking"] = bool(r.get("feature_order_tracking", 1))
                r["feature_manual_reply"] = bool(r.get("feature_manual_reply", 1))
            return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-ticket", status_code=201)
def create_ticket_endpoint(data: EmailRecordRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    result = create_email_record_db(data.model_dump())
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "message": "Ticket created successfully",
        "ticket_id": result["ticket_id"],
        "client_id": result["client_id"],
        "mail_id": result["mail_id"],
        "status": data.status,
    }


@router.post("/order-status")
def order_status_endpoint(data: OrderStatusRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        order = get_order_by_id(data.client_id, data.order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return {"status": "success", "data": order}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/payment-status")
def payment_status_endpoint(data: PaymentStatusRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.connector_config import run_payment_status_lookup
        res = run_payment_status_lookup(
            client_id=data.client_id,
            payment_id=data.payment_id,
            order_id=data.payment_id
        )
        if not res.get("success"):
            raise HTTPException(status_code=404, detail=res.get("error", "Payment record not found"))
        return {"status": "success", "data": res.get("data", {})}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ticket-status")
def ticket_status_endpoint(data: TicketStatusRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        from app.connector_config import run_ticket_status_lookup
        res = run_ticket_status_lookup(
            client_id=data.client_id,
            ticket_id=data.ticket_id
        )
        if not res.get("success"):
            raise HTTPException(status_code=404, detail=res.get("error", "Ticket record not found"))
        return {"status": "success", "data": res.get("data", {})}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/insert-create_payload_ticket")
def create_payload_ticket_endpoint(data: PayloadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        res_id = insert_create_payload_ticket(client_id=data.client_id, url=data.url, paylod=data.paylod)
        return {"status": "success", "client_id": res_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/insert-payload_get_ticket")
def payload_get_ticket_endpoint(data: PayloadRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    try:
        res_id = insert_payload_get_ticket(client_id=data.client_id, url=data.url, paylod=data.paylod)
        return {"status": "success", "client_id": res_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get-create_payload/{client_id}")
def get_create_payload_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        if client_id == "ALL":
            return get_all_create_payloads()
        return get_create_payload_table(client_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get-get_payload/{client_id}")
def get_payload_get_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)
    try:
        if client_id == "ALL":
            return get_all_get_payloads()
        return get_payload_get_ticket_table(client_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# URL Allowlist Endpoints
# ─────────────────────────────────────────────────────────────

@router.post("/admin/url-allowlist", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def add_url_allowlist_endpoint(data: UrlAllowlistRequest, user: dict = Depends(require_admin())):
    try:
        entry_id = add_url_to_allowlist(data.url, user.get("client_id", "admin"))
        return {"status": "success", "id": entry_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/url-allowlist")
def list_url_allowlist_endpoint(user: dict = Depends(require_admin())):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id, scheme, netloc, path, added_by, created_at FROM url_allowlist ORDER BY created_at DESC")
            rows = cursor.fetchall()
    return [{"id": r[0], "scheme": r[1], "netloc": r[2], "path": r[3], "added_by": r[4], "created_at": str(r[5])} for r in rows]


# ─────────────────────────────────────────────────────────────
# Connector Configuration Lifecycle Endpoints
# ─────────────────────────────────────────────────────────────

@router.post("/admin/connector-configs", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def create_connector_config_endpoint(data: ConnectorConfigCreateRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    if data.status not in ("draft", "pending_approval"):
        raise HTTPException(status_code=400, detail="status must be 'draft' or 'pending_approval'")

    cap = get_connector_cap(data.client_id)
    payload = {
        "http_method": data.http_method,
        "url": data.url,
        "headers_template": data.headers_template,
        "request_template": data.request_template,
        "response_mapping": data.response_mapping,
        "auth_type": data.auth_type,
        "auth_secret_encrypted": encrypt_secret(data.auth_secret, client_id=data.client_id) if data.auth_secret else None,
        "auth_field_name": data.auth_field_name,
        "payload_encoding": data.payload_encoding,
        "base64_query_param_name": data.base64_query_param_name,
        "created_by": user.get("client_id", user.get("email", "unknown")),
    }

    try:
        row_id = insert_connector_config_checked(data.client_id, data.trigger_type, data.status, payload, cap)
        return {"status": "success", "id": row_id}
    except CapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CreationRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/connector-configs/{client_id}")
def list_connector_configs_endpoint(client_id: str, user: dict = Depends(get_current_user)):
    require_client_access(client_id, user)

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            if client_id == "ALL":
                cursor.execute("""
                    SELECT id, client_id, trigger_type, http_method, url, response_mapping,
                           auth_type, status, version, created_by, approved_by, approved_at, created_at,
                           headers_template, request_template, auth_field_name, payload_encoding, base64_query_param_name,
                           auth_secret_encrypted
                    FROM connector_configs ORDER BY created_at DESC
                """)
            else:
                cursor.execute("""
                    SELECT id, client_id, trigger_type, http_method, url, response_mapping,
                           auth_type, status, version, created_by, approved_by, approved_at, created_at,
                           headers_template, request_template, auth_field_name, payload_encoding, base64_query_param_name,
                           auth_secret_encrypted
                    FROM connector_configs WHERE client_id=%s ORDER BY created_at DESC
                """, (client_id,))
            rows = cursor.fetchall()

    configs = []
    for r in rows:
        has_regex = False
        if r[5]:
            try:
                mapping = json.loads(r[5])
                has_regex = any(f.get("extract_regex") for f in mapping.get("fields", []))
            except Exception:
                pass

        oauth_meta = {}
        auth_secret_enc = r[18]
        if auth_secret_enc and r[6] == "oauth2_client_credentials":
            try:
                decrypted = decrypt_secret(auth_secret_enc, client_id=r[1])
                oauth_json = json.loads(decrypted)
                oauth_meta = {
                    "oauth_token_url": oauth_json.get("token_url", ""),
                    "oauth_client_id": oauth_json.get("client_id", ""),
                    "oauth_grant_type": oauth_json.get("grant_type", "client_credentials"),
                    "oauth_header_prefix": oauth_json.get("header_prefix", "Bearer"),
                    "oauth_scope": oauth_json.get("scope", ""),
                    "oauth_token_auth_method": oauth_json.get("token_auth_method", "client_secret_post"),
                    "oauth_has_secret": bool(oauth_json.get("client_secret")),
                    "oauth_has_refresh_token": bool(oauth_json.get("refresh_token")),
                }
            except Exception as e:
                logger.warning(f"Failed to decrypt OAuth metadata for connector id={r[0]}: {e}")

        configs.append({
            "id": r[0], "client_id": r[1], "trigger_type": r[2], "http_method": r[3],
            "url": r[4], "response_mapping": r[5], "auth_type": r[6], "status": r[7],
            "version": r[8], "created_by": r[9], "approved_by": r[10],
            "approved_at": str(r[11]) if r[11] else None, "created_at": str(r[12]),
            "headers_template": r[13], "request_template": r[14],
            "auth_field_name": r[15], "payload_encoding": r[16], "base64_query_param_name": r[17],
            "requires_regex_review": has_regex,
            **oauth_meta,
        })
    return configs


@router.post("/admin/connector-configs/{config_id}/approve")
def approve_connector_config_endpoint(config_id: int, data: ConnectorConfigApproveRequest, user: dict = Depends(require_admin())):
    require_client_access(data.client_id, user)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT trigger_type FROM connector_configs WHERE id=%s AND client_id=%s AND status='pending_approval'",
                (config_id, data.client_id)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Config not found or not pending_approval")
            server_trigger_type = row[0]

    admin_id = user.get("client_id", "admin")
    cap = get_connector_cap(data.client_id)

    try:
        approve_connector_config(data.client_id, server_trigger_type, config_id, admin_id, cap)
        return {"status": "success", "id": config_id, "approved": True}
    except AllowlistViolationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (TemplateValidationError, ResponseMappingValidationError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CapExceededError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SwapRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/admin/connector-configs/{config_id}/reject")
def reject_connector_config_endpoint(config_id: int, data: ConnectorConfigRejectRequest, user: dict = Depends(require_admin())):
    try:
        reject_connector_config(config_id, data.client_id, user.get("client_id", "admin"), data.reason)
        return {"status": "success", "id": config_id, "rejected": True}
    except SwapRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/admin/connector-configs/{config_id}")
def delete_connector_config_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status == 'draft':
        deleted = delete_draft_connector_config(config_id, cfg_client_id)
        if not deleted:
            raise HTTPException(status_code=400, detail="Failed to delete draft connector.")
        return {"status": "success", "id": config_id, "deleted": True, "message": "Draft connector deleted."}

    elif cfg_status == 'pending_approval':
        deleted = delete_pending_connector_config(config_id, cfg_client_id)
        if not deleted:
            raise HTTPException(status_code=400, detail="Failed to delete pending approval connector.")
        return {"status": "success", "id": config_id, "deleted": True, "message": "Pending approval connector permanently deleted."}

    elif cfg_status in ('live', 'disabled', 'pending_deletion'):
        if is_admin:
            deleted = approve_delete_connector(config_id, cfg_client_id)
            if not deleted:
                raise HTTPException(status_code=400, detail="Failed to delete connector.")
            return {"status": "success", "id": config_id, "deleted": True, "message": f"{cfg_status.capitalize()} connector permanently deleted by admin."}
        else:
            if cfg_status == 'pending_deletion':
                return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Connector takedown / deletion is already pending admin approval."}
            requested = request_delete_disabled_connector(config_id, cfg_client_id)
            if not requested:
                raise HTTPException(status_code=400, detail="Failed to request takedown/deletion for connector.")
            return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Takedown and deletion requested. Awaiting administrator approval."}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete a connector in '{cfg_status}' status."
        )


@router.post("/admin/connector-configs/{config_id}/takedown")
def takedown_connector_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status != 'live':
        raise HTTPException(status_code=400, detail=f"Connector is not live (current status: '{cfg_status}')")

    if is_admin:
        disabled = disable_connector_config(config_id, cfg_client_id)
        if not disabled:
            raise HTTPException(status_code=400, detail="Could not take down connector.")
        return {"status": "success", "id": config_id, "disabled": True, "message": "Live connector taken down (disabled) by admin."}
    else:
        requested = request_delete_disabled_connector(config_id, cfg_client_id)
        if not requested:
            raise HTTPException(status_code=400, detail="Could not request takedown.")
        return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Takedown requested. Awaiting administrator review."}


@router.post("/admin/connector-configs/{config_id}/request-deletion")
def request_deletion_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    is_admin = user.get("role") == "admin" or user.get("client_id") == "admin"

    if cfg_status not in ('live', 'disabled', 'pending_deletion'):
        raise HTTPException(
            status_code=400,
            detail=f"Only live or disabled connectors can have deletion requested. Current status: '{cfg_status}'"
        )

    if is_admin:
        deleted = approve_delete_connector(config_id, cfg_client_id)
        return {"status": "success", "id": config_id, "deleted": True, "message": "Connector deleted by admin."}

    requested = request_delete_disabled_connector(config_id, cfg_client_id)
    if not requested:
        raise HTTPException(status_code=400, detail="Could not request deletion.")
    return {"status": "success", "id": config_id, "deletion_requested": True, "message": "Deletion request submitted. Awaiting admin approval."}


@router.post("/admin/connector-configs/{config_id}/approve-deletion")
def approve_deletion_endpoint(config_id: int, user: dict = Depends(require_admin())):
    deleted = approve_delete_connector(config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connector not found or not in live/disabled/pending_deletion status")
    return {"status": "success", "id": config_id, "deleted": True, "message": "Connector deletion approved and permanently deleted."}


@router.post("/admin/connector-configs/{config_id}/reject-deletion")
def reject_deletion_endpoint(config_id: int, user: dict = Depends(require_admin())):
    reverted = reject_delete_connector(config_id)
    if not reverted:
        raise HTTPException(status_code=404, detail="Connector not found or not in pending_deletion status")
    return {"status": "success", "id": config_id, "rejected": True, "message": "Deletion request rejected. Connector remains disabled."}


@router.post("/admin/connector-configs/{config_id}/cancel-deletion")
def cancel_deletion_endpoint(config_id: int, user: dict = Depends(get_current_user)):
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT client_id, status FROM connector_configs WHERE id=%s", (config_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Connector configuration not found")
            cfg_client_id, cfg_status = row[0], row[1]

    require_client_access(cfg_client_id, user)
    cancelled = cancel_delete_request(config_id, cfg_client_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Could not cancel deletion request (connector may not be pending deletion).")
    return {"status": "success", "id": config_id, "cancelled": True, "message": "Deletion request cancelled."}


@router.post("/admin/connector-configs/regenerate", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def regenerate_connector_config_endpoint(data: ConnectorConfigEditRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    if data.status not in ("draft", "pending_approval"):
        raise HTTPException(status_code=400, detail="status must be 'draft' or 'pending_approval'")

    cap = get_connector_cap(data.client_id)
    auth_secret_enc = None
    if data.auth_secret:
        if data.auth_type == "oauth2_client_credentials":
            try:
                oauth_data = json.loads(data.auth_secret)
                if oauth_data.get("client_secret") == "__KEEP_EXISTING__" or oauth_data.get("refresh_token") == "__KEEP_EXISTING__":
                    with get_db_ctx() as db:
                        with db.cursor() as cursor:
                            cursor.execute(
                                "SELECT auth_secret_encrypted FROM connector_configs WHERE client_id=%s AND trigger_type=%s AND auth_secret_encrypted IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                                (data.client_id, data.trigger_type)
                            )
                            prev_row = cursor.fetchone()
                            if prev_row and prev_row[0]:
                                try:
                                    prev_plain = decrypt_secret(prev_row[0], client_id=data.client_id)
                                    prev_oauth = json.loads(prev_plain)
                                    if oauth_data.get("client_secret") == "__KEEP_EXISTING__":
                                        oauth_data["client_secret"] = prev_oauth.get("client_secret", "")
                                    if oauth_data.get("refresh_token") == "__KEEP_EXISTING__":
                                        oauth_data["refresh_token"] = prev_oauth.get("refresh_token", "")
                                except Exception:
                                    pass
                auth_secret_enc = encrypt_secret(json.dumps(oauth_data), client_id=data.client_id)
            except Exception:
                auth_secret_enc = encrypt_secret(data.auth_secret, client_id=data.client_id)
        else:
            auth_secret_enc = encrypt_secret(data.auth_secret, client_id=data.client_id)
    else:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT auth_secret_encrypted FROM connector_configs WHERE client_id=%s AND trigger_type=%s AND auth_secret_encrypted IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                    (data.client_id, data.trigger_type)
                )
                prev_row = cursor.fetchone()
                auth_secret_enc = prev_row[0] if prev_row else None

    payload = {
        "http_method": data.http_method,
        "url": data.url,
        "headers_template": data.headers_template,
        "request_template": data.request_template,
        "response_mapping": data.response_mapping,
        "auth_type": data.auth_type,
        "auth_secret_encrypted": auth_secret_enc,
        "auth_field_name": data.auth_field_name,
        "payload_encoding": data.payload_encoding,
        "base64_query_param_name": data.base64_query_param_name,
        "created_by": user.get("client_id", user.get("email", "unknown")),
    }

    try:
        row_id = insert_connector_config_checked(
            data.client_id, data.trigger_type, data.status, payload, cap
        )
        msg_suffix = "waiting for approval" if data.status == "pending_approval" else "saved as draft"
        return {
            "status": "success",
            "id": row_id,
            "message": f"New version ({msg_suffix}) created for trigger_type='{data.trigger_type}'."
        }
    except CapExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CreationRaceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/connector-configs/test-oauth", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def test_oauth_endpoint(data: OAuthTestRequest, user: dict = Depends(get_current_user)):
    import requests
    start_time = time.time()

    effective_grant = data.grant_type or ("refresh_token" if data.refresh_token else "client_credentials")
    post_data = {"grant_type": effective_grant}
    if effective_grant == "refresh_token":
        if not data.refresh_token:
            return {
                "success": False,
                "error": "grant_type=refresh_token requires refresh_token to be provided",
                "duration_ms": 0
            }
        post_data["refresh_token"] = data.refresh_token

    if data.scope:
        post_data["scope"] = data.scope

    auth = None
    if data.token_auth_method == "client_secret_basic":
        auth = (data.client_id, data.client_secret)
    else:
        post_data["client_id"] = data.client_id
        post_data["client_secret"] = data.client_secret

    headers = {"Accept": "application/json"}

    try:
        res = requests.post(data.token_url, data=post_data, headers=headers, auth=auth, timeout=10)
        duration_ms = round((time.time() - start_time) * 1000)

        if res.status_code != 200:
            return {
                "success": False,
                "status_code": res.status_code,
                "error": f"Token endpoint returned HTTP {res.status_code}: {res.text[:300]}",
                "duration_ms": duration_ms
            }

        res_json = res.json()
        access_token = res_json.get("access_token")
        if not access_token:
            return {
                "success": False,
                "status_code": res.status_code,
                "error": f"No 'access_token' found in JSON response: {res_json}",
                "duration_ms": duration_ms
            }

        return {
            "success": True,
            "token_type": res_json.get("token_type", data.header_prefix or "Bearer"),
            "expires_in": res_json.get("expires_in", 3600),
            "scope": res_json.get("scope", data.scope or ""),
            "duration_ms": duration_ms,
            "message": f"OAuth 2.0 ({effective_grant}) token handshake successful!"
        }
    except Exception as e:
        duration_ms = round((time.time() - start_time) * 1000)
        return {
            "success": False,
            "error": str(e),
            "duration_ms": duration_ms
        }


@router.post("/admin/connector-configs/generate-preview", dependencies=[Depends(RedisRateLimiter(limit=10, window=60))])
def generate_connector_template_preview_endpoint(data: GenerateTemplatePreviewRequest, user: dict = Depends(get_current_user)):
    require_client_access(data.client_id, user)
    result = generate_connector_template(
        trigger_type=data.trigger_type,
        crm_schema_description=data.crm_schema_description,
        sample_response=data.sample_response,
    )
    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error", "Template generation failed"))

    return {
        "status": "success",
        "trigger_type": data.trigger_type,
        "request_template": result["request_template"],
        "response_mapping": result["response_mapping"],
        "note": "This is a preview only — nothing has been saved."
    }


# =====================================================================
# 📦 Action Outbox Telemetry & Sweeper
# =====================================================================

class SweepOutboxRequest(BaseModel):
    max_age_seconds: int = 120


@router.get("/admin/action-outbox")
def get_action_outbox_telemetry(
    client_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(require_admin())
):
    from app.action_outbox import get_action_logs
    records = get_action_logs(client_id=client_id, status=status, limit=limit)
    return {"status": "success", "count": len(records), "records": records}


@router.post("/admin/action-outbox/sweep")
def trigger_outbox_sweep(
    data: Optional[SweepOutboxRequest] = None,
    user: dict = Depends(require_admin())
):
    from app.action_outbox import sweep_stale_actions
    max_age = data.max_age_seconds if data else 120
    res = sweep_stale_actions(max_age_seconds=max_age)
    return {"status": "success", **res}

