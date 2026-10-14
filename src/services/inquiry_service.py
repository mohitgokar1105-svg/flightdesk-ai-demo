"""Inquiry persistence, status transitions and search."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.ai.locations import is_ambiguous
from src.ai.validation import compute_missing_information
from src.auth import Permission, require
from src.domain import (
    BOOKING_CONTROLLED_INQUIRY_STATUSES,
    INQUIRY_TRANSITIONS,
    InquiryStatus,
    ensure_transition,
)
from src.exceptions import DuplicateRecord, NotFound, PermissionDenied, ValidationFailure
from src.models import Customer, Inquiry, utcnow
from src.schemas import DateSpec, InquiryCreate, InquiryExtraction, ValidationIssue
from src.services.audit_service import record_audit

INQUIRY_FIELDS = [
    "origin", "destination", "trip_type", "departure_date", "departure_earliest", "departure_latest", "return_date",
    "return_earliest", "return_latest", "departure_time_preference", "special_requirements", "passenger_count", "trip_purpose", "date_flexibility", "budget_amount",
    "currency", "requested_services", "estimated_opportunity_value", "advisor_notes",
]


def compute_content_hash(customer_label: str, original_text: str, data: InquiryCreate | None = None) -> str:
    """Duplicate-detection key: same customer + same inquiry text (or same structured trip if no text)."""
    norm_text = " ".join((original_text or "").lower().split())
    if not norm_text and data is not None:
        norm_text = "|".join(str(getattr(data, f)) for f in ("origin", "destination", "departure_date",
                                                              "departure_earliest", "passenger_count"))
    raw = f"{customer_label.strip().lower()}::{norm_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def missing_information_for(data: InquiryCreate) -> list[dict[str, str]]:
    """Recompute required missing information from advisor-approved fields (deterministic)."""
    ambiguities = [
        ValidationIssue(code="AMBIGUOUS_LOCATION", severity="warning", field=f, message="ambiguous")
        for f in ("origin", "destination") if is_ambiguous(getattr(data, f))
    ]
    ex = InquiryExtraction(
        origin=data.origin, destination=data.destination, trip_type=data.trip_type,
        departure=DateSpec(exact=data.departure_date, earliest=data.departure_earliest, latest=data.departure_latest),
        return_date=DateSpec(exact=data.return_date, earliest=data.return_earliest, latest=data.return_latest),
        passenger_count=data.passenger_count, budget_amount=data.budget_amount, currency=data.currency,
        ambiguities=ambiguities,
    )
    return [m.model_dump() for m in compute_missing_information(ex)]


def create_inquiry(
    session: Session,
    data: InquiryCreate,
    *,
    extraction_metadata: dict[str, Any] | None = None,
    source: str = "intake",
    actor: str = "advisor",
    is_sample: bool = False,
    status: InquiryStatus | None = None,
    customer: Customer | None = None,
) -> Inquiry:
    """Persist an advisor-approved inquiry. Rejects duplicates (same customer + same text).
    ``customer`` links a specific customer record instead of matching by email/name (used by the customer portal)."""
    require(Permission.EDIT_OPERATIONS)
    content_hash = compute_content_hash(data.customer_label, data.original_text, data)
    if session.scalar(select(Inquiry.id).where(Inquiry.content_hash == content_hash)):
        raise DuplicateRecord("An identical inquiry for this customer already exists.")
    missing = missing_information_for(data)
    if status is None:
        status = InquiryStatus.NEEDS_INFORMATION if missing else InquiryStatus.NEW
    inquiry = Inquiry(
        customer_label=data.customer_label,
        original_text=data.original_text,
        **{f: getattr(data, f) for f in INQUIRY_FIELDS},
        missing_information=missing,
        status=status.value,
        extraction_metadata=extraction_metadata or {},
        content_hash=content_hash,
        source=source,
        is_sample=is_sample,
    )
    session.add(inquiry)
    session.flush()
    from src.schemas import CustomerCreate
    from src.services.customer_service import find_or_create_customer, link_inquiry

    if customer is None:
        customer = find_or_create_customer(
            session, CustomerCreate(name=data.customer_label, email=data.customer_email, phone=data.customer_phone),
            actor="system" if actor == "system" else "advisor", is_sample=is_sample)
    link_inquiry(session, inquiry, customer)
    session.flush()
    record_audit(session, "inquiry_created", actor=actor, entity_type="inquiry", entity_id=inquiry.id,
                 metadata={"source": source, "status": inquiry.status, "missing_fields": [m["field"] for m in missing]})
    return inquiry


def get_inquiry(session: Session, inquiry_id: str) -> Inquiry:
    require(Permission.VIEW_OPERATIONS)
    inquiry = session.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise NotFound(f"Inquiry {inquiry_id} not found.")
    return inquiry


def inquiry_as_create(inquiry: Inquiry) -> InquiryCreate:
    c = inquiry.customer
    return InquiryCreate(customer_label=inquiry.customer_label, original_text=inquiry.original_text or "",
                         customer_email=c.email if c else None, customer_phone=c.phone if c else None,
                         **{f: getattr(inquiry, f) for f in INQUIRY_FIELDS})


def update_inquiry_fields(session: Session, inquiry_id: str, changes: dict[str, Any], actor: str = "advisor") -> Inquiry:
    """Validate and apply field edits (not status). Missing information is recomputed."""
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model may not edit inquiry records directly.")
    unknown = set(changes) - set(INQUIRY_FIELDS) - {"customer_label"}
    if unknown:
        raise ValidationFailure(f"Fields cannot be edited: {', '.join(sorted(unknown))}")
    inquiry = get_inquiry(session, inquiry_id)
    merged = inquiry_as_create(inquiry).model_dump()
    merged.update(changes)
    try:
        validated = InquiryCreate.model_validate(merged)
    except Exception as exc:
        raise ValidationFailure(str(exc)) from exc
    changed = [f for f in changes if getattr(inquiry, f) != getattr(validated, f)]
    for f in INQUIRY_FIELDS + ["customer_label"]:
        setattr(inquiry, f, getattr(validated, f))
    inquiry.missing_information = missing_information_for(validated)
    inquiry.updated_at = utcnow()
    session.flush()
    if changed:
        record_audit(session, "inquiry_updated", actor=actor, entity_type="inquiry", entity_id=inquiry.id,
                     metadata={"fields": changed})
    return inquiry


def change_inquiry_status(
    session: Session,
    inquiry_id: str,
    target: InquiryStatus | str,
    *,
    actor: str = "advisor",
    reason: str | None = None,
    via_booking_service: bool = False,
) -> Inquiry:
    """Validated status transition. Booking-controlled statuses can only be set by the booking service."""
    require(Permission.EDIT_OPERATIONS)
    target = InquiryStatus(target)
    if actor == "model":
        raise PermissionDenied("The model cannot change inquiry status directly; it may only propose changes.")
    if target in BOOKING_CONTROLLED_INQUIRY_STATUSES and not via_booking_service:
        raise PermissionDenied(f"Status '{target}' is set only by the booking workflow.")
    inquiry = get_inquiry(session, inquiry_id)
    current = InquiryStatus(inquiry.status)
    if current == InquiryStatus.BOOKED and target == InquiryStatus.QUOTED and not via_booking_service:
        raise PermissionDenied("A booked inquiry returns to 'quoted' only when its booking is cancelled.")
    ensure_transition(INQUIRY_TRANSITIONS, current, target, f"Inquiry {inquiry_id}")
    inquiry.status = target.value
    inquiry.updated_at = utcnow()
    session.flush()
    record_audit(session, "inquiry_status_changed", actor=actor, entity_type="inquiry", entity_id=inquiry_id,
                 metadata={"from": current.value, "to": target.value, "reason": reason or ""})
    return inquiry


def list_inquiries(session: Session, statuses: list[str] | None = None, limit: int = 500) -> list[Inquiry]:
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Inquiry).order_by(Inquiry.created_at.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(Inquiry.status.in_(statuses))
    return list(session.scalars(stmt))


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_inquiries(session: Session, query: str = "", status: str | None = None, limit: int = 20, *,
                     received_from: date | None = None, received_to: date | None = None) -> list[Inquiry]:
    """Parameterised search over ID, customer label and route. Every word must match one of those fields
    ("Avery Aspen" finds Avery's Aspen trip). Date filters run in the database, before the limit. No raw SQL."""
    require(Permission.VIEW_OPERATIONS)
    limit = max(1, min(int(limit), 1000))
    stmt = select(Inquiry).order_by(Inquiry.created_at.desc()).limit(limit)
    for word in query.strip()[:80].split()[:6]:
        like = f"%{_escape_like(word)}%"
        stmt = stmt.where(or_(Inquiry.id.ilike(like, escape="\\"), Inquiry.customer_label.ilike(like, escape="\\"),
                              Inquiry.origin.ilike(like, escape="\\"), Inquiry.destination.ilike(like, escape="\\")))
    if status:
        stmt = stmt.where(Inquiry.status == InquiryStatus(status).value)
    if received_from:
        stmt = stmt.where(Inquiry.created_at >= datetime.combine(received_from, time.min, tzinfo=UTC))
    if received_to:
        stmt = stmt.where(Inquiry.created_at <= datetime.combine(received_to, time.max, tzinfo=UTC))
    return list(session.scalars(stmt))


def _d(v: date | None) -> str | None:
    return v.isoformat() if v else None


def inquiry_to_dict(inquiry: Inquiry, include_text: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "inquiry_id": inquiry.id,
        "created_at": inquiry.created_at.isoformat(),
        "updated_at": inquiry.updated_at.isoformat(),
        "customer_label": inquiry.customer_label,
        "customer_id": inquiry.customer_id,
        "customer_email": inquiry.customer.email if inquiry.customer else None,
        "customer_phone": inquiry.customer.phone if inquiry.customer else None,
        "status": inquiry.status,
        "origin": inquiry.origin,
        "destination": inquiry.destination,
        "trip_type": inquiry.trip_type,
        "departure_date": _d(inquiry.departure_date),
        "departure_earliest": _d(inquiry.departure_earliest),
        "departure_latest": _d(inquiry.departure_latest),
        "return_date": _d(inquiry.return_date),
        "return_earliest": _d(inquiry.return_earliest),
        "return_latest": _d(inquiry.return_latest),
        "departure_time_preference": inquiry.departure_time_preference,
        "special_requirements": inquiry.special_requirements,
        "passenger_count": inquiry.passenger_count,
        "trip_purpose": inquiry.trip_purpose,
        "date_flexibility": inquiry.date_flexibility,
        "budget_amount": inquiry.budget_amount,
        "currency": inquiry.currency,
        "requested_services": list(inquiry.requested_services or []),
        "missing_information": [m["field"] for m in (inquiry.missing_information or [])],
        "estimated_opportunity_value": inquiry.estimated_opportunity_value,
        "advisor_notes": inquiry.advisor_notes,
        "source": inquiry.source,
        "is_sample": inquiry.is_sample,
    }
    if include_text:
        out["original_text"] = inquiry.original_text
    return out
