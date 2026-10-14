"""Domain exceptions. Each carries a short, user-safe message (never secrets or raw payloads)."""

from __future__ import annotations


class FlightDeskError(Exception):
    """Base class for expected, user-presentable application errors."""

    category = "application_error"


class ValidationFailure(FlightDeskError):
    """Input or model output failed deterministic validation. Never retried."""

    category = "validation_error"


class InvalidTransition(FlightDeskError):
    """A status/state change is not permitted by the transition table."""

    category = "invalid_transition"


class DuplicateRecord(FlightDeskError):
    """The record would duplicate an existing one."""

    category = "duplicate_record"


class NotFound(FlightDeskError):
    category = "not_found"


class PermissionDenied(FlightDeskError):
    """An actor (usually the model) attempted an operation it is not permitted to perform."""

    category = "permission_denied"


class ApprovalRequired(FlightDeskError):
    category = "approval_required"


class ConfirmationRequired(FlightDeskError):
    """A destructive operation was requested without the explicit confirmation token."""

    category = "confirmation_required"


class AIServiceError(FlightDeskError):
    """Base class for AI provider failures."""

    category = "ai_error"
    transient = False


class AINotConfigured(AIServiceError):
    category = "ai_not_configured"


class AITransientError(AIServiceError):
    """Timeouts, connection errors, rate limits, 5xx — eligible for bounded retry."""

    category = "ai_transient"
    transient = True


class AIResponseInvalid(AIServiceError):
    """The provider answered but the output failed schema validation. Not retried as a network error."""

    category = "ai_invalid_output"


class AIRequestRejected(AIServiceError):
    """Non-transient provider error (authentication, bad request, permission)."""

    category = "ai_request_rejected"
