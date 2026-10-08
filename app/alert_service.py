import logging
import json
import time
from typing import Optional, Dict, Any
import httpx

from app.db import get_db_ctx
from app.redis_pool import get_redis_main

logger = logging.getLogger(__name__)


def get_alert_settings(client_id: str) -> Dict[str, Any]:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    webhook_url, alert_email, 
                    COALESCE(alert_on_sla_breach, 1) as alert_on_sla_breach,
                    COALESCE(alert_on_worker_stall, 1) as alert_on_worker_stall,
                    COALESCE(alert_sla_hours, 4) as alert_sla_hours
                FROM email_accounts 
                WHERE client_id = %s 
                LIMIT 1
            """, (client_id,))
            row = cursor.fetchone()
            if not row:
                return {
                    "webhook_url": "",
                    "alert_email": "",
                    "alert_on_sla_breach": True,
                    "alert_on_worker_stall": True,
                    "alert_sla_hours": 4
                }
            return {
                "webhook_url": row[0] or "",
                "alert_email": row[1] or "",
                "alert_on_sla_breach": bool(row[2]),
                "alert_on_worker_stall": bool(row[3]),
                "alert_sla_hours": int(row[4] or 4)
            }


def update_alert_settings(client_id: str, data: Dict[str, Any]) -> bool:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                UPDATE email_accounts 
                SET webhook_url = %s,
                    alert_email = %s,
                    alert_on_sla_breach = %s,
                    alert_on_worker_stall = %s,
                    alert_sla_hours = %s
                WHERE client_id = %s
            """, (
                data.get("webhook_url") or None,
                data.get("alert_email") or None,
                1 if data.get("alert_on_sla_breach", True) else 0,
                1 if data.get("alert_on_worker_stall", True) else 0,
                int(data.get("alert_sla_hours") or 4),
                client_id
            ))
            if cursor.rowcount == 0:
                cursor.execute("SELECT id FROM email_accounts WHERE client_id = %s LIMIT 1", (client_id,))
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO email_accounts (
                            client_id, email, password, webhook_url, alert_email,
                            alert_on_sla_breach, alert_on_worker_stall, alert_sla_hours
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        client_id,
                        f"alerts-{client_id.lower()}@internal.system",
                        "managed-credential-placeholder",
                        data.get("webhook_url") or None,
                        data.get("alert_email") or None,
                        1 if data.get("alert_on_sla_breach", True) else 0,
                        1 if data.get("alert_on_worker_stall", True) else 0,
                        int(data.get("alert_sla_hours") or 4),
                    ))
            db.commit()
    return True


def dispatch_webhook(webhook_url: str, payload: Dict[str, Any]) -> bool:
    """
    Sends payload to webhook_url with timeout and error handling.
    Formats multi-platform payload compatible with generic JSON, Slack, and Discord.
    """
    if not webhook_url or not webhook_url.startswith("http"):
        return False

    title = payload.get("title", "Mail AI Operational Alert")
    description = payload.get("description", "")
    severity = payload.get("severity", "warning").upper()
    client_id = payload.get("client_id", "SYSTEM")

    # Slack format
    slack_payload = {
        "text": f"🚨 *[{severity}] {title}* (Tenant: `{client_id}`)\n{description}",
        "attachments": [
            {
                "color": "#E01E5A" if severity == "CRITICAL" else "#ECB22E",
                "fields": [
                    {"title": k, "value": str(v), "short": True}
                    for k, v in payload.get("details", {}).items()
                ],
                "footer": "Mail AI Automation Sentinel",
                "ts": int(time.time())
            }
        ]
    }

    # Universal combined payload
    combined_payload = {
        **payload,
        **slack_payload,
        "content": f"🚨 **[{severity}] {title}** (Tenant: `{client_id}`)\n{description}",
    }

    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.post(webhook_url, json=combined_payload)
            if resp.status_code in (200, 204):
                logger.info(f"✅ Webhook alert successfully delivered to {webhook_url[:35]}...")
                return True
            else:
                logger.warning(f"⚠️ Webhook rejected with HTTP {resp.status_code}: {resp.text[:150]}")
                return False
    except Exception as e:
        logger.error(f"❌ Webhook dispatch failed: {e}")
        return False


def trigger_alert_if_needed(
    client_id: str, 
    event_type: str, 
    title: str, 
    description: str, 
    details: Optional[Dict[str, Any]] = None,
    cooldown_seconds: int = 1800
) -> bool:
    """
    Fires an alert if configured and not throttled by Redis cooldown.
    """
    settings = get_alert_settings(client_id)
    webhook_url = settings.get("webhook_url")
    if not webhook_url:
        return False

    if event_type == "sla_breach" and not settings.get("alert_on_sla_breach", True):
        return False
    if event_type == "worker_stall" and not settings.get("alert_on_worker_stall", True):
        return False

    try:
        r = get_redis_main()
        cd_key = f"alert_cooldown:{client_id}:{event_type}"
        # Set NX with expiry: if key already exists, cooldown is active -> skip
        acquired = r.set(cd_key, "1", ex=cooldown_seconds, nx=True)
        if not acquired:
            logger.info(f"⏳ Alert [{event_type}] for client {client_id} is in cooldown — skipping dispatch")
            return False
    except Exception as r_err:
        logger.warning(f"⚠️ Redis cooldown check failed: {r_err}")

    payload = {
        "event_type": event_type,
        "client_id": client_id,
        "title": title,
        "description": description,
        "severity": "CRITICAL" if event_type in ("worker_stall", "dlq_poison") else "WARNING",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "details": details or {}
    }
    return dispatch_webhook(webhook_url, payload)
