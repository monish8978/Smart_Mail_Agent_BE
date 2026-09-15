import logging
from typing import Optional, List, Dict, Any

from app.db import get_db

logger = logging.getLogger(__name__)


def get_live_config(client_id: str, trigger_type: str) -> Optional[dict]:
    """
    Fetches the live connector_configs row for (client_id, trigger_type),
    or None if none exists. Callers (worker/tasks.py) must treat None
    the same as get_order_status's {"success": False} — fall to the
    existing verification/ticket-creation fallback path, never raise.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT url, http_method, headers_template, request_template,
                       response_mapping, auth_type, auth_secret_encrypted,
                       auth_field_name, payload_encoding, base64_query_param_name,
                       trigger_type
                FROM connector_configs
                WHERE client_id=%s AND trigger_type=%s AND status='live'
                LIMIT 1
            """, (client_id, trigger_type))
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "client_id": client_id,
                "url": row[0], "http_method": row[1], "headers_template": row[2],
                "request_template": row[3], "response_mapping": row[4],
                "auth_type": row[5], "auth_secret_encrypted": row[6],
                "auth_field_name": row[7], "payload_encoding": row[8],
                "base64_query_param_name": row[9],
                "trigger_type": row[10],
            }
    finally:
        conn.close()


def run_ticket_create(
    client_id: str,
    from_email: str,
    subject: str,
    body: str,
    history: list,
    status: str = "Ticket_Generated",
    old_summary: str = "",
    intent: str = "general_query",
    sentiment: str = "Neutral",
    priority: str = "Medium",
) -> dict:
    """
    Drop-in replacement for app.request_handler.call_create_ticket's core
    API-call responsibility. Matches call_create_ticket's return shape:
    {"success": bool, "ticket_id": str|None, ...extra CRM-returned fields}.
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector
    from app.utils import normalize_subject

    config = get_live_config(client_id, "ticket_create")
    if config is None:
        return {"success": False, "ticket_id": None, "error": f"No live ticket_create connector config for client_id={client_id}"}

    clean_sub = normalize_subject(subject, body)

    context_base = build_context_data_base(
        client_id=client_id, from_email=from_email, subject=clean_sub,
        body=body, cleaned_body=body, ticket_id=None,
        intent=intent, sentiment=sentiment, priority=priority,
        history=history,
    )

    result = execute_connector(config, context_base, body=body, history=history, old_summary=old_summary)
    if not result.get("success"):
        return {"success": False, "ticket_id": None, "error": result.get("error", "Unknown executor failure")}

    mapped = result["data"]
    reserved_keys = {"success", "ticket_id", "message"}
    collision = reserved_keys & mapped.keys() - {"ticket_id"}  # ticket_id collision is expected/harmless
    if collision:
        logger.warning(
            f"⚠️ response_mapping for ticket_create produced reserved field name(s) "
            f"{collision} — these will be silently overwritten by run_ticket_create's "
            f"own return contract. Rename these fields in response_mapping to avoid confusion."
        )
    return {
        "success": True,
        "ticket_id": mapped.get("ticket_id"),
        "message": "Ticket created successfully",
        **{k: v for k, v in mapped.items() if k not in ("success", "message")},
    }


def run_payment_status_lookup(
    client_id: str,
    payment_id: str = "",
    order_id: str = "",
    body: str = "",
    history: list = None,
    subject: str = "",
    from_email: str = "",
    old_summary: str = "",
    intent: str = "payment_status",
    sentiment: str = "Neutral",
    priority: str = "Medium",
) -> dict:
    """
    Executes real-time payment status lookup against payment gateways
    (e.g., Stripe, Razorpay, Shopify Payments, Zoho Books).
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector

    cfg_payment = get_live_config(client_id, "payment_status")
    if not cfg_payment:
        return {
            "success": False,
            "error": f"No live payment_status connector config found for client_id={client_id}"
        }

    ref = str(payment_id or order_id or "").strip()
    context_base = build_context_data_base(
        client_id=client_id,
        from_email=from_email,
        subject=subject,
        body=body,
        cleaned_body=body,
        ticket_id=ref,
        order_id=order_id or ref,
        payment_id=payment_id or ref,
        reference_id=ref,
        intent=intent,
        sentiment=sentiment,
        priority=priority,
        history=history or [],
    )

    result = execute_connector(cfg_payment, context_base, body=body, history=history or [], old_summary=old_summary)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Unknown payment connector failure")}

    data = result.get("data", {})
    has_record = any(data.get(k) for k in ("payment_status", "status", "transaction_id", "amount", "id"))
    if not has_record:
        return {"success": False, "error": f"No matching payment record found for reference '{ref}'"}

    return {"success": True, "data": data}


