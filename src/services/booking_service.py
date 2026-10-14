"""Fictional booking records with validated status transitions.

A booking here is a *record that the advisor obtained a confirmation outside this prototype*.
No real booking, payment or operator contact happens. Confirmation from a quote runs only through
the approved booking workflow (see :mod:`src.workflows.workflow_engine`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.domain import (
    BOOKING_TRANSITIONS,
    INQUIRY_TRANSITIONS,
    BookingStatus,
    InquiryStatus,
    QuoteStatus,
    ServiceCategory,
    ensure_transition,
)
from src.exceptions import DuplicateRecord, NotFound, PermissionDenied, ValidationFailure
from src.models import Booking, Inquiry, Quote, utcnow
from src.schemas import BookingCreate
from src.services.audit_service import record_audit

REQUIRED_FOR_BOOKING = {"origin", "destination", "departure_date", "exact_departure_date", "passenger_count"}


@dataclass
class BookingCheck:
    """Result of pre-confirmation validation (used by the workflow's VALIDATING step)."""

    ok: bool
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    booked_value: float | None = None
    currency: str | None = None


def check_booking_from_quote(session: Session, quote_id: str) -> BookingCheck:
    require(Permission.VIEW_OPERATIONS)
    quote = session.get(Quote, quote_id)
    if quote is None:
        return BookingCheck(ok=False, blocking=[f"Quote {quote_id} not found."])
    inquiry = quote.inquiry
    check = BookingCheck(ok=True, booked_value=quote.all_in_price, currency=quote.currency)
    if QuoteStatus(quote.status) not in (QuoteStatus.RECEIVED, QuoteStatus.PRESENTED):
        check.blocking.append(f"Quote status is '{quote.status}'; only received or presented quotes can be booked.")
    status = InquiryStatus(inquiry.status)
    if status == InquiryStatus.NEEDS_INFORMATION:
        check.blocking.append("Inquiry is awaiting customer information; resolve missing information first.")
    elif status not in (InquiryStatus.NEW, InquiryStatus.QUALIFIED, InquiryStatus.QUOTED):
        check.blocking.append(f"Inquiry status is '{inquiry.status}'; it cannot be booked.")
    missing = {m["field"] for m in (inquiry.missing_information or [])} & REQUIRED_FOR_BOOKING
    if missing:
        check.blocking.append(f"Inquiry is missing required details: {', '.join(sorted(missing))}.")
    if quote.passenger_capacity is not None and inquiry.passenger_count is not None \
            and quote.passenger_capacity < inquiry.passenger_count:
        check.blocking.append(f"Quote capacity {quote.passenger_capacity} is below {inquiry.passenger_count} passengers.")
    if quote.passenger_capacity is None:
        check.warnings.append("Quote capacity is not documented.")
    if inquiry.budget_amount and inquiry.currency == quote.currency and quote.all_in_price > inquiry.budget_amount:
        check.warnings.append("Quote exceeds the customer's stated budget.")
    active = session.scalar(select(Booking.id).where(Booking.inquiry_id == inquiry.id,
                                                     Booking.service_category == ServiceCategory.CHARTER_FLIGHT.value,
                                                     Booking.status != BookingStatus.CANCELLED.value))
    if active:
        check.blocking.append(f"Inquiry already has an active charter booking ({active}).")
    check.ok = not check.blocking
    return check


def _hop_inquiry(session: Session, inquiry: Inquiry, targets: list[InquiryStatus], actor: str, reason: str) -> None:
    for target in targets:
        current = InquiryStatus(inquiry.status)
        if current == target:
            continue
        ensure_transition(INQUIRY_TRANSITIONS, current, target, f"Inquiry {inquiry.id}")
        inquiry.status = target.value
        inquiry.updated_at = utcnow()
        record_audit(session, "inquiry_status_changed", actor=actor, entity_type="inquiry", entity_id=inquiry.id,
                     metadata={"from": current.value, "to": target.value, "reason": reason})


def confirm_booking_from_quote(session: Session, quote_id: str, *, actor: str = "advisor", notes: str | None = None,
                               approved_via_workflow: str | None = None) -> Booking:
    """Record a confirmed fictional booking for a quote. Requires an approving workflow run ID."""
    require(Permission.MANAGE_BOOKINGS)
    if actor == "model":
        raise PermissionDenied("The model cannot confirm bookings.")
    if not approved_via_workflow:
        raise PermissionDenied("Booking confirmation must go through the approved booking workflow.")
    check = check_booking_from_quote(session, quote_id)
    if not check.ok:
        raise ValidationFailure(" ".join(check.blocking))
    quote = session.get(Quote, quote_id)
    assert quote is not None
    inquiry = quote.inquiry
    if QuoteStatus(quote.status) == QuoteStatus.RECEIVED:
        quote.status = QuoteStatus.PRESENTED.value
        quote.presented_at = utcnow()
    quote.status = QuoteStatus.ACCEPTED.value
    quote.updated_at = utcnow()
    record_audit(session, "quote_status_changed", actor=actor, entity_type="quote", entity_id=quote.id,
                 metadata={"to": "accepted", "workflow_run_id": approved_via_workflow})
    _hop_inquiry(session, inquiry, [InquiryStatus.QUALIFIED, InquiryStatus.QUOTED, InquiryStatus.BOOKED]
                 if inquiry.status == InquiryStatus.NEW.value else [InquiryStatus.QUOTED, InquiryStatus.BOOKED],
                 actor, f"booking confirmed via {approved_via_workflow}")
    now = utcnow()
    booking = Booking(inquiry_id=inquiry.id, quote_id=quote.id, status=BookingStatus.CONFIRMED.value, created_at=now,
                      confirmed_at=now, booked_value=quote.all_in_price, currency=quote.currency,
                      service_category=ServiceCategory.CHARTER_FLIGHT.value, notes=notes)
    session.add(booking)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateRecord("An active booking already exists for this quote or inquiry.") from exc
    record_audit(session, "booking_confirmed", actor=actor, entity_type="booking", entity_id=booking.id,
                 metadata={"inquiry_id": inquiry.id, "quote_id": quote.id, "currency": booking.currency,
                           "workflow_run_id": approved_via_workflow})
    return booking


def create_booking_record(session: Session, data: BookingCreate, *, status: BookingStatus = BookingStatus.CONFIRMED,
                          actor: str = "advisor", is_sample: bool = False, created_at: Any = None,
                          confirmed_at: Any = None) -> Booking:
    """Direct record creation used for seed data / imports of ancillary services (not quote-based charters)."""
    require(Permission.MANAGE_BOOKINGS)
    if actor == "model":
        raise PermissionDenied("The model cannot create bookings.")
    inquiry = session.get(Inquiry, data.inquiry_id)
    if inquiry is None:
        raise NotFound(f"Inquiry {data.inquiry_id} not found.")
    now = utcnow()
    booking = Booking(inquiry_id=data.inquiry_id, quote_id=data.quote_id, status=status.value,
                      created_at=created_at or now, confirmed_at=confirmed_at or created_at or now,
                      booked_value=data.booked_value, currency=data.currency, service_category=data.service_category.value,
                      notes=data.notes, is_sample=is_sample)
    if status == BookingStatus.COMPLETED:
        booking.completed_at = booking.confirmed_at
    if status == BookingStatus.CANCELLED:
        booking.cancelled_at = booking.confirmed_at
    session.add(booking)
    try:
        session.flush()
    except IntegrityError as exc:
        raise DuplicateRecord("An active booking already exists for this quote or inquiry/service category.") from exc
    record_audit(session, "booking_recorded", actor=actor, entity_type="booking", entity_id=booking.id,
                 metadata={"status": status.value, "category": booking.service_category})
    return booking


def _get(session: Session, booking_id: str) -> Booking:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise NotFound(f"Booking {booking_id} not found.")
    return booking


def cancel_booking(session: Session, booking_id: str, *, reason: str, actor: str = "advisor") -> Booking:
    require(Permission.MANAGE_BOOKINGS)
    if actor == "model":
        raise PermissionDenied("The model cannot cancel bookings.")
    if not reason or not reason.strip():
        raise ValidationFailure("A cancellation reason is required.")
    booking = _get(session, booking_id)
    ensure_transition(BOOKING_TRANSITIONS, BookingStatus(booking.status), BookingStatus.CANCELLED, f"Booking {booking_id}")
    booking.status = BookingStatus.CANCELLED.value
    booking.cancelled_at = utcnow()
    booking.notes = ((booking.notes or "") + f"\nCancelled: {reason.strip()}").strip()
    if booking.quote is not None and booking.quote.status == QuoteStatus.ACCEPTED.value:
        booking.quote.status = QuoteStatus.PRESENTED.value
        record_audit(session, "quote_status_changed", actor=actor, entity_type="quote", entity_id=booking.quote.id,
                     metadata={"from": "accepted", "to": "presented", "reason": "booking cancelled"})
    inquiry = booking.inquiry
    still_active = any(b.status != BookingStatus.CANCELLED.value and b.id != booking.id for b in inquiry.bookings)
    if inquiry.status == InquiryStatus.BOOKED.value and not still_active:
        _hop_inquiry(session, inquiry, [InquiryStatus.QUOTED], actor, "booking cancelled")
    session.flush()
    record_audit(session, "booking_cancelled", actor=actor, entity_type="booking", entity_id=booking_id,
                 metadata={"reason": reason.strip()})
    return booking


def complete_booking(session: Session, booking_id: str, *, actor: str = "advisor") -> Booking:
    require(Permission.MANAGE_BOOKINGS)
    if actor == "model":
        raise PermissionDenied("The model cannot change bookings.")
    booking = _get(session, booking_id)
    ensure_transition(BOOKING_TRANSITIONS, BookingStatus(booking.status), BookingStatus.COMPLETED, f"Booking {booking_id}")
    booking.status = BookingStatus.COMPLETED.value
    booking.completed_at = utcnow()
    inquiry = booking.inquiry
    others_open = any(b.status == BookingStatus.CONFIRMED.value and b.id != booking.id for b in inquiry.bookings)
    if inquiry.status == InquiryStatus.BOOKED.value and not others_open:
        _hop_inquiry(session, inquiry, [InquiryStatus.COMPLETED], actor, "all bookings completed")
    session.flush()
    record_audit(session, "booking_completed", actor=actor, entity_type="booking", entity_id=booking_id)
    return booking


def list_bookings(session: Session, statuses: list[str] | None = None) -> list[Booking]:
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Booking).order_by(Booking.created_at.desc())
    if statuses:
        stmt = stmt.where(Booking.status.in_(statuses))
    return list(session.scalars(stmt))


def booking_to_dict(b: Booking) -> dict[str, Any]:
    return {
        "booking_id": b.id, "inquiry_id": b.inquiry_id, "quote_id": b.quote_id, "status": b.status,
        "created_at": b.created_at.isoformat(), "confirmed_at": b.confirmed_at.isoformat() if b.confirmed_at else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None, "booked_value": b.booked_value,
        "currency": b.currency, "service_category": b.service_category, "notes": b.notes, "is_sample": b.is_sample,
    }
