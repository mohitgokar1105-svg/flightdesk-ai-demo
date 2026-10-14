"""Audit events for important state changes. Metadata must be safe: no secrets, no full customer text."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.models import AuditEvent
from src.security import redact_secrets

_MAX_META_STR = 300


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)[:_MAX_META_STR]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in list(value.items())[:30]}
    if isinstance(value, list | tuple):
        return [_safe(v) for v in list(value)[:30]]
    return value


def record_audit(
    session: Session,
    event_type: str,
    *,
    actor: str = "advisor",
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    outcome: str = "success",
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        actor_type=actor,
        entity_type=entity_type,
        entity_id=entity_id,
        event_metadata=_safe(metadata or {}),
        outcome=outcome,
    )
    session.add(event)
    session.flush()
    return event


def recent_audit_events(session: Session, limit: int = 50, outcome: str | None = None) -> list[AuditEvent]:
    require(Permission.VIEW_DIAGNOSTICS)
    stmt = select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(limit)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    return list(session.scalars(stmt))
