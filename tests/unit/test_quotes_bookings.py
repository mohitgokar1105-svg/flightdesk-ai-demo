"""Quote comparison and booking rules."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.exceptions import InvalidTransition, PermissionDenied, ValidationFailure
from src.models import Inquiry, Quote
from src.schemas import BookingCreate, InquiryCreate, QuoteCreate
from src.services.booking_service import (
    cancel_booking,
    check_booking_from_quote,
    complete_booking,
    confirm_booking_from_quote,
    create_booking_record,
)
from src.services.inquiry_service import create_inquiry
from src.services.quote_service import change_quote_status, compare_quotes, create_quote


def _inq(session, pax=6, budget=60000, currency="USD", services=("catering",), label="Q1"):
    return create_inquiry(session, InquiryCreate(
        customer_label=label, original_text=label, origin="Teterboro", destination="Aspen", trip_type="one_way",
        departure_date=date(2026, 12, 18), passenger_count=pax, budget_amount=budget, currency=currency,
        requested_services=list(services)))


def _q(session, iid, price, ccy="USD", cap=8, op="Demo Operator A (fictional)", **kw):
    return create_quote(session, QuoteCreate(inquiry_id=iid, operator_label=op, total_price=price, currency=ccy,
                                             passenger_capacity=cap, **kw))


def test_lowest_price_and_differences(session):
    inq = _inq(session)
    _q(session, inq.id, 70000, op="A", aircraft_type="Midsize jet", included_services=["catering"])
    _q(session, inq.id, 64000, op="B", aircraft_type="Light jet")
    comp = compare_quotes(inq, inq.quotes)
    assert comp.lowest_price == 64000 and comp.comparison_currency == "USD"
    a = next(x for x in comp.assessments if x.operator_label == "A")
    assert a.difference_from_lowest == 6000 and a.difference_from_lowest_pct == pytest.approx(9.4)
    assert "Includes requested catering" in a.advantages and a.budget_status == "over"
    b = next(x for x in comp.assessments if x.operator_label == "B")
    assert b.is_lowest and "catering" in b.requested_unknown
    assert any("catering" in q for q in b.operator_questions)


def test_currency_mismatch_not_price_compared(session):
    inq = _inq(session)
    _q(session, inq.id, 70000, op="A")
    _q(session, inq.id, 30000, ccy="EUR", op="B")
    comp = compare_quotes(inq, inq.quotes)
    eur = next(x for x in comp.assessments if x.currency == "EUR")
    assert not eur.comparable and "no conversion" in eur.exclusion_reason
    assert comp.lowest_price == 70000, "a cheaper-looking EUR quote must not win"
    assert any("not price-compared" in n for n in comp.notes)


def test_capacity_check(session):
    inq = _inq(session, pax=8)
    _q(session, inq.id, 40000, cap=6, op="Small")
    _q(session, inq.id, 50000, cap=None, op="Unknown")
    comp = compare_quotes(inq, inq.quotes)
    small = next(x for x in comp.assessments if x.operator_label == "Small")
    unknown = next(x for x in comp.assessments if x.operator_label == "Unknown")
    assert small.capacity_status == "insufficient" and not small.comparable
    assert unknown.capacity_status == "unknown" and "passenger capacity" in unknown.missing_information
    assert comp.lowest_quote_id == unknown.quote_id


def test_missing_quote_information_produces_questions(session):
    inq = _inq(session)
    _q(session, inq.id, 55000, cap=None, op="Sparse")
    a = compare_quotes(inq, inq.quotes).assessments[0]
    assert {"aircraft type", "passenger capacity", "quote validity date"} <= set(a.missing_information)
    assert any("taxes" in q for q in a.operator_questions)


def test_invalid_quote_values_rejected():
    for bad in [dict(total_price=0), dict(total_price=-5), dict(currency="BTC"), dict(passenger_capacity=0),
                dict(included_services=["spaceship"]), dict(included_services=["catering"], excluded_services=["catering"])]:
        payload = dict(inquiry_id="INQ-X", operator_label="A", total_price=100, currency="USD") | bad
        with pytest.raises(ValidationError):
            QuoteCreate(**payload)


def test_quote_accepted_only_via_booking(session):
    inq = _inq(session)
    q = _q(session, inq.id, 50000)
    with pytest.raises(PermissionDenied):
        change_quote_status(session, q.id, "accepted")
    change_quote_status(session, q.id, "presented")
    assert session.get(Inquiry, inq.id).status == "quoted"
    with pytest.raises(InvalidTransition):
        change_quote_status(session, q.id, "received")


def test_booking_confirmation_cancellation_and_status_validation(session):
    inq = _inq(session)
    q = _q(session, inq.id, 50000)
    with pytest.raises(PermissionDenied):
        confirm_booking_from_quote(session, q.id)  # no approving workflow
    with pytest.raises(PermissionDenied):
        confirm_booking_from_quote(session, q.id, actor="model", approved_via_workflow="RUN-X")
    b = confirm_booking_from_quote(session, q.id, approved_via_workflow="RUN-TEST")
    assert b.status == "confirmed" and b.confirmed_at and session.get(Quote, q.id).status == "accepted"
    assert session.get(Inquiry, inq.id).status == "booked"
    assert not check_booking_from_quote(session, q.id).ok
    with pytest.raises(ValidationFailure):
        cancel_booking(session, b.id, reason="  ")
    cancel_booking(session, b.id, reason="customer changed plans (fictional)")
    assert b.status == "cancelled" and session.get(Quote, q.id).status == "presented"
    assert session.get(Inquiry, inq.id).status == "quoted"
    with pytest.raises(InvalidTransition):
        complete_booking(session, b.id)


def test_booking_completion(session):
    inq = _inq(session)
    q = _q(session, inq.id, 50000)
    b = confirm_booking_from_quote(session, q.id, approved_via_workflow="RUN-TEST")
    complete_booking(session, b.id)
    assert session.get(Inquiry, inq.id).status == "completed"
    with pytest.raises(InvalidTransition):
        cancel_booking(session, b.id, reason="too late")


def test_booking_blocked_for_incomplete_inquiry(session):
    inq = create_inquiry(session, InquiryCreate(customer_label="Inc", original_text="x", destination="Aspen"))
    q = _q(session, inq.id, 10000)
    check = check_booking_from_quote(session, q.id)
    assert not check.ok and any("missing" in b.lower() or "awaiting" in b.lower() for b in check.blocking)


def test_booking_value_validation():
    with pytest.raises(ValidationError):
        BookingCreate(inquiry_id="INQ-X", booked_value=0, currency="USD")
    with pytest.raises(ValidationError):
        BookingCreate(inquiry_id="INQ-X", booked_value=10, currency="XXX")


def test_direct_booking_record_model_forbidden(session):
    inq = _inq(session)
    with pytest.raises(PermissionDenied):
        create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=100, currency="USD"), actor="model")