def run_ticket_status_lookup(
    client_id: str,
    ticket_id: str,
    body: str = "",
    history: list = None,
    subject: str = "",
    from_email: str = "",
    old_summary: str = "",
    intent: str = "ticket_status",
    sentiment: str = "Neutral",
    priority: str = "Medium",
) -> dict:
    """
    Specifically queries CRM ticket_status connectors (e.g. Zoho Desk, Freshdesk, Zendesk).
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector

    cfg_ticket = get_live_config(client_id, "ticket_status")
    if not cfg_ticket:
        # Fallback to order_status if client configured it under order_status
        cfg_ticket = get_live_config(client_id, "order_status")

    if not cfg_ticket:
        return {"success": False, "error": f"No live ticket_status connector config for client_id={client_id}"}

    clean_ticket_id = str(ticket_id or "").strip().lstrip("#")
    context_base = build_context_data_base(
        client_id=client_id, from_email=from_email, subject=subject,
        body=body, cleaned_body=body, ticket_id=clean_ticket_id,
        order_id=clean_ticket_id, reference_id=clean_ticket_id,
        intent=intent, sentiment=sentiment, priority=priority,
        history=history or [],
    )

    result = execute_connector(cfg_ticket, context_base, body=body, history=history or [], old_summary=old_summary)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Unknown executor failure")}

    data = result.get("data", {})
    has_record = any(data.get(k) for k in ("docket_no", "ticket_status", "ticket_id", "status"))
    if not has_record:
        return {"success": False, "error": f"No matching ticket record found for '{ticket_id}'"}

    return {"success": True, "data": data}


def run_order_status_lookup(
    client_id: str,
    ticket_id: str = "",
    body: str = "",
    history: list = None,
    subject: str = "",
    from_email: str = "",
    old_summary: str = "",
    intent: str = "order_status",
    sentiment: str = "Neutral",
    priority: str = "Medium",
    order_id: str = "",
) -> dict:
    """
    Queries e-commerce/ERP order_status connectors (e.g. Shopify, Magento, ERP).
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector

    clean_order_id = str(order_id or ticket_id or "").strip()

    cfg_order = get_live_config(client_id, "order_status")
    cfg_ticket = get_live_config(client_id, "ticket_status")
    selected_config = cfg_order or cfg_ticket

    if not selected_config:
        return {"success": False, "error": f"No live order_status connector config for client_id={client_id}"}

    context_base = build_context_data_base(
        client_id=client_id, from_email=from_email, subject=subject,
        body=body, cleaned_body=body, ticket_id=clean_order_id,
        order_id=clean_order_id, reference_id=clean_order_id,
        intent=intent, sentiment=sentiment, priority=priority,
        history=history or [],
    )

    result = execute_connector(selected_config, context_base, body=body, history=history or [], old_summary=old_summary)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Unknown executor failure")}

    data = result.get("data", {})
    has_record = any(data.get(k) for k in ("docket_no", "ticket_status", "ticket_id", "status", "order_id"))

    # If an e-commerce order (like Shopify) wasn't found with plain '1001', retry with '%231001' (#1001)
    if not has_record and selected_config.get("trigger_type") == "order_status" and clean_order_id and not clean_order_id.startswith("#"):
        context_base_hash = dict(context_base)
        context_base_hash["ticket_id"] = f"%23{clean_order_id}"
        context_base_hash["order_id"] = f"%23{clean_order_id}"
        result_hash = execute_connector(selected_config, context_base_hash, body=body, history=history or [], old_summary=old_summary)
        if result_hash.get("success"):
            data_hash = result_hash.get("data", {})
            if any(data_hash.get(k) for k in ("docket_no", "ticket_status", "ticket_id", "status", "order_id")):
                return {"success": True, "data": data_hash}

    if not has_record:
        return {"success": False, "error": f"No matching record found for '{clean_order_id}' in {selected_config.get('trigger_type')} system response"}

    return {"success": True, "data": data}
