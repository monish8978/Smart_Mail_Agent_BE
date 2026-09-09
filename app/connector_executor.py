"""
app/connector_executor.py

Generic executor for connector_configs: render request_template from
context_data → fire HTTP request → apply response_mapping → return a
result shaped like get_order_status()'s existing {"success", "data"/"error"}
contract, so worker/tasks.py's PATH A/B/C branches don't need restructuring.

Zero per-trigger_type Python here — trigger_type is only used by the
caller to look up which config row to pass in.

Caller contract: `config` must be a dict containing ALL columns needed
for execution — url, http_method, headers_template, request_template,
response_mapping, auth_type, auth_secret_encrypted, auth_field_name,
payload_encoding, base64_query_param_name. This is a separate, full
fetch from whatever narrow SELECT swap_to_live/activate_first_live do
for approval-time validation — those two are unrelated read paths
against the same table.
"""

import base64
import json
import logging
import re
import requests
import jmespath

from app.context_data import CONTEXT_DATA_KEYS, resolve_expensive_keys
from app.url_allowlist import is_url_allowed
from app.secrets_crypto import decrypt_secret

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
REQUEST_TIMEOUT_SECONDS = 10


class ExecutorError(Exception):
    """
    Base class for all executor failures. worker/tasks.py should catch
    this (or its subclasses) and fall to manual review — same degraded
    path as 'no config exists yet', per the spec's explicit requirement
    that a zero-live-config or executor failure never propagates as an
    unhandled exception.
    """
    pass


class TemplateRenderError(ExecutorError):
    """Placeholder references an unknown key, or a referenced key's value is missing/None."""
    pass


class ExecutorAllowlistError(ExecutorError):
    """URL failed the execution-time allowlist re-check."""
    pass


def _find_placeholders(template_json: str | None) -> set:
    if not template_json:
        return set()
    return set(_PLACEHOLDER_RE.findall(template_json))


def _render_template(template_json: str | dict | None, context: dict) -> dict | None:
    """
    Substitutes {{key}} placeholders in template_json with JSON-escaped
    values from context, returns the parsed dict. Fails loudly on any
    unknown or missing/None placeholder.
    """
    if not template_json:
        return None

    if isinstance(template_json, dict):
        template_json = json.dumps(template_json)

    def _replace(match):
        key = match.group(1)
        if key not in CONTEXT_DATA_KEYS:
            raise TemplateRenderError(
                f"Template references unknown placeholder '{{{{{key}}}}}' — "
                f"not in context_data schema."
            )
        if key not in context or context[key] is None:
            raise TemplateRenderError(
                f"Template placeholder '{{{{{key}}}}}' has no value in this "
                f"email's context (missing or None) — cannot render."
            )
        escaped = json.dumps(str(context[key]))[1:-1]
        return escaped

    rendered_str = _PLACEHOLDER_RE.sub(_replace, template_json)
    try:
        return json.loads(rendered_str)
    except json.JSONDecodeError as e:
        raise TemplateRenderError(
            f"Rendered template is not valid JSON: {e}. Rendered string: {rendered_str[:500]}"
        ) from e


def _render_string(template_str: str | None, context: dict) -> str | None:
    if not template_str:
        return template_str
    def _replace(match):
        key = match.group(1)
        val = context.get(key)
        return str(val) if val is not None else match.group(0)
    return _PLACEHOLDER_RE.sub(_replace, template_str)


import hashlib
import redis
import os

_REDIS_URL = os.getenv("REDIS_URL", "redis://mail_ai_redis:6379/0") or "redis://localhost:6379/0"
try:
    _redis_client = redis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=2)
except Exception:
    _redis_client = None


