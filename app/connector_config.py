# app/connector_config.py — add near the top, after imports
import json
import re
from app.context_data import CONTEXT_DATA_KEYS
import logging
from app.db import get_db

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

REQUIRED_RESPONSE_FIELDS = {
    "order_status": {"docket_no", "ticket_status"},
    "ticket_status": {"docket_no", "ticket_status"},
    "ticket_create": {"ticket_id"},
}

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

    Unlike an earlier version of this function, the full mapped response
    data is passed through (not just ticket_id) — a CRM's response_mapping
    may include extra fields (status, priority, remarks, etc.) beyond the
    hard-required ticket_id, and callers like _create_ticket_and_reply
    read resp.get("status", ...) / resp.get("priority", ...) expecting
    those to be present when the CRM actually returns them.
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector

    config = get_live_config(client_id, "ticket_create")
    if config is None:
        return {"success": False, "ticket_id": None, "error": f"No live ticket_create connector config for client_id={client_id}"}

    context_base = build_context_data_base(
        client_id=client_id, from_email=from_email, subject=subject,
        body=body, cleaned_body=body, ticket_id=None,
        intent=intent, sentiment=sentiment, priority=priority,
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

    

def run_order_status_lookup(
    client_id: str,
    ticket_id: str,
    body: str,
    history: list,
    subject: str = "",
    from_email: str = "",
    old_summary: str = "",
    intent: str = "ticket_status",
    sentiment: str = "Neutral",
    priority: str = "Medium",
) -> dict:
    """
    Drop-in replacement for app.request_handler.get_order_status.
    Seamlessly supports both ticket_status (e.g. Zoho Desk) and
    order_status (e.g. Shopify) connectors. If both are active, it queries
    the most relevant one first and falls back to the other.
    """
    from app.context_data import build_context_data_base
    from app.connector_executor import execute_connector

    cfg_ticket = get_live_config(client_id, "ticket_status")
    cfg_order = get_live_config(client_id, "order_status")

    # Determine which connector applies based on inquiry content & ID format
    text_corpus = f"{subject} {body}".lower()
    is_order_inquiry = any(w in text_corpus for w in ("order", "ship", "shipped", "shipping", "deliver", "delivery", "track", "tracking", "package", "item", "purchase"))
    is_ticket_inquiry = any(w in text_corpus for w in ("ticket", "case", "complaint", "issue", "billing", "escalat"))

    ticket_str = str(ticket_id or "").strip()
    looks_like_crm_ticket = len(ticket_str) > 8 or ticket_str.upper().startswith(("T-", "INC", "CAS", "SR", "REQ"))

    selected_config = None
    if cfg_ticket and cfg_order:
        if is_order_inquiry and not is_ticket_inquiry:
            selected_config = cfg_order
        elif is_ticket_inquiry and not is_order_inquiry:
            selected_config = cfg_ticket
        elif looks_like_crm_ticket:
            selected_config = cfg_ticket
        else:
            selected_config = cfg_order
    elif cfg_ticket:
        selected_config = cfg_ticket
    elif cfg_order:
        selected_config = cfg_order
    else:
        return {"success": False, "error": f"No live ticket_status or order_status connector config for client_id={client_id}"}

    context_base = build_context_data_base(
        client_id=client_id, from_email=from_email, subject=subject,
        body=body, cleaned_body=body, ticket_id=ticket_id,
        intent=intent, sentiment=sentiment, priority=priority,
    )

    result = execute_connector(selected_config, context_base, body=body, history=history, old_summary=old_summary)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Unknown executor failure")}

    data = result.get("data", {})
    has_record = any(data.get(k) for k in ("docket_no", "ticket_status", "ticket_id"))

    # If an e-commerce order (like Shopify) wasn't found with plain '1001', retry with '%231001' (#1001)
    if not has_record and selected_config.get("trigger_type") == "order_status" and ticket_id and not str(ticket_id).startswith("#"):
        context_base_hash = dict(context_base)
        context_base_hash["ticket_id"] = f"%23{ticket_id}"
        result_hash = execute_connector(selected_config, context_base_hash, body=body, history=history, old_summary=old_summary)
        if result_hash.get("success"):
            data_hash = result_hash.get("data", {})
            if any(data_hash.get(k) for k in ("docket_no", "ticket_status", "ticket_id")):
                return {"success": True, "data": data_hash}

    if not has_record:
        return {"success": False, "error": f"No matching record found in {selected_config.get('trigger_type')} system response"}

    return {"success": True, "data": data}
    
       

def get_live_config(client_id: str, trigger_type: str) -> dict | None:
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

class ResponseMappingValidationError(Exception):
    """
    Raised at approval time when a response_mapping's configured fields
    don't include the hard-required minimum set for its trigger_type.
    Trigger_types not present in REQUIRED_RESPONSE_FIELDS have no
    required fields — this validation is a no-op for them (keeps
    trigger_type mostly a free routing string, per spec, with only these
    two known types carrying a hard floor).
    """
    pass


def _extract_configured_fields(mapping: dict) -> set:
    """Extracts field names whether mapping is {"fields": [{"field": ...}]} or {"ticket_id": "id", ...}"""
    if isinstance(mapping, dict) and "fields" in mapping and isinstance(mapping["fields"], list):
        return {f.get("field") for f in mapping["fields"] if isinstance(f, dict) and "field" in f}
    if isinstance(mapping, dict):
        return {k for k in mapping.keys() if k != "pagination"}
    return set()


def _validate_response_mapping_fields(trigger_type: str, response_mapping_json: str | None) -> None:
    required = REQUIRED_RESPONSE_FIELDS.get(trigger_type)
    if not required:
        return  # unknown/custom trigger_type — no required-field floor

    if not response_mapping_json:
        raise ResponseMappingValidationError(
            f"trigger_type='{trigger_type}' requires response_mapping with fields "
            f"{sorted(required)}, but response_mapping is empty."
        )

    try:
        mapping = json.loads(response_mapping_json) if isinstance(response_mapping_json, str) else response_mapping_json
    except json.JSONDecodeError as e:
        raise ResponseMappingValidationError(f"response_mapping is not valid JSON: {e}")

    if not isinstance(mapping, dict):
        raise ResponseMappingValidationError("response_mapping must be a JSON object.")

    configured_fields = _extract_configured_fields(mapping)
    missing = required - configured_fields
    if missing:
        raise ResponseMappingValidationError(
            f"trigger_type='{trigger_type}' response_mapping is missing required "
            f"field(s): {sorted(missing)}. Configured fields: {sorted(configured_fields)}"
        )

class TemplateValidationError(Exception):
    """
    Raised at approval time when request_template or headers_template
    references a placeholder not in CONTEXT_DATA_KEYS. Distinct from
    AllowlistViolationError — this is a template-authoring defect, not a
    URL policy rejection.
    """
    pass


def _validate_template_placeholders(template_json: str | None) -> None:
    """
    template_json: the raw JSON string stored in request_template or
    headers_template (or None). Checks every {{key}} placeholder found
    anywhere in the string against CONTEXT_DATA_KEYS.
    """
    if not template_json:
        return
    found_keys = set(_PLACEHOLDER_RE.findall(template_json))
    unknown = found_keys - CONTEXT_DATA_KEYS
    if unknown:
        raise TemplateValidationError(
            f"Template references unknown placeholder(s): {sorted(unknown)} — "
            f"not in context_data schema. Allowed keys: {sorted(CONTEXT_DATA_KEYS)}"
        )


def ensure_connector_configs_table():
    """
    One-time startup call, mirrors ensure_accounts_table_startup /
    _ensure_table conventions elsewhere in this codebase.
    """
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS connector_configs (
                    id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
                    client_id             VARCHAR(50) NOT NULL,
                    trigger_type          VARCHAR(100) NOT NULL,
                    http_method           VARCHAR(10) NOT NULL,
                    url                   VARCHAR(500) NOT NULL,
                    headers_template      JSON,
                    request_template      JSON,
                    response_mapping      JSON,
                    auth_type             ENUM('bearer','basic','api_key_header','api_key_query','oauth2_client_credentials') NOT NULL,
                    auth_secret_encrypted TEXT,
                    auth_field_name       VARCHAR(100) NULL,
                    payload_encoding        ENUM('plain','base64_query') NOT NULL DEFAULT 'plain',
                    base64_query_param_name VARCHAR(50) NULL,
                    status                ENUM('draft','pending_approval','live','disabled','pending_deletion') NOT NULL DEFAULT 'draft',
                    version               INT NOT NULL DEFAULT 1,
                    created_by            VARCHAR(50),
                    approved_by           VARCHAR(50) NULL,
                    approved_at           TIMESTAMP NULL,
                    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    live_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'live' THEN trigger_type ELSE NULL END) STORED,
                    pending_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'pending_approval' THEN trigger_type ELSE NULL END) STORED,
                    UNIQUE INDEX uq_client_live_trigger (client_id, live_marker),
                    UNIQUE INDEX uq_client_pending_trigger (client_id, pending_marker),
                    INDEX idx_client_status (client_id, status),
                    INDEX idx_trigger_type (trigger_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # pending_marker / uq_client_pending_trigger — already applied
            # directly to the live dev table; kept here for fresh installs
            # where CREATE TABLE IF NOT EXISTS above won't have run yet.
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    ADD COLUMN pending_marker VARCHAR(100)
                        GENERATED ALWAYS AS (CASE WHEN status = 'pending_approval' THEN trigger_type ELSE NULL END) STORED
                """)
            except Exception:
                pass

            try:
                cursor.execute(
                    "ALTER TABLE connector_configs ADD UNIQUE INDEX uq_client_pending_trigger (client_id, pending_marker)"
                )
            except Exception:
                pass

            # OAuth 2.0 ENUM upgrade migration for existing tables
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    MODIFY COLUMN auth_type ENUM('bearer','basic','api_key_header','api_key_query','oauth2_client_credentials') NOT NULL
                """)
            except Exception:
                pass

            # Pending Deletion ENUM upgrade migration for existing tables
            try:
                cursor.execute("""
                    ALTER TABLE connector_configs
                    MODIFY COLUMN status ENUM('draft','pending_approval','live','disabled','pending_deletion') NOT NULL DEFAULT 'draft'
                """)
            except Exception:
                pass

        conn.commit()
        logger.info("✅ connector_configs table ensured")
    except Exception as e:
        logger.error(f"❌ Failed to ensure connector_configs table: {e}", exc_info=True)
        raise
    finally:
        conn.close()







