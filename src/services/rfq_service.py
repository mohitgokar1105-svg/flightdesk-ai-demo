"""Operator directory, requests for quote (RFQs) and ready-to-send messages (WhatsApp / email / text).

FlightDesk never sends anything itself. It writes the message, opens WhatsApp or the email app with it pre-filled, and
records that the team sent it. When a quote from that operator is recorded, the request is marked as replied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote as urlquote

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.auth import Permission, require
from src.exceptions import NotFound, ValidationFailure
from src.models import Inquiry, Operator, OperatorRequest, utcnow
from src.schemas import validate_email, validate_phone
from src.services.audit_service import record_audit

CHASE_AFTER_HOURS = 24
CHANNELS = ("whatsapp", "email", "phone", "other")
RFQ_STATUS_LABELS = {"sent": "Waiting", "replied": "Replied", "declined": "Declined", "no_response": "No response"}


# ---------------------------------------------------------------------------- links
def phone_digits(phone: str | None) -> str | None:
    """International digits for wa.me links ('+44 20 7946 0103' -> '442079460103')."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits if 7 <= len(digits) <= 15 else None


def whatsapp_link(text: str, phone: str | None = None) -> str:
    """Opens WhatsApp with the message pre-filled (to that number if known, otherwise you pick the chat)."""
    digits = phone_digits(phone)
    return f"https://wa.me/{digits or ''}?text={urlquote(text[:3500])}"


def sms_link(text: str, phone: str | None = None) -> str:
    return f"sms:{phone_digits(phone) or ''}?&body={urlquote(text[:1500])}"


def email_link(subject: str, body: str, to: str | None = None) -> str:
    return f"mailto:{to or ''}?subject={urlquote(subject)}&body={urlquote(body[:1800])}"


def to_chat_message(subject: str | None, body: str) -> str:
    """Email draft → short chat message: drop the subject line, signature block and blank-line padding."""
    lines = [ln.rstrip() for ln in body.strip().splitlines()]
    cut = next((i for i, ln in enumerate(lines) if re.match(r"^(best|kind) regards|^regards|^thanks,?$|^thank you,?$|^--\s*$",
                                                          ln.strip(), re.I)), None)
    if cut is not None and cut > 2:
        lines = lines[:cut + 1]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text


# ---------------------------------------------------------------------------- RFQ message
def _fmt_date(d: Any) -> str | None:
    return d.strftime("%a %d %b %Y") if d else None


def rfq_message(inquiry: Inquiry) -> str:
    """The standard request-for-quote an operator expects, from the inquiry record. Customer identity is never included."""
    trip = {"one_way": "One way", "round_trip": "Round trip", "multi_leg": "Multi-leg"}.get(inquiry.trip_type or "")
    dep = _fmt_date(inquiry.departure_date)
    if not dep and (inquiry.departure_earliest or inquiry.departure_latest):
        dep = f"between {_fmt_date(inquiry.departure_earliest) or '?'} and {_fmt_date(inquiry.departure_latest) or '?'}"
    lines = ["Hi, could you quote the following charter please?", ""]
    lines.append(f"✈ {inquiry.origin or 'TBC'} → {inquiry.destination or 'TBC'}" + (f" ({trip})" if trip else ""))
    lines.append(f"📅 {dep or 'Dates TBC'}" + (f", {inquiry.departure_time_preference}" if inquiry.departure_time_preference else ""))
    if inquiry.return_date:
        lines.append(f"↩ Return {_fmt_date(inquiry.return_date)}")
    lines.append(f"👥 {inquiry.passenger_count} passengers" if inquiry.passenger_count else "👥 Passengers TBC")
    if inquiry.special_requirements:
        lines.append(f"📝 {inquiry.special_requirements}")
    services = [s.replace("_", " ") for s in (inquiry.requested_services or [])]
    if services:
        lines.append(f"➕ Also asked for: {', '.join(services)}")
    lines += ["", "Please include aircraft type, seats, all-in price with any extra fees, what's included and how long "
                  "the price is valid. Thank you!", f"Ref {inquiry.id}"]
    return "\n".join(lines)


def rfq_email_subject(inquiry: Inquiry) -> str:
    return (f"Quote request: {inquiry.origin or 'TBC'} → {inquiry.destination or 'TBC'}, "
            f"{_fmt_date(inquiry.departure_date) or 'dates TBC'} ({inquiry.passenger_count or '?'} pax) · Ref {inquiry.id}")


# ---------------------------------------------------------------------------- operators
@dataclass
class OperatorInput:
    name: str
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    base_airport: str | None = None
    fleet: str | None = None
    notes: str | None = None


def list_operators(session: Session, active_only: bool = True) -> list[Operator]:
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Operator).order_by(Operator.name)
    if active_only:
        stmt = stmt.where(Operator.is_active.is_(True))
    return list(session.scalars(stmt))


def save_operator(session: Session, data: OperatorInput, operator_id: str | None = None) -> Operator:
    require(Permission.EDIT_OPERATIONS)
    name = (data.name or "").strip()
    if len(name) < 2:
        raise ValidationFailure("Please enter the operator's name.")
    try:
        email, phone = validate_email(data.email), validate_phone(data.phone)
    except ValueError as exc:
        raise ValidationFailure(str(exc)) from exc
    clash = session.scalar(select(Operator.id).where(func.lower(Operator.name) == name.lower(), Operator.id != (operator_id or "")))
    if clash:
        raise ValidationFailure(f"An operator called “{name}” already exists.")
    op = session.get(Operator, operator_id) if operator_id else Operator()
    if op is None:
        raise NotFound("That operator no longer exists.")
    op.name, op.contact_name, op.email, op.phone = name[:80], (data.contact_name or None), email, phone
    op.base_airport, op.fleet, op.notes = data.base_airport or None, data.fleet or None, data.notes or None
    if operator_id is None:
        session.add(op)
    session.flush()
    record_audit(session, "operator_saved", actor="advisor", entity_type="operator", entity_id=op.id)
    return op


