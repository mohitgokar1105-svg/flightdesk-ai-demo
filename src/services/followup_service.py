"""Follow-up drafts and tracking. This prototype never sends email: 'completed' means the advisor
recorded that the follow-up happened outside the application."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.domain import (
    FOLLOWUP_CLOSED_STATUSES,
    FOLLOWUP_EDITABLE_STATUSES,
    FOLLOWUP_TRANSITIONS,
    BookingStatus,
    FollowUpStatus,
    FollowUpType,
    ensure_transition,
)
from src.exceptions import DuplicateRecord, NotFound, PermissionDenied, ValidationFailure
from src.models import FollowUp, utcnow
from src.schemas import FollowUpCreate
from src.services.audit_service import record_audit
from src.services.inquiry_service import get_inquiry, inquiry_to_dict
from src.services.quote_service import list_quotes_for_inquiry, quote_to_dict

COMPLETION_NOTE = "Recorded as completed by the advisor. No email is sent by this application."


def build_followup_facts(session: Session, inquiry_id: str) -> dict[str, Any]:
    """Verified facts for drafting, taken only from stored records."""
    require(Permission.VIEW_OPERATIONS)
    inquiry = get_inquiry(session, inquiry_id)
    inq = inquiry_to_dict(inquiry)
    inq.pop("advisor_notes", None)
    inq["missing_information_questions"] = list(inquiry.missing_information or [])
    quotes = [
        {k: v for k, v in quote_to_dict(q).items() if k in (
            "operator_label", "total_price", "additional_fees", "all_in_price", "currency", "aircraft_type",
            "passenger_capacity", "status",
            "included_services", "excluded_services", "restrictions", "valid_until")}
        for q in list_quotes_for_inquiry(session, inquiry_id) if q.status in ("received", "presented", "accepted")
    ]
    recorded = [f for f in inquiry.followups if f.status == FollowUpStatus.COMPLETED.value]
    return {
        "inquiry": inq,
        "quotes": quotes,
        "bookings_confirmed": [b.id for b in inquiry.bookings if b.status == BookingStatus.CONFIRMED.value],
        "bookings_completed": [b.id for b in inquiry.bookings if b.status == BookingStatus.COMPLETED.value],
        "recorded_interactions": [{"type": f.followup_type, "completed_at": f.completed_at.isoformat() if f.completed_at else None}
                                  for f in recorded],
        "system_note": "This application has not sent any email. Quotes are fictional demonstration records.",
    }


def _body_hash(subject: str, body: str) -> str:
    return hashlib.sha256((subject.strip() + "\n" + body.strip()).encode()).hexdigest()


def create_followup(session: Session, data: FollowUpCreate, *, actor: str = "advisor", is_sample: bool = False,
                    status: FollowUpStatus = FollowUpStatus.DRAFT, created_at: datetime | None = None) -> FollowUp:
    require(Permission.EDIT_OPERATIONS)
    get_inquiry(session, data.inquiry_id)
    if actor == "model" and status != FollowUpStatus.DRAFT:
        raise PermissionDenied("Model-initiated follow-ups can only be saved as drafts.")
    digest = _body_hash(data.subject, data.body)
    for existing in session.scalars(select(FollowUp).where(FollowUp.inquiry_id == data.inquiry_id,
                                                             FollowUp.followup_type == data.followup_type.value,
                                                             FollowUp.status != FollowUpStatus.CANCELLED.value)):
        if _body_hash(existing.subject, existing.body) == digest:
            raise DuplicateRecord(f"An identical follow-up already exists ({existing.id}).")
    metadata = dict(data.generation_metadata)
    metadata["content_sha256"] = digest
    followup = FollowUp(inquiry_id=data.inquiry_id, followup_type=data.followup_type.value, tone=data.tone.value,
                        subject=data.subject.strip(), body=data.body.strip(), due_at=data.due_at, status=status.value,
                        advisor_notes=data.advisor_notes, generation_method=data.generation_method,
                        generation_metadata=metadata, is_sample=is_sample)
    if created_at is not None:
        followup.created_at = created_at
        followup.updated_at = created_at
    session.add(followup)
    session.flush()
    record_audit(session, "followup_created", actor=actor, entity_type="followup", entity_id=followup.id,
                 metadata={"inquiry_id": data.inquiry_id, "type": data.followup_type.value, "method": data.generation_method})
    return followup


def get_followup(session: Session, followup_id: str) -> FollowUp:
    require(Permission.VIEW_OPERATIONS)
    f = session.get(FollowUp, followup_id)
    if f is None:
        raise NotFound(f"Follow-up {followup_id} not found.")
    return f


def update_followup(session: Session, followup_id: str, *, subject: str | None = None, body: str | None = None,
                    due_at: datetime | None = None, advisor_notes: str | None = None, actor: str = "advisor") -> FollowUp:
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model cannot edit saved follow-ups.")
    f = get_followup(session, followup_id)
    content_change = (subject is not None and subject != f.subject) or (body is not None and body != f.body)
    if content_change and FollowUpStatus(f.status) not in FOLLOWUP_EDITABLE_STATUSES:
        raise ValidationFailure(f"Follow-up is '{f.status}'; its content can no longer be edited.")
    if subject is not None:
        if not subject.strip():
            raise ValidationFailure("Subject cannot be empty.")
        f.subject = subject.strip()[:200]
    if body is not None:
        if not body.strip():
            raise ValidationFailure("Body cannot be empty.")
        f.body = body.strip()[:8000]
    if due_at is not None:
        if due_at.tzinfo is None:
            raise ValidationFailure("Due date must be timezone-aware.")
        f.due_at = due_at
    if advisor_notes is not None:
        f.advisor_notes = advisor_notes.strip()[:4000] or None
    if content_change:
        meta = dict(f.generation_metadata or {})
        meta["edited_by_advisor"] = True
        meta["content_sha256"] = _body_hash(f.subject, f.body)
        f.generation_metadata = meta
    f.updated_at = utcnow()
    session.flush()
    record_audit(session, "followup_updated", actor=actor, entity_type="followup", entity_id=f.id,
                 metadata={"content_changed": content_change})
    return f


def change_followup_status(session: Session, followup_id: str, target: FollowUpStatus | str, *, actor: str = "advisor",
                           notes: str | None = None) -> FollowUp:
    require(Permission.EDIT_OPERATIONS)
    target = FollowUpStatus(target)
    if actor == "model":
        raise PermissionDenied("The model cannot change follow-up status.")
    f = get_followup(session, followup_id)
    current = FollowUpStatus(f.status)
    ensure_transition(FOLLOWUP_TRANSITIONS, current, target, f"Follow-up {followup_id}")
    if target == FollowUpStatus.SCHEDULED and f.due_at is None:
        raise ValidationFailure("Set a due date before scheduling a follow-up.")
    f.status = target.value
    if target == FollowUpStatus.COMPLETED:
        f.completed_at = utcnow()
        f.advisor_notes = "\n".join(x for x in [f.advisor_notes, notes, COMPLETION_NOTE] if x)
    elif notes:
        f.advisor_notes = "\n".join(x for x in [f.advisor_notes, notes] if x)
    f.updated_at = utcnow()
    session.flush()
    record_audit(session, "followup_status_changed", actor=actor, entity_type="followup", entity_id=f.id,
                 metadata={"from": current.value, "to": target.value})
    return f


def list_followups(session: Session, inquiry_id: str | None = None, statuses: list[str] | None = None) -> list[FollowUp]:
    require(Permission.VIEW_OPERATIONS)
    stmt = select(FollowUp).order_by(FollowUp.due_at.is_(None), FollowUp.due_at, FollowUp.created_at.desc())
    if inquiry_id:
        stmt = stmt.where(FollowUp.inquiry_id == inquiry_id)
    if statuses:
        stmt = stmt.where(FollowUp.status.in_(statuses))
    return list(session.scalars(stmt))


def is_overdue(f: FollowUp, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return f.due_at is not None and f.due_at < now and FollowUpStatus(f.status) not in FOLLOWUP_CLOSED_STATUSES


def list_due_followups(session: Session, within_days: int = 7, now: datetime | None = None) -> list[FollowUp]:
    """Open follow-ups that are overdue or due within ``within_days``."""
    require(Permission.VIEW_OPERATIONS)
    from datetime import timedelta

    now = now or utcnow()
    horizon = now + timedelta(days=max(0, min(within_days, 90)))
    open_statuses = [s.value for s in FollowUpStatus if s not in FOLLOWUP_CLOSED_STATUSES]
    stmt = (select(FollowUp).where(FollowUp.status.in_(open_statuses), FollowUp.due_at.is_not(None),
                                   FollowUp.due_at <= horizon).order_by(FollowUp.due_at))
    return list(session.scalars(stmt))


def followup_to_dict(f: FollowUp, now: datetime | None = None) -> dict[str, Any]:
    return {
        "followup_id": f.id, "inquiry_id": f.inquiry_id, "followup_type": f.followup_type, "tone": f.tone,
        "status": f.status, "subject": f.subject, "body": f.body,
        "due_at": f.due_at.isoformat() if f.due_at else None, "overdue": is_overdue(f, now),
        "created_at": f.created_at.isoformat(), "completed_at": f.completed_at.isoformat() if f.completed_at else None,
        "generation_method": f.generation_method, "advisor_notes": f.advisor_notes, "is_sample": f.is_sample,
    }


def followup_type_from(value: str) -> FollowUpType:
    try:
        return FollowUpType(value)
    except ValueError as exc:
        raise ValidationFailure(f"Unknown follow-up type '{value}'.") from exc