class CapExceededError(Exception):
    """Raised when a client's live or pending_approval cap would be exceeded."""
    pass


class CreationRaceError(Exception):
    """
    Raised only on the 'live' path, where uq_client_live_trigger backs it.
    Audience: whoever submitted (client or admin) — message is about
    concurrent creation, not approval.
    """
    pass


def insert_connector_config_checked(client_id, trigger_type, new_status, payload, cap):
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

def _is_deadlock_or_lock_timeout(exc) -> bool:
    """MySQL error 1213 = deadlock, 1205 = lock wait timeout. Both are
    transient contention errors, not data-integrity violations — treat
    them the same as a caught race: tell the caller to retry."""
    import pymysql
    if isinstance(exc, pymysql.err.OperationalError):
        return exc.args and exc.args[0] in (1213, 1205)
    return False

def activate_first_live(client_id, trigger_type, new_config_id, approving_admin, cap):
    from app.url_allowlist import is_url_allowed

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
            # 
            # import time; time.sleep(3)
            # 
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

def approve_connector_config(client_id, trigger_type, new_config_id, approving_admin, cap):
    """
    Single entry point for approving a pending_approval row to live.
    Routes to swap_to_live() if a live row already exists for this
    (client_id, trigger_type), or activate_first_live() if not.

    Step 9's approval endpoint must call this — never swap_to_live or
    activate_first_live directly — so the routing decision lives in one
    place. Note: the SELECT below is unlocked, so the routing decision
    itself can theoretically race with a concurrent approval for the
    same client/trigger_type. That's tolerated, not ignored — both
    downstream functions independently re-verify their own assumptions
    (rowcount checks, FOR UPDATE count) and degrade to SwapRaceError or
    CapExceededError rather than corrupting state. This has NOT been
    tested under concurrency yet — see the required test below.
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

def _is_integrity_error(exc) -> bool:
    """pymysql raises IntegrityError for unique-constraint violations."""
    import pymysql
    return isinstance(exc, pymysql.err.IntegrityError)






class SwapRaceError(Exception):
    """
    Distinct from CreationRaceError — different function, different race,
    different audience (admin approving, not client submitting), different
    message. Fires when two admins concurrently approve different
    pending_approval rows for the same (client_id, trigger_type), or when
    this function is bypassed and something else violates the live
    constraint directly.
    """
    pass


def swap_to_live(client_id, trigger_type, new_config_id, old_config_id, approving_admin):
    from app.url_allowlist import is_url_allowed

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



class AllowlistViolationError(Exception):
    """
    Raised when a connector config's URL is not on url_allowlist at
    approval time. Distinct from CapExceededError/SwapRaceError — this is
    a policy rejection, not a concurrency conflict, and needs its own
    message so the admin understands it's not a race, it's a missing
    allowlist entry.
    """
    pass




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




def generate_connector_template(trigger_type: str, crm_schema_description: str, sample_response: str = "") -> dict:
    """
    Preview-only: calls the LLM to draft a request_template + response_mapping
    pair for a given trigger_type, based on a human-provided description of
    the target CRM. Does NOT write to the database — caller must review
    (and may edit) the output, then submit it through the normal
    create_connector_config / regenerate endpoint to actually persist it.

    Returns either:
      {"success": True, "request_template": {...}, "response_mapping": {...}}
    or
      {"success": False, "error": str, "raw_output": str}
    on any failure — LLM call failure, invalid JSON, or failed validation
    against the same rules a human-submitted config must pass.
    """
    from app.llm import client, resolve_model, current_client_id
    from app.context_data import CONTEXT_DATA_KEYS
    import re as _re

    required_fields = REQUIRED_RESPONSE_FIELDS.get(trigger_type, set())

    prompt = f"""