def _fetch_oauth2_token(client_id: str, secret_json_str: str) -> tuple[str, str]:
    """
    Fetches and caches an OAuth 2.0 access token (client_credentials or refresh_token).
    Returns (access_token, header_prefix).
    Uses Redis cache key: `oauth2_token:{client_id}:{hash(creds)}`.
    """
    try:
        creds = json.loads(secret_json_str) if isinstance(secret_json_str, str) else secret_json_str
    except Exception as e:
        raise ExecutorError(f"Invalid OAuth 2.0 secret JSON: {e}")

    token_url = creds.get("token_url")
    oauth_client_id = creds.get("client_id")
    oauth_client_secret = creds.get("client_secret")
    refresh_token = creds.get("refresh_token")
    scope = creds.get("scope")
    auth_method = creds.get("token_auth_method", "client_secret_post")
    header_prefix = creds.get("header_prefix", "Bearer")
    grant_type = creds.get("grant_type") or ("refresh_token" if refresh_token else "client_credentials")

    if not token_url or not oauth_client_id or not oauth_client_secret:
        raise ExecutorError("OAuth 2.0 requires token_url, client_id, and client_secret in auth_secret")

    if grant_type == "refresh_token" and not refresh_token:
        raise ExecutorError("OAuth 2.0 refresh_token grant requires refresh_token in auth_secret")

    # Generate stable cache key
    creds_seed = f"{token_url}:{oauth_client_id}:{refresh_token or 'cc'}"
    creds_hash = hashlib.sha256(creds_seed.encode()).hexdigest()[:16]
    cache_key = f"oauth2_token:{client_id}:{creds_hash}"

    # Try Redis Cache
    if _redis_client:
        try:
            cached_token = _redis_client.get(cache_key)
            if cached_token:
                logger.info(f"⚡ OAuth 2.0 token cache hit for client_id={client_id}")
                return cached_token, header_prefix
        except Exception as err:
            logger.warning(f"⚠️ Redis OAuth token read failed: {err}")

    # Fetch new token
    logger.info(f"🔑 Requesting fresh OAuth 2.0 token ({grant_type}) from {token_url} for client_id={client_id}")
    data = {"grant_type": grant_type}
    if grant_type == "refresh_token":
        data["refresh_token"] = refresh_token
    if scope:
        data["scope"] = scope

    headers = {"Accept": "application/json"}
    auth = None

    if auth_method == "client_secret_basic":
        auth = (oauth_client_id, oauth_client_secret)
    else:
        data["client_id"] = oauth_client_id
        data["client_secret"] = oauth_client_secret

    try:
        res = requests.post(token_url, data=data, headers=headers, auth=auth, timeout=10)
        res.raise_for_status()
        token_data = res.json()
    except Exception as e:
        raise ExecutorError(f"OAuth 2.0 token request failed: {e}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise ExecutorError(f"OAuth 2.0 response did not contain access_token: {token_data}")

    expires_in = token_data.get("expires_in", 3600)
    try:
        expires_in = int(expires_in)
    except (ValueError, TypeError):
        expires_in = 3600

    # Cache with safety buffer (e.g. 60 seconds before actual expiry)
    ttl = max(expires_in - 60, 30)
    if _redis_client:
        try:
            _redis_client.setex(cache_key, ttl, access_token)
            logger.info(f"✅ Cached OAuth 2.0 token for client_id={client_id} (TTL={ttl}s)")
        except Exception as err:
            logger.warning(f"⚠️ Failed to cache OAuth token in Redis: {err}")

    return access_token, header_prefix


def _apply_auth(request_kwargs: dict, auth_type: str, secret: str, auth_field_name: str | None, client_id: str = "system") -> None:
    if auth_type == "bearer":
        request_kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {secret}"
    elif auth_type == "basic":
        try:
            creds = json.loads(secret)
            if isinstance(creds, dict):
                request_kwargs["auth"] = (creds.get("username", ""), creds.get("password", "X"))
            else:
                request_kwargs["auth"] = (str(creds), "X")
        except (json.JSONDecodeError, TypeError):
            # Raw API key string (e.g. Freshdesk API key with password 'X')
            request_kwargs["auth"] = (secret, "X")
    elif auth_type == "api_key_header":
        if not auth_field_name:
            raise ExecutorError("auth_type=api_key_header requires auth_field_name")
        request_kwargs.setdefault("headers", {})[auth_field_name] = secret
    elif auth_type == "api_key_query":
        if not auth_field_name:
            raise ExecutorError("auth_type=api_key_query requires auth_field_name")
        request_kwargs.setdefault("params", {})[auth_field_name] = secret
    elif auth_type in ("oauth2_client_credentials", "oauth2_refresh_token", "oauth2"):
        token, prefix = _fetch_oauth2_token(client_id, secret)
        request_kwargs.setdefault("headers", {})["Authorization"] = f"{prefix} {token}"
    else:
        raise ExecutorError(f"Unknown auth_type: {auth_type}")


def _apply_response_mapping(response_json: dict, response_mapping) -> dict:
    """
    response_mapping: raw JSON string from the DB column (pymysql returns
    JSON columns as str, confirmed by direct test), or None, or already
    a dict if a caller passes one directly (defensive — accept both).
    """
    if response_mapping is None:
        return response_json

    if isinstance(response_mapping, str):
        try:
            response_mapping = json.loads(response_mapping)
        except json.JSONDecodeError as e:
            logger.error(f"❌ response_mapping is not valid JSON: {e}")
            return response_json

    if not isinstance(response_mapping, dict):
        return response_json

    if response_mapping.get("pagination", {}).get("enabled"):
        logger.warning(
            "⚠️ response_mapping.pagination.enabled=True but pagination is NOT "
            "implemented in this executor — ignoring pagination config, "
            "returning only the first page's mapped fields. Per spec, "
            "pagination is scaffolded/unvalidated and must not be trusted "
            "until tested against a real integration."
        )

    field_specs = []
    if "fields" in response_mapping and isinstance(response_mapping["fields"], list):
        field_specs = response_mapping["fields"]
    else:
        for k, v in response_mapping.items():
            if k == "pagination":
                continue
            if isinstance(v, str):
                field_specs.append({"field": k, "path": v, "extract_regex": None})
            elif isinstance(v, dict):
                field_specs.append({"field": k, "path": v.get("path"), "extract_regex": v.get("extract_regex")})

    if not field_specs:
        return response_json

    result = {}
    for field_spec in field_specs:
        if not isinstance(field_spec, dict):
            continue
        field_name = field_spec.get("field")
        if not field_name:
            continue
        path = field_spec.get("path")
        extract_regex = field_spec.get("extract_regex")

        value = None
        if path:
            try:
                value = jmespath.search(path, response_json)
            except Exception as e:
                logger.warning(f"⚠️ JMESPath extraction failed for field={field_name} path={path}: {e}")
                value = None

        if extract_regex and isinstance(value, str):
            match = re.search(extract_regex, value)
            if match:
                value = match.group(0)
            else:
                logger.warning(
                    f"⚠️ extract_regex '{extract_regex}' did not match extracted "
                    f"value for field={field_name} — keeping raw JMESPath result"
                )

        result[field_name] = value

    return result

def execute_connector(
    config: dict,
    context_base: dict,
    body: str,
    history: list,
    old_summary: str = "",
) -> dict:
    """
    config: full row dict from connector_configs — url, http_method,
    headers_template, request_template, response_mapping, auth_type,
    auth_secret_encrypted, auth_field_name, payload_encoding,
    base64_query_param_name.

    context_base: output of build_context_data_base() — cheap fields only.

    Returns {"success": True, "data": {...}} or
            {"success": False, "error": str}
    — never raises to the caller.
    """
    try:
        request_template = config.get("request_template")
        headers_template = config.get("headers_template")

        needed = _find_placeholders(request_template) | _find_placeholders(headers_template)
        expensive_needed = needed & (CONTEXT_DATA_KEYS - context_base.keys())
        expensive_values = resolve_expensive_keys(expensive_needed, body, history, old_summary) if expensive_needed else {}

        full_context = {**context_base, **expensive_values}

        rendered_body = _render_template(request_template, full_context)
        rendered_headers = _render_template(headers_template, full_context) or {}

        raw_url = config["url"]
        needed = needed | _find_placeholders(raw_url)
        url = _render_string(raw_url, full_context)
        if not is_url_allowed(raw_url) and not is_url_allowed(url):
            raise ExecutorAllowlistError(
                f"URL '{url}' failed execution-time allowlist re-check — "
                f"it may have been removed from the allowlist since this config was approved."
            )

        secret = decrypt_secret(config["auth_secret_encrypted"]) if config.get("auth_secret_encrypted") else None

        request_kwargs = {"headers": dict(rendered_headers), "timeout": REQUEST_TIMEOUT_SECONDS}
        if secret:
            _apply_auth(request_kwargs, config["auth_type"], secret, config.get("auth_field_name"), client_id=config.get("client_id", "system"))

        payload_encoding = config.get("payload_encoding", "plain")
        http_method = config.get("http_method", "POST").upper()

        safe_headers = {k: ("***" if "auth" in k.lower() else v) for k, v in request_kwargs.get("headers", {}).items()}
        logger.info(f"🚀 Outgoing Connector Request: {http_method} {url} | Headers: {safe_headers}")

        if payload_encoding == "base64_query":
            param_name = config.get("base64_query_param_name")
            if not param_name:
                raise ExecutorError(
                    "payload_encoding='base64_query' but base64_query_param_name is not set — "
                    "this should have been caught at insert time; config is invalid."
                )
            encoded = base64.b64encode(json.dumps(rendered_body or {}).encode()).decode()
            request_kwargs.setdefault("params", {})[param_name] = encoded

            if http_method == "GET":
                response = requests.get(url, **request_kwargs)
            elif http_method == "POST":
                response = requests.post(url, **request_kwargs)  # payload is in query param, no json body
            else:
                raise ExecutorError(f"Unsupported http_method: {http_method}")

        elif payload_encoding == "plain":
            if http_method == "GET":
                request_kwargs.setdefault("params", {}).update(rendered_body or {})
                response = requests.get(url, **request_kwargs)
            elif http_method == "POST":
                response = requests.post(url, json=rendered_body, **request_kwargs)
            else:
                raise ExecutorError(f"Unsupported http_method: {http_method}")
        else:
            raise ExecutorError(f"Unknown payload_encoding: {payload_encoding}")

        response.raise_for_status()
        response_json = response.json()
        logger.info(f"📥 Connector Response [{response.status_code}]: {response_json}")

        mapped = _apply_response_mapping(response_json, config.get("response_mapping"))
        return {"success": True, "data": mapped}

    except ExecutorError as e:
        logger.error(f"❌ Executor error: {e}")
        return {"success": False, "error": str(e)}
    except requests.exceptions.RequestException as e:
        resp_text = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                resp_text = f" — Response Body: {e.response.text}"
            except Exception:
                pass
        logger.error(f"❌ Executor HTTP call failed: {e}{resp_text}")
        return {"success": False, "error": f"{e}{resp_text}"}
    except Exception as e:
        logger.error(f"❌ Executor unexpected failure: {e}", exc_info=True)
        return {"success": False, "error": str(e)}




REQUIRED_DISPLAY_LABELS = {
    "docket_no": "Ticket ID",
    "ticket_status": "Status",
    "ticket_id": "Ticket ID",
}


def format_mapped_data_for_prompt(mapped_data: dict) -> str:
    """
    Generic formatter for CRM response data going into an LLM prompt.
    Required fields (docket_no/ticket_status/ticket_id) get friendly
    labels if present; every other key present in mapped_data gets
    included generically as 'Key Name: value'. Missing optional fields
    are simply omitted — no 'N/A' padding, since an LLM writing a reply
    doesn't need to see placeholders for data a given CRM never provided.
    """
    lines = []
    for key, value in mapped_data.items():
        if value is None or value == "":
            continue
        label = REQUIRED_DISPLAY_LABELS.get(key, key.replace("_", " ").title())
        lines.append(f"{label}: {value}")
    return "\n".join(lines)