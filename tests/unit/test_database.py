"""Database: initialisation, versioning, constraints, relationships, transactions, duplicates, guarded reset."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from src.database import (
    SCHEMA_VERSION,
    delete_all_records,
    get_engine,
    get_schema_version,
    init_db,
    record_counts,
    session_scope,
)
from src.exceptions import ConfirmationRequired, DuplicateRecord, ValidationFailure
from src.models import Booking, Inquiry, Quote
from src.schemas import InquiryCreate, QuoteCreate
from src.services.inquiry_service import create_inquiry, update_inquiry_fields
from src.services.quote_service import create_quote


def _inq(session, label="Demo Customer DB1", text="LA to Aspen Dec 18 for 4, one way", **kw):
    data = InquiryCreate(customer_label=label, original_text=text, origin="Los Angeles", destination="Aspen",
                         trip_type="one_way", departure_date=date(2026, 12, 18), passenger_count=4, **kw)
    return create_inquiry(session, data)


def test_init_is_idempotent_and_versioned(settings):
    assert init_db(settings.database_url) == SCHEMA_VERSION
    assert get_schema_version(get_engine(settings.database_url)) == SCHEMA_VERSION


def test_create_and_update_record(session):
    inq = _inq(session)
    assert inq.id.startswith("INQ-") and inq.status == "new" and inq.created_at.tzinfo is not None
    update_inquiry_fields(session, inq.id, {"passenger_count": 6, "advisor_notes": "VIP (fictional)"})
    assert session.get(Inquiry, inq.id).passenger_count == 6


def test_update_validation_rejects_bad_values(session):
    inq = _inq(session)
    with pytest.raises(ValidationFailure):
        update_inquiry_fields(session, inq.id, {"return_date": date(2026, 12, 1), "trip_type": "round_trip"})
    with pytest.raises(ValidationFailure):
        update_inquiry_fields(session, inq.id, {"status": "booked"})


def test_relationships_and_foreign_keys(session):
    inq = _inq(session)
    q = create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label="Demo Operator A (fictional)", total_price=50000, currency="USD"))
    session.flush()
    assert q.inquiry.id == inq.id and inq.quotes[0].id == q.id
    session.add(Quote(inquiry_id="INQ-DOESNOTEXIST", operator_label="x", total_price=1, currency="USD", dedupe_key="k"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_check_constraints_enforced(session):
    session.add(Inquiry(customer_label="x", content_hash="h1", status="not_a_status"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_transaction_rollback_on_error(settings):
    with pytest.raises(RuntimeError), session_scope(settings.database_url) as s:
        _inq(s, label="Rollback")
        raise RuntimeError("boom")
    with session_scope(settings.database_url) as s:
        assert s.scalar(select(Inquiry).where(Inquiry.customer_label == "Rollback")) is None


def test_duplicate_inquiry_and_quote_prevented(session):
    inq = _inq(session)
    with pytest.raises(DuplicateRecord):
        _inq(session)
    data = QuoteCreate(inquiry_id=inq.id, operator_label="Demo Operator A (fictional)", total_price=50000, currency="USD")
    create_quote(session, data)
    with pytest.raises(DuplicateRecord):
        create_quote(session, data)


def test_partial_unique_index_on_active_bookings(session):
    inq = _inq(session)
    now = datetime.now(UTC)
    session.add(Booking(inquiry_id=inq.id, status="confirmed", booked_value=1000, currency="USD",
                        service_category="charter_flight", created_at=now, confirmed_at=now))
    session.flush()
    session.add(Booking(inquiry_id=inq.id, status="cancelled", booked_value=1000, currency="USD",
                        service_category="charter_flight", created_at=now, confirmed_at=now))
    session.flush()  # cancelled bookings don't count toward uniqueness
    session.add(Booking(inquiry_id=inq.id, status="confirmed", booked_value=2000, currency="USD",
                        service_category="charter_flight", created_at=now, confirmed_at=now))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_naive_datetimes_rejected(session):
    inq = _inq(session)
    inq.updated_at = datetime(2026, 1, 1)  # naive
    with pytest.raises(StatementError):
        session.flush()
    session.rollback()


def test_reset_requires_confirmation(session):
    _inq(session)
    with pytest.raises(ConfirmationRequired):
        delete_all_records(session, "yes")
    assert record_counts(session)["inquiries"] == 1
    delete_all_records(session, "RESET")
    assert record_counts(session)["inquiries"] == 0


def test_seed_is_idempotent_and_labelled(seeded):
    from src.seed import seed_demo_data

    counts = record_counts(seeded)
    assert counts["inquiries"] > 20 and counts["quotes"] > 20 and counts["bookings"] > 5
    assert all(i.is_sample for i in seeded.scalars(select(Inquiry)))
    assert all(i.customer_label.startswith("Demo Customer") for i in seeded.scalars(select(Inquiry)))
    assert seed_demo_data(seeded)["skipped"] is True
    assert record_counts(seeded)["inquiries"] == counts["inquiries"]