You are generating a request template and response mapping for a generic
HTTP connector system. Your output will be validated by strict rules —
follow them exactly.

## Available placeholders (ONLY these may be used, exactly as {{{{key}}}} — no others, no invented field names)
{sorted(CONTEXT_DATA_KEYS)}

## Trigger type
{trigger_type}

## Required response_mapping output fields for this trigger_type
{sorted(required_fields) if required_fields else "(none strictly required, but include whatever is useful)"}

## Target CRM description (human-provided)
{crm_schema_description}

## Sample CRM response (if provided, use this to write accurate JMESPath paths)
{sample_response or "(none provided — use best judgment on likely response shape)"}

## Rules
- Only include fields in request_template that the description clearly specifies or strongly implies. Do NOT include every available placeholder defensively — a bloated template with unused/inappropriate fields (like internal classification signals such as intent/sentiment) is wrong even if syntactically valid.
- If a CRM field has no reasonable match among the available placeholders, OMIT that field entirely from request_template. NEVER emit a literal null or an empty string as a placeholder value — an unmappable field should not appear in the output at all.
- If the description is too vague to determine specific fields, use only the most common/obvious ones (typically: a subject-like field, a description/body-like field, an email field) rather than including everything available.

## Task
1. Produce a `request_template`: a JSON object representing the request body
   this system should send, using {{{{key}}}} placeholders ONLY from the
   allowed list above, mapped sensibly to what the target CRM likely expects
   based on the description.
