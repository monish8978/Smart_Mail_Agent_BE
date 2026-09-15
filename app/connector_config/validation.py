import json
import re
from typing import Optional, Set, Dict, Any

from app.context_data import CONTEXT_DATA_KEYS
from app.connector_config.exceptions import (
    ResponseMappingValidationError,
    TemplateValidationError,
)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

REQUIRED_RESPONSE_FIELDS = {
    "order_status": {"docket_no", "ticket_status"},
    "ticket_status": {"docket_no", "ticket_status"},
    "ticket_create": {"ticket_id"},
    "payment_status": {"payment_status"},
}


def _extract_configured_fields(mapping: dict) -> Set[str]:
    """Extracts field names whether mapping is {"fields": [{"field": ...}]} or {"ticket_id": "id", ...}"""
    if isinstance(mapping, dict) and "fields" in mapping and isinstance(mapping["fields"], list):
        return {f.get("field") for f in mapping["fields"] if isinstance(f, dict) and "field" in f}
    if isinstance(mapping, dict):
        return {k for k in mapping.keys() if k != "pagination"}
    return set()


def _validate_response_mapping_fields(trigger_type: str, response_mapping_json: Optional[str | dict]) -> None:
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


def _validate_template_placeholders(template_json: Optional[str]) -> None:
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
