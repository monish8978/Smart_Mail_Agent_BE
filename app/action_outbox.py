import hashlib
import json
import logging
from typing import Callable, Any, Optional, Dict
from app.db import get_db_ctx

logger = logging.getLogger(__name__)


def compute_idempotency_key(client_id: str, message_id: str, action_type: str) -> str:
    """
    Computes a deterministic SHA256 key for a specific external action.
    """
    raw = f"{client_id.strip().lower()}:{message_id.strip()}:{action_type.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execute_idempotent_action(
    client_id: str,
    message_id: str,
    action_type: str,
    action_fn: Callable[..., Any],
    *args,
    extract_ref_fn: Optional[Callable[[Any], Optional[str]]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Executes an external side-effect (e.g. CRM ticket creation or SMTP email send)
    guaranteeing at-most-once execution using action_logs idempotency tracking.

    Returns:
        {
            "success": bool,
            "already_completed": bool,
            "external_ref": Optional[str],
            "result": Any,
            "error": Optional[str]
        }
    """
    if not message_id:
        # Fallback if no RFC message-id is provided: use caller-supplied args hash
        seed = f"{client_id}:{action_type}:{args}:{kwargs}"
        message_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()

    key = compute_idempotency_key(client_id, message_id, action_type)

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, external_ref FROM action_logs WHERE idempotency_key = %s FOR UPDATE",
                (key,)
            )
            existing = cursor.fetchone()

            if existing:
                status, ext_ref = existing[0], existing[1]
                if status == "completed":
                    logger.info(
                        f"⏭️ [Idempotency] Action '{action_type}' already completed for key {key[:12]}... "
                        f"Reusing ref: {ext_ref}"
                    )
                    return {
                        "success": True,
                        "already_completed": True,
                        "external_ref": ext_ref,
                        "result": None,
                        "error": None
                    }
                elif status == "pending":
                    logger.warning(
                        f"🔄 [Idempotency] Action '{action_type}' for key {key[:12]}... is in 'pending' state. "
                        f"Re-executing to ensure completion."
                    )
            else:
                cursor.execute(
                    "INSERT INTO action_logs (idempotency_key, client_id, action_type, status) VALUES (%s, %s, %s, 'pending')",
                    (key, client_id, action_type)
                )
        db.commit()

    # Execute external action outside database transaction to avoid holding locks
    try:
        result = action_fn(*args, **kwargs)
        ext_ref = None
        if extract_ref_fn and callable(extract_ref_fn):
            try:
                ext_ref = extract_ref_fn(result)
            except Exception as ref_err:
                logger.warning(f"⚠️ Failed to extract external_ref from action result: {ref_err}")

        # Check if result is a dict with status or ticket_id
        if not ext_ref and isinstance(result, dict):
            ext_ref = result.get("ticket_id") or result.get("Refrence_No") or result.get("message_id")

        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute(
                    "UPDATE action_logs SET status = 'completed', external_ref = %s, error_message = NULL WHERE idempotency_key = %s",
                    (ext_ref, key)
                )
            db.commit()

        return {
            "success": True,
            "already_completed": False,
            "external_ref": ext_ref,
            "result": result,
            "error": None
        }

    except Exception as e:
        err_msg = str(e)
        logger.error(f"❌ [Idempotency] Action '{action_type}' failed for key {key[:12]}...: {err_msg}")
        try:
            with get_db_ctx() as db:
                with db.cursor() as cursor:
                    cursor.execute(
                        "UPDATE action_logs SET status = 'failed', error_message = %s WHERE idempotency_key = %s",
                        (err_msg[:500], key)
                    )
                db.commit()
        except Exception as update_err:
            logger.error(f"⚠️ Failed to record action failure in action_logs: {update_err}")

        return {
            "success": False,
            "already_completed": False,
            "external_ref": None,
            "result": None,
            "error": err_msg
        }


def sweep_stale_actions(max_age_seconds: int = 120) -> Dict[str, Any]:
    """
    Scans action_logs for records stuck in 'pending' status longer than max_age_seconds
    and transitions them to 'failed' with a diagnostic error message.
    """
    swept_keys = []
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT idempotency_key, client_id, action_type, created_at
                FROM action_logs
                WHERE status = 'pending'
                  AND created_at < DATE_SUB(NOW(), INTERVAL %s SECOND)
            """, (max_age_seconds,))
            stale_rows = cursor.fetchall()

            for row in stale_rows:
                key = row[0]
                cursor.execute("""
                    UPDATE action_logs 
                    SET status = 'failed', 
                        error_message = 'Action timed out in pending state without completion'
                    WHERE idempotency_key = %s AND status = 'pending'
                """, (key,))
                swept_keys.append(key)

            db.commit()

    if swept_keys:
        logger.warning(f"🧹 [Outbox Sweeper] Swept {len(swept_keys)} stale pending actions: {swept_keys}")
    else:
        logger.info("🧹 [Outbox Sweeper] 0 stale pending actions found")

    return {
        "swept_count": len(swept_keys),
        "swept_keys": swept_keys
    }


def get_action_logs(client_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """
    Returns telemetry records from action_logs ordered by creation time descending.
    """
    query = "SELECT idempotency_key, client_id, action_type, status, external_ref, error_message, created_at, updated_at FROM action_logs"
    params = []
    conditions = []

    if client_id and client_id != "ALL":
        conditions.append("client_id = %s")
        params.append(client_id)
    if status:
        conditions.append("status = %s")
        params.append(status)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

    return [
        {
            "idempotency_key": r[0],
            "client_id": r[1],
            "action_type": r[2],
            "status": r[3],
            "external_ref": r[4],
            "error_message": r[5],
            "created_at": str(r[6]) if r[6] else None,
            "updated_at": str(r[7]) if r[7] else None,
        }
        for r in rows
    ]