2. Produce a `response_mapping`: a JSON object with a "fields" array. Each
   entry has "field" (output name), "path" (a JMESPath expression into the
   CRM's response), and "extract_regex" (null, unless the value needs regex
   extraction from a larger string — e.g. an ID embedded in a sentence).
   MUST include all of the required output fields listed above.

## Output format — return ONLY this JSON structure, nothing else:
{{
  "request_template": {{ ... }},
  "response_mapping": {{ "fields": [ {{ "field": "...", "path": "...", "extract_regex": null }} ] }}
}}

No markdown, no explanation, no code fences. Raw JSON only.
"""

    try:
        res = client.chat.completions.create(
            model=resolve_model(current_client_id.get(), "generate_connector_template"),
            messages=[
                {"role": "system", "content": "You are a JSON-only response system. Return ONLY valid JSON. No markdown, no explanation."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        output = res.choices[0].message.content.strip()
    except Exception as e:
        return {"success": False, "error": f"LLM call failed: {e}", "raw_output": ""}

    match = _re.search(r'\{.*\}', output, _re.DOTALL)
    if not match:
        return {"success": False, "error": "No JSON object found in LLM response", "raw_output": output}

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"LLM output is not valid JSON: {e}", "raw_output": output}

    request_template = parsed.get("request_template")
    response_mapping = parsed.get("response_mapping")

    if request_template is None or response_mapping is None:
        return {"success": False, "error": "LLM output missing request_template or response_mapping key", "raw_output": output}

    # Defensive post-processing: strip any field the LLM emitted for something
    # it couldn't actually map (null, empty string, or no placeholder at all).
    # Prompt instructions alone proved unreliable at preventing this — see
    # test 5 in the standalone test round, which kept emitting
    # "customerPhoneNumber": null despite explicit "never emit null" wording.
    request_template = _strip_unmapped_fields(request_template)

    # Run through the SAME validation a human-submitted config must pass —
    # no exemption for LLM-generated output.
    try:
        request_template_str = json.dumps(request_template)
        response_mapping_str = json.dumps(response_mapping)
        _validate_template_placeholders(request_template_str)
        _validate_response_mapping_fields(trigger_type, response_mapping_str)
    except (TemplateValidationError, ResponseMappingValidationError) as e:
        return {"success": False, "error": f"Generated template failed validation: {e}", "raw_output": output}

    return {"success": True, "request_template": request_template, "response_mapping": response_mapping}


def _strip_unmapped_fields(request_template: dict) -> dict:
    """
    Removes any key whose value doesn't contain a {{placeholder}} —
    covers null, empty string, and any literal value the LLM emitted
    for a field it couldn't actually map. Belt-and-suspenders against
    prompt instructions the model doesn't reliably follow.
    """
    return {
        k: v for k, v in request_template.items()
        if isinstance(v, str) and _PLACEHOLDER_RE.search(v)
    }


def delete_draft_connector_config(config_id: int, client_id: str | None = None) -> bool:
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


def delete_pending_connector_config(config_id: int, client_id: str | None = None) -> bool:
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


def request_delete_disabled_connector(config_id: int, client_id: str | None = None) -> bool:
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


def disable_connector_config(config_id: int, client_id: str | None = None) -> bool:
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


def approve_delete_connector(config_id: int, client_id: str | None = None) -> bool:
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


def reject_delete_connector(config_id: int, client_id: str | None = None) -> bool:
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


def cancel_delete_request(config_id: int, client_id: str | None = None) -> bool:
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