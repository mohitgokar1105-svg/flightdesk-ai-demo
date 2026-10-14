"""Business-owner analytics: periods, KPIs, date boundaries, currencies, duplicates, cancellations, empty DB."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.domain import BookingStatus, FollowUpType, ServiceCategory
from src.schemas import BookingCreate, FollowUpCreate, InquiryCreate, QuoteCreate
from src.services.booking_service import create_booking_record
from src.services.dashboard_service import (
    PROFIT_NOTE,
    business_kpis,
    change_text,
    needs_attention,
    plain_summary,
    previous_period,
    recent_activity,
    resolve_period,
    summary_facts,
    trend_frame,
)
from src.services.followup_service import create_followup
from src.services.inquiry_service import create_inquiry
from src.services.quote_service import change_quote_status, create_quote

TODAY = date(2026, 10, 14)  # a Wednesday
NOW = datetime(2026, 10, 14, 15, tzinfo=UTC)


def _inq(s, label, created, **kw):
    data = dict(customer_label=label, original_text=label, origin="Miami", destination="Aspen", trip_type="one_way",
                departure_date=date(2026, 12, 1), passenger_count=2)
    data.update(kw)
    inq = create_inquiry(s, InquiryCreate(**data))
    inq.created_at = created
    return inq


def test_period_resolution_and_previous_period():
    assert resolve_period("this_week", TODAY).start == date(2026, 10, 12)
    assert resolve_period("this_month", TODAY).start == date(2026, 10, 1)
    assert resolve_period("last_30_days", TODAY).start == date(2026, 9, 15)
    assert resolve_period("this_quarter", TODAY).start == date(2026, 10, 1)
    assert resolve_period("this_year", TODAY).start == date(2026, 1, 1)
    all_time = resolve_period("all_time", TODAY)
    assert all_time.start is None and previous_period(all_time) is None
    prev = previous_period(resolve_period("this_month", TODAY))  # 1–14 Oct vs 1–14 Sep (like for like)
    assert (prev.start, prev.end, prev.label) == (date(2026, 9, 1), date(2026, 9, 14), "same days last month")
    week = previous_period(resolve_period("this_week", TODAY))  # Mon 12–Wed 14 Oct vs Mon 5–Wed 7 Oct
    assert (week.start, week.end) == (date(2026, 10, 5), date(2026, 10, 7))
    last_month = previous_period(resolve_period("last_month", TODAY))  # all of September vs all of August
    assert (last_month.start, last_month.end) == (date(2026, 8, 1), date(2026, 8, 31))
    year = previous_period(resolve_period("this_year", TODAY))
    assert (year.start, year.end) == (date(2025, 1, 1), date(2025, 10, 14))
    leap = previous_period(resolve_period("this_month", date(2026, 3, 31)))  # March 1–31 vs Feb 1–28
    assert leap.end == date(2026, 2, 28)
    with pytest.raises(ValueError):
        resolve_period("forever", TODAY)


def test_date_boundaries_are_inclusive(session):
    p = resolve_period("this_month", TODAY)
    _inq(session, "first-day", datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    _inq(session, "last-moment-before", datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))
    _inq(session, "today-late", datetime(2026, 10, 14, 23, 59, tzinfo=UTC))
    _inq(session, "same-day-last-month", datetime(2026, 9, 14, 23, 59, tzinfo=UTC))  # inside 1–14 Sep
    _inq(session, "after-same-day-last-month", datetime(2026, 9, 15, 0, 0, tzinfo=UTC))  # outside
    k = business_kpis(session, p, NOW)
    assert k.new_inquiries == 2
    assert k.previous["new_inquiries"] == 1, "compared like for like: 1–14 Sep, not the 14 days before"
    assert k.previous_label.startswith("same days last month")


def test_empty_database_kpis_are_zero_and_safe(session):
    k = business_kpis(session, resolve_period("this_month", TODAY), NOW)
    assert (k.new_inquiries, k.quotes_sent, k.confirmed_bookings, k.followups_due) == (0, 0, 0, 0)
    assert k.booked_value == {} and k.conversion_rate is None
    assert trend_frame(session, resolve_period("this_month", TODAY)).empty
    assert needs_attention(session, NOW) == [] and recent_activity(session) == []
    lines = plain_summary(summary_facts(session, resolve_period("this_month", TODAY), NOW))
    assert lines[0].startswith("0 new inquiries") and "Nothing urgent" in lines[-1]


def test_quotes_sent_counts_sent_date_not_creation(session):
    inq = _inq(session, "Q", datetime(2026, 9, 1, tzinfo=UTC))
    q1 = create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label="A", total_price=1000, currency="USD"))
    q1.created_at = datetime(2026, 9, 2, tzinfo=UTC)
    create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label="B", total_price=900, currency="USD"))
    change_quote_status(session, q1.id, "presented")
    assert q1.presented_at is not None
    k = business_kpis(session, resolve_period("all_time"), NOW)
    assert k.quotes_created == 2 and k.quotes_sent == 1


def test_booking_value_currencies_cancellations_and_conversion(session):
    created = datetime(2026, 10, 2, tzinfo=UTC)
    a = _inq(session, "A", created)
    b = _inq(session, "B", created, currency="EUR")
    c = _inq(session, "C", created)
    _inq(session, "D", created)
    when = datetime(2026, 10, 5, tzinfo=UTC)
    for inq, value, ccy, status in ((a, 1000, "USD", BookingStatus.CONFIRMED), (b, 2000, "EUR", BookingStatus.COMPLETED),
                                    (c, 5000, "USD", BookingStatus.CANCELLED)):
        create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=value, currency=ccy),
                              status=status, created_at=when, confirmed_at=when)
    create_booking_record(session, BookingCreate(inquiry_id=a.id, booked_value=300, currency="USD",
                                                 service_category=ServiceCategory.ACCOMMODATION),
                          created_at=when, confirmed_at=when)
    k = business_kpis(session, resolve_period("this_month", TODAY), NOW)
    assert k.booked_value == {"EUR": 2000.0, "USD": 1300.0}, "currencies separate; cancelled excluded"
    assert k.confirmed_bookings == 3
    assert (k.conversion_numerator, k.conversion_denominator) == (2, 4), "each inquiry counted once"
    assert PROFIT_NOTE in k.as_dict()["notes"]


def test_quoted_value_uses_cheapest_all_in_quote_per_inquiry(session):
    inq = _inq(session, "Q2", datetime(2026, 10, 3, tzinfo=UTC))
    for op, price, fees in (("A", 50000, None), ("B", 45000, 6000), ("C", 48000, None)):
        q = create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label=op, total_price=price, additional_fees=fees,
                                              currency="USD"))
        q.created_at = datetime(2026, 10, 4, tzinfo=UTC)
    k = business_kpis(session, resolve_period("this_month", TODAY), NOW)
    assert k.quoted_value == {"USD": 48000.0}, "B is 51,000 all-in; alternatives are not summed"


def test_followups_due_and_attention_items(session):
    inq = _inq(session, "F", datetime(2026, 10, 1, tzinfo=UTC), passenger_count=None)
    for subject, due in (("late", NOW - timedelta(days=2)), ("today", NOW + timedelta(hours=2)),
                         ("soon", NOW + timedelta(days=5)), ("later", NOW + timedelta(days=20))):
        create_followup(session, FollowUpCreate(inquiry_id=inq.id, followup_type=FollowUpType.GENERAL_UPDATE, subject=subject,
                                                body=subject, due_at=due))
    k = business_kpis(session, resolve_period("this_month", TODAY), NOW)
    assert k.followups_due == 3 and k.followups_overdue == 1
    kinds = [a.kind for a in needs_attention(session, NOW)]
    assert kinds[:2] == ["followup_overdue", "followup_today"] and "missing_details" in kinds


def test_change_text():
    assert change_text(5, 3) == "+2 vs previous period"
    assert change_text(1, 3) == "−2 vs previous period"
    assert change_text(2, 2) == "same as previous period"
    assert change_text(2, None) == ""


def test_trend_frame_buckets(session):
    _inq(session, "T1", datetime(2026, 10, 5, tzinfo=UTC))
    _inq(session, "T2", datetime(2026, 10, 6, tzinfo=UTC))
    df = trend_frame(session, resolve_period("this_month", TODAY))
    row = df[df["series"] == "Inquiries received"]
    assert row["count"].sum() == 2 and str(row["bucket"].iloc[0]) == "2026-10-05"


def test_recent_activity_uses_record_dates(session):
    from src.services.dashboard_service import recent_activity

    inq = _inq(session, "Act", datetime(2026, 10, 2, tzinfo=UTC))
    q = create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label="Op A", total_price=7000, currency="USD"))
    change_quote_status(session, q.id, "presented")
    q.presented_at = datetime(2026, 10, 3, tzinfo=UTC)
    events = recent_activity(session, 5, now=NOW)
    assert [e["kind"] for e in events] == ["quote", "inquiry"]
    assert events[0]["text"] == "Quote sent · Op A, 7,000 USD" and events[0]["who"] == "Act"
    assert events[1]["time"] == datetime(2026, 10, 2, tzinfo=UTC)


def test_trend_window_widens_short_periods():
    from src.services.dashboard_service import trend_window

    w = trend_window(resolve_period("this_month", TODAY))
    assert w.days is not None and w.days >= 12 * 7 - 6 and w.end == TODAY and w.start.weekday() == 0
    year = resolve_period("this_year", TODAY)
    assert trend_window(year) == year
