import logging
from typing import Optional
import pymysql

from app.db import get_db
from app.url_allowlist import is_url_allowed
from app.connector_config.exceptions import (
    CapExceededError,
    CreationRaceError,
    SwapRaceError,
    AllowlistViolationError,
)
from app.connector_config.validation import (
    _validate_template_placeholders,
    _validate_response_mapping_fields,
)

logger = logging.getLogger(__name__)


def _is_integrity_error(exc) -> bool:
    """pymysql raises IntegrityError for unique-constraint violations."""
    return isinstance(exc, pymysql.err.IntegrityError)


def _is_deadlock_or_lock_timeout(exc) -> bool:
    """
    MySQL error 1213 = deadlock, 1205 = lock wait timeout. Both are
    transient contention errors, not data-integrity violations — treat
    them the same as a caught race: tell the caller to retry.
    """
    if isinstance(exc, pymysql.err.OperationalError):
        return bool(exc.args and exc.args[0] in (1213, 1205))
    return False


def insert_connector_config_checked(client_id: str, trigger_type: str, new_status: str, payload: dict, cap: int) -> int:
    """
    The ONLY permitted entry point for count-changing writes to
    connector_configs (draft->pending_approval, or a direct pending->live
    single-row insert path if one ever exists — currently live rows are
    only created via swap_to_live(), see connector_config.py).

    payload: dict of the row fields to insert — http_method, url,
    headers_template, request_template, response_mapping, auth_type,
    auth_secret_encrypted, auth_field_name, created_by. Caller is
    responsible for encrypting auth_secret_encrypted before calling this.
    """
    if new_status not in ("draft", "pending_approval", "live"):
        raise ValueError(f"Unsupported new_status for checked insert: {new_status}")

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.begin()

            if new_status == "live":
                cursor.execute(
                    "SELECT COUNT(*) FROM connector_configs WHERE client_id=%s AND status='live' FOR UPDATE",
                    (client_id,)
                )
                live_count = cursor.fetchone()[0]
                if live_count >= cap:
                    conn.rollback()
                    raise CapExceededError(
                        f"Live connector cap exceeded for client {client_id}: {live_count}/{cap}"
                    )

            elif new_status == "pending_approval":
                cursor.execute(
                    "SELECT COUNT(*) FROM connector_configs WHERE client_id=%s AND status='pending_approval' FOR UPDATE",
                    (client_id,)
                )
                pending_count = cursor.fetchone()[0]
                if pending_count >= cap:
                    conn.rollback()
                    raise CapExceededError(
                        f"Pending-approval connector cap exceeded for client {client_id}: {pending_count}/{cap}"
                    )
            # 'draft': no lock, no cap check — uncapped by design

            if payload.get("payload_encoding") == "base64_query" and not payload.get("base64_query_param_name"):
                conn.rollback()
                raise ValueError(
                    "payload_encoding='base64_query' requires base64_query_param_name to be set"
                )

            try:
                cursor.execute("""
                    INSERT INTO connector_configs
                        (client_id, trigger_type, http_method, url, headers_template,
                         request_template, response_mapping, auth_type,
                         auth_secret_encrypted, auth_field_name, payload_encoding,
                         base64_query_param_name, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    client_id, trigger_type, payload["http_method"], payload["url"],
                    payload.get("headers_template"), payload.get("request_template"),
                    payload.get("response_mapping"), payload["auth_type"],
                    payload.get("auth_secret_encrypted"), payload.get("auth_field_name"),
                    payload.get("payload_encoding", "plain"), payload.get("base64_query_param_name"),
                    new_status, payload.get("created_by")
                ))
            except Exception as insert_err:
                conn.rollback()
                if new_status in ("live", "pending_approval") and _is_integrity_error(insert_err):
                    raise CreationRaceError(
                        "A configuration for this trigger type is already being created/edited — "
                        "refresh to see the current state before submitting again."
                    ) from insert_err
                if new_status in ("live", "pending_approval") and _is_deadlock_or_lock_timeout(insert_err):
                    raise CreationRaceError(
                        "A concurrent submission caused a database lock conflict — "
                        "please retry."
                    ) from insert_err
                raise

            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def activate_first_live(client_id: str, trigger_type: str, new_config_id: int, approving_admin: str, cap: int) -> None:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT url, request_template, headers_template, response_mapping "
                "FROM connector_configs WHERE id=%s AND client_id=%s",
                (new_config_id, client_id)
            )
            row = cursor.fetchone()
            if row is None:
                raise SwapRaceError(
                    f"Config id={new_config_id} not found for client {client_id} — "
                    "it may have been deleted or edited before this approval completed."
                )
            new_url, request_template, headers_template, response_mapping = row

        _validate_template_placeholders(request_template)
        _validate_template_placeholders(headers_template)
        _validate_response_mapping_fields(trigger_type, response_mapping)

        if not is_url_allowed(new_url):
            raise AllowlistViolationError(
                f"URL '{new_url}' is not on the allowlist — add it via /admin/url-allowlist "
                f"before approving this configuration."
            )

        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.begin()

            cursor.execute(
                "SELECT COUNT(*) FROM connector_configs WHERE client_id=%s AND status='live' FOR UPDATE",
                (client_id,)
            )
            live_count = cursor.fetchone()[0]
            if live_count >= cap:
                conn.rollback()
                raise CapExceededError(
                    f"Live connector cap exceeded for client {client_id}: {live_count}/{cap}"
                )

            try:
                cursor.execute(
                    "UPDATE connector_configs SET status='live', approved_by=%s, approved_at=NOW() "
                    "WHERE id=%s AND client_id=%s AND status='pending_approval'",
                    (approving_admin, new_config_id, client_id)
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    raise SwapRaceError(
                        "The configuration being approved is no longer pending_approval — "
                        "it may have already been approved, rejected, or edited. Refresh and re-review."
                    )
            except SwapRaceError:
                raise
            except Exception as update_err:
                conn.rollback()
                if _is_integrity_error(update_err):
                    raise SwapRaceError(
                        "Another activation for this client/trigger_type completed concurrently — "
                        "refresh and re-review before retrying."
                    ) from update_err
                if _is_deadlock_or_lock_timeout(update_err):
                    raise SwapRaceError(
                        "Another activation for this client/trigger_type completed concurrently — "
                        "refresh and re-review before retrying."
                    ) from update_err
                raise

            conn.commit()
    finally:
        conn.close()


def swap_to_live(client_id: str, trigger_type: str, new_config_id: int, old_config_id: Optional[int], approving_admin: str) -> None:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT url, request_template, headers_template, response_mapping "
                "FROM connector_configs WHERE id=%s AND client_id=%s",
                (new_config_id, client_id)
            )
            row = cursor.fetchone()
            if row is None:
                raise SwapRaceError(
                    f"Config id={new_config_id} not found for client {client_id} — "
                    "it may have been deleted or edited before this approval completed."
                )
            new_url, request_template, headers_template, response_mapping = row

        _validate_template_placeholders(request_template)
        _validate_template_placeholders(headers_template)
        _validate_response_mapping_fields(trigger_type, response_mapping)

        if not is_url_allowed(new_url):
            raise AllowlistViolationError(
                f"URL '{new_url}' is not on the allowlist — add it via /admin/url-allowlist "
                f"before approving this configuration."
            )

        with conn.cursor() as cursor:
            conn.begin()

            if old_config_id is not None:
                try:
                    cursor.execute(
                        "UPDATE connector_configs SET status='disabled' "
                        "WHERE id=%s AND client_id=%s AND status='live'",
                        (old_config_id, client_id)
                    )
                    if cursor.rowcount == 0:
                        conn.rollback()
                        raise SwapRaceError(
                            "The current live configuration for this trigger type "
                            "changed before this approval completed — refresh and re-review."
                        )
                except SwapRaceError:
                    raise
                except Exception as disable_err:
                    conn.rollback()
                    if _is_deadlock_or_lock_timeout(disable_err):
                        raise SwapRaceError(
                            "A concurrent approval caused a database lock conflict — "
                            "please retry this approval."
                        ) from disable_err
                    raise

            try:
                cursor.execute(
                    "UPDATE connector_configs SET status='live', approved_by=%s, approved_at=NOW() "
                    "WHERE id=%s AND client_id=%s AND status='pending_approval'",
                    (approving_admin, new_config_id, client_id)
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    raise SwapRaceError(
                        "The configuration being approved is no longer pending_approval — "
                        "it may have already been approved, rejected, or edited. Refresh and re-review."
                    )
            except SwapRaceError:
                raise
            except Exception as update_err:
                conn.rollback()
                if _is_integrity_error(update_err):
                    raise SwapRaceError(
                        "Another approval for this client/trigger_type completed concurrently — "
                        "refresh and re-review before retrying."
                    ) from update_err
                if _is_deadlock_or_lock_timeout(update_err):
                    raise SwapRaceError(
                        "A concurrent approval caused a database lock conflict — "
                        "please retry this approval."
                    ) from update_err
                raise

            conn.commit()
    finally:
        conn.close()


def approve_connector_config(client_id: str, trigger_type: str, new_config_id: int, approving_admin: str, cap: int) -> None:
    """
    Single entry point for approving a pending_approval row to live.
    Routes to swap_to_live() if a live row already exists for this
    (client_id, trigger_type), or activate_first_live() if not.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM connector_configs WHERE client_id=%s AND trigger_type=%s AND status='live'",
                (client_id, trigger_type)
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    if row:
        return swap_to_live(client_id, trigger_type, new_config_id, row[0], approving_admin)
    else:
        return activate_first_live(client_id, trigger_type, new_config_id, approving_admin, cap)


def reject_connector_config(config_id: int, client_id: str, rejecting_admin: str, reason: str = "") -> None:
    """
    Marks a pending_approval row as disabled, freeing its pending-cap
    slot. Does NOT delete the row — keeps it for audit purposes, same as
    everything else in this table. Only valid on rows currently in
    pending_approval; rejecting an already-live or already-disabled row
    is a no-op detected via rowcount==0, not silently ignored.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE connector_configs SET status='disabled' "
                "WHERE id=%s AND client_id=%s AND status='pending_approval'",
                (config_id, client_id)
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise SwapRaceError(
                    f"Config id={config_id} is not currently pending_approval for "
                    f"client {client_id} — it may have already been approved, "
                    f"rejected, or deleted. Refresh and re-review."
                )
            conn.commit()
        logger.info(f"🚫 Connector config id={config_id} rejected by {rejecting_admin}: {reason}")
    finally:
        conn.close()


def delete_draft_connector_config(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Deletes a connector configuration row if and only if it is in 'draft' status.
    If client_id is provided, enforces client_id ownership.
    Returns True if deleted, False if not found or not in draft status.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND client_id=%s AND status='draft'",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND status='draft'",
                    (config_id,)
                )
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"🗑️ Deleted draft connector config id={config_id} (client_id={client_id})")
            return deleted
    finally:
        conn.close()


def delete_pending_connector_config(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Deletes a connector configuration row if and only if it is in 'pending_approval' status.
    If client_id is provided, enforces client_id ownership.
    Returns True if deleted, False if not found or not in pending_approval status.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND client_id=%s AND status='pending_approval'",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND status='pending_approval'",
                    (config_id,)
                )
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"🗑️ Deleted pending_approval connector config id={config_id} (client_id={client_id})")
            return deleted
    finally:
        conn.close()


def request_delete_disabled_connector(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Transitions a live or disabled connector config to 'pending_deletion' status.
    Requires client or admin ownership.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "UPDATE connector_configs SET status='pending_deletion' WHERE id=%s AND client_id=%s AND status IN ('live', 'disabled')",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "UPDATE connector_configs SET status='pending_deletion' WHERE id=%s AND status IN ('live', 'disabled')",
                    (config_id,)
                )
            updated = cursor.rowcount > 0
            conn.commit()
            if updated:
                logger.info(f"⏳ Requested takedown/deletion for connector config id={config_id} (client_id={client_id})")
            return updated
    finally:
        conn.close()


def disable_connector_config(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Transitions a live connector config to 'disabled' status (immediately taking it offline).
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND client_id=%s AND status='live'",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND status='live'",
                    (config_id,)
                )
            updated = cursor.rowcount > 0
            conn.commit()
            if updated:
                logger.info(f"⛔ Took down (disabled) live connector config id={config_id} (client_id={client_id})")
            return updated
    finally:
        conn.close()


def approve_delete_connector(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Admin approval to permanently delete a connector config row in 'live', 'disabled' or 'pending_deletion' status.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND client_id=%s AND status IN ('live', 'disabled', 'pending_deletion')",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "DELETE FROM connector_configs WHERE id=%s AND status IN ('live', 'disabled', 'pending_deletion')",
                    (config_id,)
                )
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"🗑️ Admin approved permanent deletion of connector config id={config_id}")
            return deleted
    finally:
        conn.close()


def reject_delete_connector(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Admin rejection of a deletion request: reverts status from 'pending_deletion' back to 'disabled'.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND client_id=%s AND status='pending_deletion'",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND status='pending_deletion'",
                    (config_id,)
                )
            reverted = cursor.rowcount > 0
            conn.commit()
            if reverted:
                logger.info(f"↩️ Admin rejected deletion for connector config id={config_id}, reverted to disabled")
            return reverted
    finally:
        conn.close()


def cancel_delete_request(config_id: int, client_id: Optional[str] = None) -> bool:
    """
    Client or admin cancels a pending deletion request: reverts status from 'pending_deletion' back to 'disabled'.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if client_id and client_id != "ALL":
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND client_id=%s AND status='pending_deletion'",
                    (config_id, client_id)
                )
            else:
                cursor.execute(
                    "UPDATE connector_configs SET status='disabled' WHERE id=%s AND status='pending_deletion'",
                    (config_id,)
                )
            cancelled = cursor.rowcount > 0
            conn.commit()
            if cancelled:
                logger.info(f"↩️ Deletion request cancelled for connector config id={config_id}")
            return cancelled
    finally:
        conn.close()
