"""Analytics: conversion maths, empty DB, date filters, cancelled exclusion, currencies, double counting."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from src.domain import BookingStatus, ServiceCategory
from src.schemas import BookingCreate, InquiryCreate, QuoteCreate
from src.services.analytics_service import DashboardFilters, chart_data, compute_metrics, drilldown
from src.services.booking_service import create_booking_record
from src.services.inquiry_service import create_inquiry
from src.services.quote_service import create_quote

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _inq(session, label, created, currency="USD", opp=None, services=()):
    inq = create_inquiry(session, InquiryCreate(customer_label=label, original_text=label, origin="Miami", destination="Aspen",
                                                trip_type="one_way", departure_date=date(2026, 12, 1), passenger_count=2,
                                                currency=currency, estimated_opportunity_value=opp,
                                                requested_services=list(services)))
    inq.created_at = created
    return inq


def _book(session, inq, value, currency="USD", status=BookingStatus.CONFIRMED, when=None, category=ServiceCategory.CHARTER_FLIGHT):
    when = when or inq.created_at + timedelta(days=4)
    return create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=value, currency=currency,
                                                        service_category=category), status=status, created_at=when, confirmed_at=when)


def test_empty_database_has_no_division_errors(session):
    m = compute_metrics(session, DashboardFilters(), NOW)
    assert all(v == 0 for v in m.counts.values())
    assert all(r.value is None and r.denominator == 0 for r in m.ratios.values())
    assert m.avg_days_to_confirmation is None
    charts = chart_data(session, DashboardFilters(), "USD", NOW)
    assert charts["inquiry_trend"].empty and charts["funnel"]["inquiries"].sum() == 0


def test_conversion_rate_counts_inquiries_once_and_excludes_cancelled(session):
    a = _inq(session, "A", NOW - timedelta(days=10))
    b = _inq(session, "B", NOW - timedelta(days=9))
    c = _inq(session, "C", NOW - timedelta(days=8))
    _inq(session, "D", NOW - timedelta(days=7))
    _book(session, a, 1000)
    _book(session, a, 500, category=ServiceCategory.ACCOMMODATION)  # second booking, same inquiry
    _book(session, b, 2000, status=BookingStatus.COMPLETED)
    _book(session, c, 3000, status=BookingStatus.CANCELLED)
    m = compute_metrics(session, DashboardFilters(), NOW)
    assert (m.ratios["conversion_rate"].numerator, m.ratios["conversion_rate"].denominator) == (2, 4)
    assert m.money["confirmed_booking_value"] == {"USD": 3500.0}
    assert m.counts["cancelled_bookings"] == 1 and m.counts["confirmed_bookings"] == 2 and m.counts["completed_bookings"] == 1
    assert m.avg_days_to_confirmation == 4.0


def test_currencies_never_summed(session):
    a = _inq(session, "A", NOW - timedelta(days=5), opp=10000)
    b = _inq(session, "B", NOW - timedelta(days=5), currency="EUR", opp=20000)
    _book(session, a, 1000, "USD")
    _book(session, b, 2000, "EUR")
    m = compute_metrics(session, DashboardFilters(), NOW)
    assert m.money["confirmed_booking_value"] == {"EUR": 2000.0, "USD": 1000.0}
    assert m.money["estimated_opportunity_value"] == {"EUR": 20000.0, "USD": 10000.0}


def test_quoted_value_uses_lowest_quote_per_inquiry(session):
    a = _inq(session, "A", NOW - timedelta(days=3))
    for i, price in enumerate([50000, 42000, 61000]):
        q = create_quote(session, QuoteCreate(inquiry_id=a.id, operator_label=f"Op{i}", total_price=price, currency="USD"))
        q.created_at = NOW - timedelta(days=2)
    m = compute_metrics(session, DashboardFilters(), NOW)
    assert m.money["total_quoted_value"] == {"USD": 42000.0}
    assert m.counts["quotes_created"] == 3 and m.counts["quotes_awaiting_decision"] == 3


def test_date_filter_changes_metrics(session):
    old = _inq(session, "Old", NOW - timedelta(days=200))
    _inq(session, "New", NOW - timedelta(days=5))
    _book(session, old, 7000, when=NOW - timedelta(days=195))
    last30 = DashboardFilters(start=(NOW - timedelta(days=30)).date(), end=NOW.date())
    m = compute_metrics(session, last30, NOW)
    assert m.counts["total_inquiries"] == 1 and m.money["confirmed_booking_value"] == {}
    m_all = compute_metrics(session, DashboardFilters(), NOW)
    assert m_all.counts["total_inquiries"] == 2 and m_all.money["confirmed_booking_value"] == {"USD": 7000.0}


def test_status_and_service_filters(session):
    _inq(session, "Hotel", NOW - timedelta(days=5), services=("accommodation",))
    _inq(session, "Plain", NOW - timedelta(days=5))
    m = compute_metrics(session, DashboardFilters(service_categories=["accommodation"]), NOW)
    assert m.counts["total_inquiries"] == 1
    m = compute_metrics(session, DashboardFilters(inquiry_statuses=["closed"]), NOW)
    assert m.counts["total_inquiries"] == 0


def test_dashboard_updates_after_new_record(session):
    before = compute_metrics(session, DashboardFilters(), NOW).counts["total_inquiries"]
    _inq(session, "Fresh", NOW - timedelta(hours=1))
    assert compute_metrics(session, DashboardFilters(), NOW).counts["total_inquiries"] == before + 1


def test_drilldown_matches_metric(seeded):
    f = DashboardFilters()
    m = compute_metrics(seeded, f)
    assert len(drilldown(seeded, f, "open_inquiries")) == m.counts["open_inquiries"]
    assert len(drilldown(seeded, f, "overdue_followups")) == m.counts["overdue_followups"]
    dd = drilldown(seeded, f, "confirmed_booking_value")
    for ccy, total in m.money["confirmed_booking_value"].items():
        assert dd[dd["currency"] == ccy]["value"].sum() == total
