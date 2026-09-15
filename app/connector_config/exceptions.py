"""
Domain exceptions for connector configurations, validations, and concurrency control.
"""

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


class AllowlistViolationError(Exception):
    """
    Raised when a connector config's URL is not on url_allowlist at
    approval time. Distinct from CapExceededError/SwapRaceError — this is
    a policy rejection, not a concurrency conflict, and needs its own
    message so the admin understands it's not a race, it's a missing
    allowlist entry.
    """
    pass


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


class TemplateValidationError(Exception):
    """
    Raised at approval time when request_template or headers_template
    references a placeholder not in CONTEXT_DATA_KEYS. Distinct from
    AllowlistViolationError — this is a template-authoring defect, not a
    URL policy rejection.
    """
    pass
