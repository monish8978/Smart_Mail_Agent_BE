import json
import re
from typing import Dict, Any

from app.llm import client, resolve_model, current_client_id
from app.context_data import CONTEXT_DATA_KEYS
from app.connector_config.exceptions import (
    TemplateValidationError,
    ResponseMappingValidationError,
)
from app.connector_config.validation import (
    REQUIRED_RESPONSE_FIELDS,
    _validate_template_placeholders,
    _validate_response_mapping_fields,
    _strip_unmapped_fields,
)


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
            caller="generate_connector_template",
            messages=[
                {"role": "system", "content": "You are a JSON-only response system. Return ONLY valid JSON. No markdown, no explanation."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        output = res.choices[0].message.content.strip()
    except Exception as e:
        return {"success": False, "error": f"LLM call failed: {e}", "raw_output": ""}

    match = re.search(r'\{.*\}', output, re.DOTALL)
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