def ensure_sample_operators(session: Session) -> int:
    """Add the fictional sample operators if the directory is empty (for databases seeded before operators existed)."""
    require(Permission.ADMIN_DATA)
    from src.seed import _seed_operators

    return _seed_operators(session, utcnow())


# ---------------------------------------------------------------------------- requests (RFQs)
def record_requests_sent(session: Session, inquiry_id: str, operator_ids: list[str], channel: str, message: str
                         ) -> list[OperatorRequest]:
    """Record that the team sent this request to these operators. Re-sending updates the existing record."""
    require(Permission.EDIT_OPERATIONS)
    if channel not in CHANNELS:
        raise ValidationFailure("Unknown channel.")
    if not operator_ids:
        raise ValidationFailure("Choose at least one operator.")
    if session.get(Inquiry, inquiry_id) is None:
        raise NotFound(f"Inquiry {inquiry_id} not found.")
    out = []
    for oid in dict.fromkeys(operator_ids):
        if session.get(Operator, oid) is None:
            raise NotFound("One of those operators no longer exists.")
        req = session.scalar(select(OperatorRequest).where(OperatorRequest.inquiry_id == inquiry_id,
                                                           OperatorRequest.operator_id == oid))
        if req is None:
            req = OperatorRequest(inquiry_id=inquiry_id, operator_id=oid)
            session.add(req)
        req.channel, req.message, req.sent_at = channel, message[:4000], utcnow()
        if req.status in ("no_response", "declined"):
            req.status = "sent"
        out.append(req)
    session.flush()
    record_audit(session, "quote_requests_sent", actor="advisor", entity_type="inquiry", entity_id=inquiry_id,
                 metadata={"operators": len(out), "channel": channel})
    return out


def set_request_status(session: Session, request_id: str, status: str) -> OperatorRequest:
    require(Permission.EDIT_OPERATIONS)
    if status not in RFQ_STATUS_LABELS:
        raise ValidationFailure("Unknown status.")
    req = session.get(OperatorRequest, request_id)
    if req is None:
        raise NotFound("That request no longer exists.")
    req.status = status
    req.replied_at = utcnow() if status in ("replied", "declined") else None
    record_audit(session, "quote_request_updated", actor="advisor", entity_type="inquiry", entity_id=req.inquiry_id,
                 metadata={"status": status})
    return req


def link_quote_to_request(session: Session, inquiry_id: str, operator_label: str, quote_id: str) -> OperatorRequest | None:
    """Called when a quote is recorded: if we asked that operator for this trip, mark the request as replied."""
    label = (operator_label or "").strip().lower()
    for req in session.scalars(select(OperatorRequest).where(OperatorRequest.inquiry_id == inquiry_id)
                               .options(selectinload(OperatorRequest.operator))):
        if req.operator.name.strip().lower() == label and req.status != "replied":
            req.status, req.replied_at, req.quote_id = "replied", utcnow(), quote_id
            return req
    return None


def requests_for(session: Session, inquiry_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    rows = session.scalars(select(OperatorRequest).where(OperatorRequest.inquiry_id == inquiry_id)
                           .options(selectinload(OperatorRequest.operator)).order_by(OperatorRequest.sent_at))
    out = []
    for r in rows:
        hours = (now - r.sent_at).total_seconds() / 3600
        out.append({"id": r.id, "operator_id": r.operator_id, "operator": r.operator.name, "phone": r.operator.phone,
                    "email": r.operator.email, "status": r.status, "label": RFQ_STATUS_LABELS[r.status],
                    "channel": r.channel, "sent_at": r.sent_at, "hours_waiting": hours,
                    "needs_chase": r.status == "sent" and hours >= CHASE_AFTER_HOURS, "quote_id": r.quote_id})
    return out


def chase_message(operator_name: str, inquiry: Inquiry) -> str:
    return (f"Hi {operator_name.split(' (')[0]}, just following up on our quote request for "
            f"{inquiry.origin or 'TBC'} → {inquiry.destination or 'TBC'} on {_fmt_date(inquiry.departure_date) or 'the dates we sent'} "
            f"({inquiry.passenger_count or '?'} pax), Ref {inquiry.id}. Are you able to quote? Thanks!")


def requests_to_chase(session: Session, now: datetime | None = None) -> list[OperatorRequest]:
    """Requests sent more than a day ago with no reply, on inquiries that are still open."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    cutoff = now - timedelta(hours=CHASE_AFTER_HOURS)
    stmt = (select(OperatorRequest).join(Inquiry).where(OperatorRequest.status == "sent", OperatorRequest.sent_at <= cutoff,
                                                        Inquiry.status.in_(["new", "needs_information", "qualified", "quoted"]))
            .options(selectinload(OperatorRequest.operator), selectinload(OperatorRequest.inquiry))
            .order_by(OperatorRequest.sent_at))
    return list(session.scalars(stmt))
