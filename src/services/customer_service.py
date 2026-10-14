"""Customers: find-or-create from inquiries, edits, and per-customer summaries."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth import Permission, has_permission, require
from src.domain import OPEN_INQUIRY_STATUSES, VALUE_COUNTED_BOOKING_STATUSES
from src.exceptions import DuplicateRecord, NotFound, PermissionDenied, ValidationFailure
from src.models import Customer, Inquiry, utcnow
from src.schemas import CustomerCreate
from src.services.audit_service import record_audit


def find_or_create_customer(session: Session, data: CustomerCreate, *, actor: str = "advisor", is_sample: bool = False) -> Customer:
    """Match by email (case-insensitive), then by exact name when no conflicting email; otherwise create."""
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model cannot create customers directly.")
    if data.email:
        existing = session.scalar(select(Customer).where(func.lower(Customer.email) == data.email.lower()))
        if existing:
            if data.phone and not existing.phone:
                existing.phone = data.phone
            return existing
    by_name = session.scalar(select(Customer).where(func.lower(Customer.name) == data.name.lower()).limit(1))
    if by_name and (not data.email or not by_name.email):
        if data.email and not by_name.email:
            by_name.email = data.email
        if data.phone and not by_name.phone:
            by_name.phone = data.phone
        return by_name
    customer = Customer(name=data.name, email=data.email, phone=data.phone, company=data.company, notes=data.notes,
                        is_sample=is_sample)
    session.add(customer)
    session.flush()
    record_audit(session, "customer_created", actor=actor, entity_type="customer", entity_id=customer.id,
                 metadata={"has_email": bool(data.email), "has_phone": bool(data.phone)})
    return customer


def get_customer(session: Session, customer_id: str) -> Customer:
    require(Permission.VIEW_OPERATIONS)
    c = session.get(Customer, customer_id)
    if c is None:
        raise NotFound(f"Customer {customer_id} not found.")
    return c


def update_customer(session: Session, customer_id: str, data: CustomerCreate, *, actor: str = "advisor") -> Customer:
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model cannot edit customers.")
    c = get_customer(session, customer_id)
    if data.email:
        clash = session.scalar(select(Customer.id).where(func.lower(Customer.email) == data.email.lower(), Customer.id != c.id))
        if clash:
            raise DuplicateRecord(f"Another customer ({clash}) already uses that email.")
    c.name, c.email, c.phone, c.company, c.notes = data.name, data.email, data.phone, data.company, data.notes
    c.updated_at = utcnow()
    for inq in c.inquiries:
        inq.customer_label = data.name[:80]
    session.flush()
    record_audit(session, "customer_updated", actor=actor, entity_type="customer", entity_id=c.id)
    return c


def link_inquiry(session: Session, inquiry: Inquiry, customer: Customer) -> None:
    inquiry.customer_id = customer.id
    inquiry.customer_label = customer.name[:80]


def customer_summaries(session: Session, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    """One row per customer with inquiry counts and booked value per currency (never summed across currencies)."""
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Customer).order_by(Customer.name).limit(max(1, min(limit, 2000)))
    if query.strip():
        like = f"%{query.strip()[:80].lower()}%"
        stmt = stmt.where(func.lower(Customer.name).like(like) | func.lower(func.coalesce(Customer.email, "")).like(like)
                          | func.lower(func.coalesce(Customer.company, "")).like(like))
    rows = []
    open_statuses = {s.value for s in OPEN_INQUIRY_STATUSES}
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    show_money = has_permission(Permission.VIEW_FINANCIALS)
    for c in session.scalars(stmt):
        booked: dict[str, float] = defaultdict(float)
        last = c.created_at
        for inq in c.inquiries:
            last = max(last, inq.updated_at)
            for b in inq.bookings:
                if show_money and b.status in value_statuses:
                    booked[b.currency] += b.booked_value
        rows.append({
            "customer_id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "company": c.company,
            "inquiries": len(c.inquiries), "open_inquiries": sum(1 for i in c.inquiries if i.status in open_statuses),
            "booked_value": dict(sorted(booked.items())), "last_activity": last, "is_sample": c.is_sample,
        })
    return rows


def require_contact_or_reason(data: CustomerCreate) -> None:
    if not (data.email or data.phone):
        raise ValidationFailure("Add an email or phone number so the customer can be contacted.")
