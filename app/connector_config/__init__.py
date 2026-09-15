"""
app.connector_config package.
Maintains 100% backwards compatibility with previous app.connector_config module.
"""

from app.connector_config.exceptions import (
    CapExceededError,
    CreationRaceError,
    SwapRaceError,
    AllowlistViolationError,
    ResponseMappingValidationError,
    TemplateValidationError,
)

from app.connector_config.validation import (
    REQUIRED_RESPONSE_FIELDS,
    _extract_configured_fields,
    _validate_response_mapping_fields,
    _validate_template_placeholders,
    _strip_unmapped_fields,
)

from app.connector_config.table import (
    ensure_connector_configs_table,
)

from app.connector_config.lifecycle import (
    _is_integrity_error,
    _is_deadlock_or_lock_timeout,
    insert_connector_config_checked,
    activate_first_live,
    swap_to_live,
    approve_connector_config,
    reject_connector_config,
    delete_draft_connector_config,
    delete_pending_connector_config,
    request_delete_disabled_connector,
    disable_connector_config,
    approve_delete_connector,
    reject_delete_connector,
    cancel_delete_request,
)

from app.connector_config.dispatch import (
    get_live_config,
    run_ticket_create,
    run_order_status_lookup,
    run_ticket_status_lookup,
    run_payment_status_lookup,
)

from app.connector_config.generator import (
    generate_connector_template,
)

__all__ = [
    # Exceptions
    "CapExceededError",
    "CreationRaceError",
    "SwapRaceError",
    "AllowlistViolationError",
    "ResponseMappingValidationError",
    "TemplateValidationError",
    # Validation & Constants
    "REQUIRED_RESPONSE_FIELDS",
    "_extract_configured_fields",
    "_validate_response_mapping_fields",
    "_validate_template_placeholders",
    "_strip_unmapped_fields",
    # DDL
    "ensure_connector_configs_table",
    # Lifecycle
    "_is_integrity_error",
    "_is_deadlock_or_lock_timeout",
    "insert_connector_config_checked",
    "activate_first_live",
    "swap_to_live",
    "approve_connector_config",
    "reject_connector_config",
    "delete_draft_connector_config",
    "delete_pending_connector_config",
    "request_delete_disabled_connector",
    "disable_connector_config",
    "approve_delete_connector",
    "reject_delete_connector",
    "cancel_delete_request",
    # Runtime Dispatch
    "get_live_config",
    "run_ticket_create",
    "run_order_status_lookup",
    "run_ticket_status_lookup",
    "run_payment_status_lookup",
    # Generator
    "generate_connector_template",
]
