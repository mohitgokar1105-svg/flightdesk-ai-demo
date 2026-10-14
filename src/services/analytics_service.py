"""Business metrics computed from database records. No hardcoded numbers.

Filter semantics (also shown in the UI):
* The date range applies to each record's own primary timestamp — inquiries/quotes/recommendations by
  ``created_at``, bookings by ``created_at`` for counts and ``confirmed_at`` for value, workflow runs by
  ``started_at``. Follow-up workload (outstanding/overdue) is *current state* and not date-filtered.
* Inquiry status and service-category filters select inquiries; related quotes, bookings, follow-ups and
  recommendations are limited to those inquiries.
* Money is grouped by currency and never summed across currencies.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.domain import (
    FOLLOWUP_CLOSED_STATUSES,
    OPEN_INQUIRY_STATUSES,
    QUOTE_AWAITING_DECISION,
    RECOMMENDATION_DECISIONS,
    VALUE_COUNTED_BOOKING_STATUSES,
    BookingStatus,
    FollowUpStatus,
    QuoteStatus,
    RecommendationStatus,
    ServiceCategory,
)
from src.models import Booking, FollowUp, Inquiry, Quote, Recommendation, WorkflowRun, utcnow

QUOTED_VALUE_EXCLUDED = {QuoteStatus.DECLINED.value, QuoteStatus.EXPIRED.value, QuoteStatus.WITHDRAWN.value}

METRIC_DEFINITIONS: dict[str, str] = {
    "total_inquiries": "Inquiries created in the date range that match the status/service filters.",
    "new_inquiries": "Of those, inquiries currently in status 'new'.",
    "open_inquiries": "Of those, inquiries currently new, needs_information, qualified or quoted.",
    "quotes_created": "Quotes recorded in the date range for in-scope inquiries.",
    "quotes_awaiting_decision": "Of those, quotes currently 'received' or 'presented' (not accepted, declined, expired or withdrawn).",
    "confirmed_bookings": "Bookings created in the date range currently in status 'confirmed' (not yet completed).",
    "completed_bookings": "Bookings created in the date range currently in status 'completed'.",
    "cancelled_bookings": "Bookings created in the date range currently in status 'cancelled'.",
    "outstanding_followups": "Follow-ups not completed or cancelled (current workload; not date-filtered).",
    "overdue_followups": "Outstanding follow-ups whose due date is in the past.",
    "estimated_opportunity_value": "Sum of advisor-estimated opportunity value for OPEN in-scope inquiries, per currency. Not revenue.",
    "total_quoted_value": "Per inquiry, the lowest quote that is not declined/expired/withdrawn, summed per currency. "
                          "Alternative quotes for the same trip are not added together. Not revenue.",
    "confirmed_booking_value": "Sum of booked value for bookings confirmed in the date range whose status is confirmed "
                               "or completed. Cancelled bookings are excluded. Per currency.",
    "conversion_rate": "Numerator: in-scope inquiries created in the range with at least one confirmed or completed booking "
                       "(counted once per inquiry). Denominator: in-scope inquiries created in the range.",
    "avg_days_to_confirmation": "Mean days from inquiry creation to booking confirmation, for confirmed/completed bookings "
                                "confirmed in the range.",
    "followup_completion_rate": "Numerator: follow-ups created in the range now 'completed'. Denominator: follow-ups created "
                                "in the range that are not cancelled.",
    "recommendation_acceptance_rate": "Numerator: recommendations created in the range marked 'accepted'. Denominator: those "
                                      "with any advisor decision (accepted, dismissed or irrelevant).",
    "workflow_success_rate": "Numerator: workflow runs started in the range that reached COMPLETED. Denominator: runs that "
                             "reached COMPLETED or FAILED (in-progress, cancelled and advisor-rejected runs are excluded).",
}


@dataclass
class DashboardFilters:
    start: date | None = None
    end: date | None = None  # inclusive
    inquiry_statuses: list[str] | None = None
    service_categories: list[str] | None = None
    include_sample: bool = True

    def in_range(self, ts: datetime | None) -> bool:
        if ts is None:
            return False
        if self.start and ts < datetime.combine(self.start, time.min, tzinfo=UTC):
            return False
        return not (self.end and ts >= datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=UTC))


@dataclass
class Ratio:
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        """None when the denominator is zero (displayed as 'n/a', never a division error)."""
        return None if self.denominator == 0 else self.numerator / self.denominator


@dataclass
class DashboardMetrics:
    counts: dict[str, int]
    money: dict[str, dict[str, float]]  # metric -> currency -> amount
    ratios: dict[str, Ratio]
    avg_days_to_confirmation: float | None
    scope_inquiry_ids: set[str] = field(default_factory=set)


def _inquiry_categories(inq: Inquiry) -> set[str]:
    return {ServiceCategory.CHARTER_FLIGHT.value, *(inq.requested_services or [])}


def scoped_inquiries(session: Session, f: DashboardFilters) -> list[Inquiry]:
    """Inquiries matching status/service/sample filters (NOT the date filter)."""
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Inquiry)
    if f.inquiry_statuses:
        stmt = stmt.where(Inquiry.status.in_(f.inquiry_statuses))
    if not f.include_sample:
        stmt = stmt.where(Inquiry.is_sample.is_(False))
    inquiries = list(session.scalars(stmt))
    if f.service_categories:
        wanted = set(f.service_categories)
        inquiries = [i for i in inquiries if _inquiry_categories(i) & wanted]
    return inquiries


def compute_metrics(session: Session, f: DashboardFilters, now: datetime | None = None) -> DashboardMetrics:
    require(Permission.VIEW_FINANCIALS)
    now = now or utcnow()
    inquiries = scoped_inquiries(session, f)
    scope_ids = {i.id for i in inquiries}
    in_range_inq = [i for i in inquiries if f.in_range(i.created_at)]
    in_range_ids = {i.id for i in in_range_inq}

    quotes = [q for q in session.scalars(select(Quote)) if q.inquiry_id in scope_ids]
    bookings = [b for b in session.scalars(select(Booking)) if b.inquiry_id in scope_ids]
    if f.service_categories:
        bookings = [b for b in bookings if b.service_category in set(f.service_categories)]
    followups = [x for x in session.scalars(select(FollowUp)) if x.inquiry_id in scope_ids]
    recs = [r for r in session.scalars(select(Recommendation)) if r.inquiry_id in scope_ids]
    runs = [r for r in session.scalars(select(WorkflowRun)) if f.in_range(r.started_at)
            and (r.inquiry_id is None or r.inquiry_id in scope_ids)]

    quotes_in_range = [q for q in quotes if f.in_range(q.created_at)]
    bookings_created = [b for b in bookings if f.in_range(b.created_at)]
    open_followups = [x for x in followups if FollowUpStatus(x.status) not in FOLLOWUP_CLOSED_STATUSES]

    counts = {
        "total_inquiries": len(in_range_inq),
        "new_inquiries": sum(1 for i in in_range_inq if i.status == "new"),
        "open_inquiries": sum(1 for i in in_range_inq if i.status in {s.value for s in OPEN_INQUIRY_STATUSES}),
        "quotes_created": len(quotes_in_range),
        "quotes_awaiting_decision": sum(1 for q in quotes_in_range if QuoteStatus(q.status) in QUOTE_AWAITING_DECISION),
        "confirmed_bookings": sum(1 for b in bookings_created if b.status == BookingStatus.CONFIRMED.value),
        "completed_bookings": sum(1 for b in bookings_created if b.status == BookingStatus.COMPLETED.value),
        "cancelled_bookings": sum(1 for b in bookings_created if b.status == BookingStatus.CANCELLED.value),
        "outstanding_followups": len(open_followups),
        "overdue_followups": sum(1 for x in open_followups if x.due_at is not None and x.due_at < now),
    }

    money: dict[str, dict[str, float]] = {"estimated_opportunity_value": defaultdict(float), "total_quoted_value": defaultdict(float),
                                          "confirmed_booking_value": defaultdict(float)}
    for i in in_range_inq:
        if i.status in {s.value for s in OPEN_INQUIRY_STATUSES} and i.estimated_opportunity_value and i.currency:
            money["estimated_opportunity_value"][i.currency] += i.estimated_opportunity_value
    lowest: dict[tuple[str, str], float] = {}
    for q in quotes_in_range:
        if q.status in QUOTED_VALUE_EXCLUDED:
            continue
        key = (q.inquiry_id, q.currency)
        lowest[key] = min(lowest.get(key, q.all_in_price), q.all_in_price)
    for (_, ccy), amount in lowest.items():
        money["total_quoted_value"][ccy] += amount
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    confirmed_in_range = [b for b in bookings if b.status in value_statuses and f.in_range(b.confirmed_at)]
    for b in confirmed_in_range:
        money["confirmed_booking_value"][b.currency] += b.booked_value
    money = {k: {c: round(v, 2) for c, v in sorted(d.items())} for k, d in money.items()}

    converted = {b.inquiry_id for b in bookings if b.status in value_statuses}
    inq_created = {i.id: i.created_at for i in inquiries}
    deltas = [(b.confirmed_at - inq_created[b.inquiry_id]).total_seconds() / 86400 for b in confirmed_in_range
              if b.confirmed_at and b.confirmed_at >= inq_created[b.inquiry_id]]
    followups_in_range = [x for x in followups if f.in_range(x.created_at) and x.status != FollowUpStatus.CANCELLED.value]
    decided = [r for r in recs if f.in_range(r.created_at) and RecommendationStatus(r.status) in RECOMMENDATION_DECISIONS]
    finished_runs = [r for r in runs if r.state in ("COMPLETED", "FAILED")]

    ratios = {
        "conversion_rate": Ratio(len(in_range_ids & converted), len(in_range_ids)),
        "followup_completion_rate": Ratio(sum(1 for x in followups_in_range if x.status == FollowUpStatus.COMPLETED.value),
                                          len(followups_in_range)),
        "recommendation_acceptance_rate": Ratio(sum(1 for r in decided if r.status == RecommendationStatus.ACCEPTED.value), len(decided)),
        "workflow_success_rate": Ratio(sum(1 for r in finished_runs if r.state == "COMPLETED"), len(finished_runs)),
    }
    return DashboardMetrics(counts=counts, money=money, ratios=ratios,
                            avg_days_to_confirmation=round(sum(deltas) / len(deltas), 1) if deltas else None,
                            scope_inquiry_ids=scope_ids)


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------
def _weekly(rows: list[dict[str, Any]], ts_key: str, value_key: str | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["week", "value"])
    df = pd.DataFrame(rows)
    df["week"] = pd.to_datetime(df[ts_key], utc=True).dt.tz_convert(None).dt.to_period("W-SUN").dt.start_time
    agg = df.groupby("week")[value_key].sum() if value_key else df.groupby("week").size()
    return agg.rename("value").reset_index().sort_values("week")


def chart_data(session: Session, f: DashboardFilters, currency: str, now: datetime | None = None) -> dict[str, pd.DataFrame]:
    require(Permission.VIEW_FINANCIALS)
    now = now or utcnow()
    inquiries = scoped_inquiries(session, f)
    scope_ids = {i.id for i in inquiries}
    in_range_inq = [i for i in inquiries if f.in_range(i.created_at)]
    bookings = [b for b in session.scalars(select(Booking)) if b.inquiry_id in scope_ids]
    if f.service_categories:
        bookings = [b for b in bookings if b.service_category in set(f.service_categories)]
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    confirmed = [b for b in bookings if b.status in value_statuses and f.in_range(b.confirmed_at)]
    quotes = [q for q in session.scalars(select(Quote)) if q.inquiry_id in scope_ids]
    followups = [x for x in session.scalars(select(FollowUp)) if x.inquiry_id in scope_ids]
    recs = [r for r in session.scalars(select(Recommendation)) if r.inquiry_id in scope_ids and f.in_range(r.created_at)]

    status_counts = pd.Series([i.status for i in in_range_inq], dtype="object").value_counts()
    ids = {i.id for i in in_range_inq}
    quoted_ids = {q.inquiry_id for q in quotes if q.inquiry_id in ids}
    presented_ids = {q.inquiry_id for q in quotes if q.inquiry_id in ids and q.status in ("presented", "accepted")}
    booked_ids = {b.inquiry_id for b in bookings if b.inquiry_id in ids and b.status in value_statuses}
    completed_ids = {b.inquiry_id for b in bookings if b.inquiry_id in ids and b.status == BookingStatus.COMPLETED.value}

    buckets = {"Overdue": 0, "Due today": 0, "Due in 7 days": 0, "Later": 0, "No due date": 0}
    for x in followups:
        if FollowUpStatus(x.status) in FOLLOWUP_CLOSED_STATUSES:
            continue
        if x.due_at is None:
            buckets["No due date"] += 1
        elif x.due_at < now:
            buckets["Overdue"] += 1
        elif x.due_at.date() == now.date():
            buckets["Due today"] += 1
        elif x.due_at <= now + timedelta(days=7):
            buckets["Due in 7 days"] += 1
        else:
            buckets["Later"] += 1

    return {
        "inquiry_trend": _weekly([{"ts": i.created_at} for i in in_range_inq], "ts"),
        "booking_trend": _weekly([{"ts": b.confirmed_at} for b in confirmed], "ts"),
        "booking_value": _weekly([{"ts": b.confirmed_at, "v": b.booked_value} for b in confirmed if b.currency == currency], "ts", "v"),
        "status_distribution": pd.DataFrame({"status": status_counts.index.astype(str), "count": status_counts.values.astype(int)}),
        "funnel": pd.DataFrame({
            "stage": ["Inquiries", "With a quote", "Quote presented", "Booked (confirmed/completed)", "Trip completed"],
            "inquiries": [len(ids), len(quoted_ids), len(presented_ids), len(booked_ids), len(completed_ids)],
        }),
        "followup_workload": pd.DataFrame({"bucket": list(buckets), "count": list(buckets.values())}),
        "recommendation_outcomes": pd.DataFrame(
            {"status": [s.value for s in RecommendationStatus],
             "count": [sum(1 for r in recs if r.status == s.value) for s in RecommendationStatus]}),
    }


def drilldown(session: Session, f: DashboardFilters, metric: str, now: datetime | None = None) -> pd.DataFrame:
    """Records behind a metric, for inspection in the UI."""
    require(Permission.VIEW_FINANCIALS)
    now = now or utcnow()
    inquiries = scoped_inquiries(session, f)
    scope_ids = {i.id for i in inquiries}
    in_range_inq = [i for i in inquiries if f.in_range(i.created_at)]
    rows: list[dict[str, Any]]
    if metric in ("total_inquiries", "new_inquiries", "open_inquiries"):
        sel = in_range_inq
        if metric == "new_inquiries":
            sel = [i for i in sel if i.status == "new"]
        elif metric == "open_inquiries":
            sel = [i for i in sel if i.status in {s.value for s in OPEN_INQUIRY_STATUSES}]
        rows = [{"inquiry_id": i.id, "created_at": i.created_at, "customer": i.customer_label, "route": f"{i.origin or '?'} → {i.destination or '?'}",
                 "status": i.status, "passengers": i.passenger_count, "opportunity_value": i.estimated_opportunity_value,
                 "currency": i.currency, "sample": i.is_sample} for i in sel]
    elif metric in ("quotes_created", "quotes_awaiting_decision", "total_quoted_value"):
        sel_q = [q for q in session.scalars(select(Quote)) if q.inquiry_id in scope_ids and f.in_range(q.created_at)]
        if metric == "quotes_awaiting_decision":
            sel_q = [q for q in sel_q if QuoteStatus(q.status) in QUOTE_AWAITING_DECISION]
        rows = [{"quote_id": q.id, "inquiry_id": q.inquiry_id, "operator": q.operator_label, "price": q.all_in_price,
                 "currency": q.currency, "status": q.status, "created_at": q.created_at, "sample": q.is_sample} for q in sel_q]
    elif metric in ("confirmed_bookings", "completed_bookings", "cancelled_bookings", "confirmed_booking_value", "conversion_rate"):
        sel_b = [b for b in session.scalars(select(Booking)) if b.inquiry_id in scope_ids]
        if metric == "confirmed_booking_value" or metric == "conversion_rate":
            sel_b = [b for b in sel_b if b.status in {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
                     and (f.in_range(b.confirmed_at) if metric == "confirmed_booking_value" else b.inquiry_id in {i.id for i in in_range_inq})]
        else:
            status = metric.split("_")[0]
            sel_b = [b for b in sel_b if b.status == status and f.in_range(b.created_at)]
        rows = [{"booking_id": b.id, "inquiry_id": b.inquiry_id, "quote_id": b.quote_id, "status": b.status,
                 "value": b.booked_value, "currency": b.currency, "category": b.service_category,
                 "created_at": b.created_at, "confirmed_at": b.confirmed_at, "sample": b.is_sample} for b in sel_b]
    elif metric in ("outstanding_followups", "overdue_followups", "followup_completion_rate"):
        sel_f = [x for x in session.scalars(select(FollowUp)) if x.inquiry_id in scope_ids]
        if metric == "followup_completion_rate":
            sel_f = [x for x in sel_f if f.in_range(x.created_at) and x.status != "cancelled"]
        else:
            sel_f = [x for x in sel_f if FollowUpStatus(x.status) not in FOLLOWUP_CLOSED_STATUSES]
            if metric == "overdue_followups":
                sel_f = [x for x in sel_f if x.due_at is not None and x.due_at < now]
        rows = [{"followup_id": x.id, "inquiry_id": x.inquiry_id, "type": x.followup_type, "status": x.status,
                 "due_at": x.due_at, "subject": x.subject, "sample": x.is_sample} for x in sel_f]
    elif metric == "recommendation_acceptance_rate":
        rows = [{"recommendation_id": r.id, "inquiry_id": r.inquiry_id, "category": r.category, "status": r.status,
                 "action": r.suggested_action, "feedback": r.advisor_feedback}
                for r in session.scalars(select(Recommendation)) if r.inquiry_id in scope_ids and f.in_range(r.created_at)]
    elif metric == "workflow_success_rate":
        rows = [{"run_id": r.id, "type": r.workflow_type, "state": r.state, "started_at": r.started_at,
                 "error_category": r.error_category, "sample": r.is_sample}
                for r in session.scalars(select(WorkflowRun)) if f.in_range(r.started_at)]
    else:
        rows = []
    return pd.DataFrame(rows)
