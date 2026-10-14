"""Business-owner analytics: reporting periods, headline KPIs, trends, needs-attention items, recent activity and
deterministic business-summary facts.

These functions are shared by the Overview page, the Reports page and the AI assistant's tools, so every number the
owner sees — and every number the assistant explains — comes from one place.

Financial rules: inquiries are never bookings; quotes are never revenue; opportunity, quoted and booked values are kept
apart; booked value counts confirmed + completed bookings only (never cancelled); money is grouped per currency; profit
is not calculated because operator cost data is not recorded.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth import Permission, has_permission, require
from src.domain import (
    FOLLOWUP_CLOSED_STATUSES,
    FOLLOWUP_TYPE_LABELS,
    OPEN_INQUIRY_STATUSES,
    VALUE_COUNTED_BOOKING_STATUSES,
    FollowUpStatus,
    FollowUpType,
    QuoteStatus,
)
from src.models import Booking, FollowUp, Inquiry, Quote, utcnow

PERIODS = {
    "this_week": "This week", "last_week": "Last week", "this_month": "This month", "last_month": "Last month",
    "last_30_days": "Last 30 days", "this_quarter": "This quarter", "this_year": "This year", "all_time": "All time",
}
FOLLOWUP_HORIZON_DAYS = 7
STALE_QUOTE_DAYS = 3
PROFIT_NOTE = "Profit is not shown: operator costs and commissions are not recorded, so booked value is not profit."


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date | None  # inclusive; None = beginning of records
    end: date  # inclusive

    def contains(self, ts: datetime | None) -> bool:
        if ts is None:
            return False
        d = ts.astimezone(UTC).date()
        return (self.start is None or d >= self.start) and d <= self.end

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days + 1

    def describe(self) -> str:
        if self.start is None:
            return f"all records up to {self.end:%d %b %Y}"
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"


def resolve_period(key: str, today: date | None = None) -> Period:
    today = today or utcnow().date()
    if key not in PERIODS:
        raise ValueError(f"Unknown period '{key}'. Use one of: {', '.join(PERIODS)}")
    if key == "this_week":
        start = today - timedelta(days=today.weekday())
    elif key == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        return Period(key, PERIODS[key], end - timedelta(days=6), end)
    elif key == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return Period(key, PERIODS[key], end.replace(day=1), end)
    elif key == "this_month":
        start = today.replace(day=1)
    elif key == "last_30_days":
        start = today - timedelta(days=29)
    elif key == "this_quarter":
        start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    elif key == "this_year":
        start = date(today.year, 1, 1)
    else:
        return Period(key, PERIODS[key], None, today)
    return Period(key, PERIODS[key], start, today)


def custom_period(start: date, end: date, today: date | None = None) -> Period:
    """An explicit inclusive date range (validated)."""
    today = today or utcnow().date()
    if end < start:
        raise ValueError("The end date is before the start date.")
    if start > today:
        raise ValueError("The range starts in the future.")
    end = min(end, today)
    return Period("custom", f"{start:%d %b %Y} – {end:%d %b %Y}", start, end)


def period_from(key: str | None, start: date | None = None, end: date | None = None) -> Period:
    """Resolve a named period, or a custom range when both dates are given."""
    if start is not None and end is not None:
        return custom_period(start, end)
    return resolve_period(key or "this_month")


def _shift_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, days_in_month(date(y, m + 1, 1))))


def previous_period(p: Period) -> Period | None:
    """The like-for-like earlier period (None for all time).

    "To date" periods compare with the same days of the previous week/month/quarter/year (Mon–Wed this week vs
    Mon–Wed last week); complete periods compare with the one before; anything else with the same number of days
    immediately before."""
    if p.start is None or p.days is None:
        return None
    if p.key == "this_week":
        return Period("prev_this_week", "same days last week", p.start - timedelta(days=7), p.end - timedelta(days=7))
    if p.key == "last_week":
        return Period("prev_last_week", "the week before", p.start - timedelta(days=7), p.end - timedelta(days=7))
    if p.key in ("this_month", "this_quarter", "this_year"):
        months = {"this_month": 1, "this_quarter": 3, "this_year": 12}[p.key]
        label = {"this_month": "same days last month", "this_quarter": "same days last quarter",
                 "this_year": "same period last year"}[p.key]
        return Period(f"prev_{p.key}", label, _shift_months(p.start, -months), _shift_months(p.end, -months))
    if p.key == "last_month":
        start = _shift_months(p.start, -1)
        return Period("prev_last_month", "the month before", start, date(start.year, start.month, days_in_month(start)))
    end = p.start - timedelta(days=1)
    return Period(f"prev_{p.key}", f"previous {p.days} days", end - timedelta(days=p.days - 1), end)


# --------------------------------------------------------------------------- KPIs
@dataclass
class KPIs:
    period: Period
    new_inquiries: int
    quotes_created: int
    quotes_sent: int
    confirmed_bookings: int
    booked_value: dict[str, float]
    followups_due: int
    followups_overdue: int
    conversion_numerator: int
    conversion_denominator: int
    opportunity_value: dict[str, float] = field(default_factory=dict)
    quoted_value: dict[str, float] = field(default_factory=dict)
    previous: dict[str, int] = field(default_factory=dict)
    financials_visible: bool = True
    quotes_in_progress: int = 0  # right now: quotes received or sent on open inquiries, awaiting a decision
    previous_label: str = ""  # e.g. "same days last week (05 Oct 2026 – 08 Oct 2026)"

    @property
    def conversion_rate(self) -> float | None:
        return None if self.conversion_denominator == 0 else self.conversion_numerator / self.conversion_denominator

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period.label, "period_dates": self.period.describe(),
            "new_inquiries": self.new_inquiries, "quotes_created": self.quotes_created, "quotes_sent": self.quotes_sent,
            "quotes_in_progress_now": self.quotes_in_progress,
            "confirmed_bookings": self.confirmed_bookings,
            "booked_value_by_currency": self.booked_value if self.financials_visible else "restricted for this role",
            "followups_due_next_7_days_including_overdue": self.followups_due, "followups_overdue": self.followups_overdue,
            "booking_conversion": {"booked_inquiries": self.conversion_numerator, "inquiries_received": self.conversion_denominator,
                                   "rate_percent": None if self.conversion_rate is None else round(self.conversion_rate * 100, 1),
                                   "definition": "Inquiries received in the period that have a confirmed or completed booking."},
            "estimated_opportunity_value_by_currency": self.opportunity_value if self.financials_visible else "restricted",
            "quoted_value_by_currency": self.quoted_value if self.financials_visible else "restricted",
            "previous_period": self.previous,
            "previous_period_is": self.previous_label or None,
            "notes": ["Opportunity and quoted values are not revenue.", "Currencies are never added together.", PROFIT_NOTE],
        }


def _counts_for(session: Session, p: Period) -> dict[str, int]:
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    return {
        "new_inquiries": sum(1 for (ts,) in session.execute(select(Inquiry.created_at)) if p.contains(ts)),
        "quotes_sent": sum(1 for (ts,) in session.execute(select(Quote.presented_at).where(Quote.presented_at.is_not(None)))
                           if p.contains(ts)),
        "confirmed_bookings": sum(1 for (ts, st) in session.execute(select(Booking.confirmed_at, Booking.status))
                                  if st in value_statuses and p.contains(ts)),
    }


def business_kpis(session: Session, period: Period, now: datetime | None = None) -> KPIs:
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    inquiries = [i for i in session.scalars(select(Inquiry)) if period.contains(i.created_at)]
    ids = {i.id for i in inquiries}
    quotes = list(session.scalars(select(Quote).options(selectinload(Quote.inquiry))))
    bookings = list(session.scalars(select(Booking)))
    booked: dict[str, float] = defaultdict(float)
    for b in bookings:
        if b.status in value_statuses and period.contains(b.confirmed_at):
            booked[b.currency] += b.booked_value
    converted = {b.inquiry_id for b in bookings if b.status in value_statuses}
    opp: dict[str, float] = defaultdict(float)
    open_statuses = {s.value for s in OPEN_INQUIRY_STATUSES}
    for i in inquiries:
        if i.status in open_statuses and i.estimated_opportunity_value and i.currency:
            opp[i.currency] += i.estimated_opportunity_value
    lowest: dict[tuple[str, str], float] = {}
    for q in quotes:
        if period.contains(q.created_at) and q.status not in ("declined", "expired", "withdrawn"):
            k = (q.inquiry_id, q.currency)
            lowest[k] = min(lowest.get(k, q.all_in_price), q.all_in_price)
    quoted: dict[str, float] = defaultdict(float)
    for (_, ccy), v in lowest.items():
        quoted[ccy] += v
    horizon = now + timedelta(days=FOLLOWUP_HORIZON_DAYS)
    open_fus = [f for f in session.scalars(select(FollowUp)) if FollowUpStatus(f.status) not in FOLLOWUP_CLOSED_STATUSES]
    counts = _counts_for(session, period)
    prev = previous_period(period)
    show_money = has_permission(Permission.VIEW_FINANCIALS)
    if not show_money:  # money is only returned to roles allowed to see financials
        booked, opp, quoted = defaultdict(float), defaultdict(float), defaultdict(float)
    return KPIs(
        period=period, new_inquiries=counts["new_inquiries"],
        quotes_created=sum(1 for q in quotes if period.contains(q.created_at)), quotes_sent=counts["quotes_sent"],
        confirmed_bookings=counts["confirmed_bookings"], booked_value={k: round(v, 2) for k, v in sorted(booked.items())},
        followups_due=sum(1 for f in open_fus if f.due_at is not None and f.due_at <= horizon),
        followups_overdue=sum(1 for f in open_fus if f.due_at is not None and f.due_at < now),
        conversion_numerator=len(ids & converted), conversion_denominator=len(ids),
        opportunity_value={k: round(v, 2) for k, v in sorted(opp.items())},
        quoted_value={k: round(v, 2) for k, v in sorted(quoted.items())},
        previous=_counts_for(session, prev) if prev else {},
        previous_label=f"{prev.label} ({prev.describe()})" if prev else "",
        financials_visible=show_money,
        quotes_in_progress=sum(1 for q in quotes if q.status in ("received", "presented")
                               and q.inquiry.status in open_statuses),
    )


def change_text(current: int, previous: int | None, label: str = "previous period") -> str:
    """'+3 vs same days last week' / 'same as same days last week' / '' (no comparison possible)."""
    if previous is None:
        return ""
    diff = current - previous
    if diff == 0:
        return f"same as {label}"
    return f"{'+' if diff > 0 else '−'}{abs(diff)} vs {label}"


# --------------------------------------------------------------------------- trends
TREND_MIN_WEEKS = 12


def trend_window(period: Period) -> Period:
    """Short periods are widened to the last 12 weeks so the trend has enough points to be meaningful."""
    if period.start is not None and period.days is not None and period.days < TREND_MIN_WEEKS * 7:
        start = period.end - timedelta(days=period.end.weekday()) - timedelta(weeks=TREND_MIN_WEEKS - 1)
        return Period(f"trend_{period.key}", f"last {TREND_MIN_WEEKS} weeks", start, period.end)
    return period


def trend_frame(session: Session, period: Period) -> pd.DataFrame:
    """Counts per week (periods ≤ 92 days) or per month: inquiries received, quotes sent, bookings confirmed."""
    require(Permission.VIEW_OPERATIONS)
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    series: dict[str, list[datetime]] = {
        "Inquiries received": [i.created_at for i in session.scalars(select(Inquiry)) if period.contains(i.created_at)],
        "Quotes sent": [ts for (ts,) in session.execute(select(Quote.presented_at)) if ts is not None and period.contains(ts)],
        "Bookings confirmed": [b.confirmed_at for b in session.scalars(select(Booking))
                               if b.confirmed_at is not None and b.status in value_statuses and period.contains(b.confirmed_at)],
    }
    weekly = period.days is not None and period.days <= 92
    rows = []
    for name, stamps in series.items():
        for ts in stamps:
            d = ts.astimezone(UTC).date()
            bucket = d - timedelta(days=d.weekday()) if weekly else d.replace(day=1)
            rows.append({"bucket": bucket, "series": name})
    if not rows:
        return pd.DataFrame(columns=["bucket", "series", "count"])
    df = pd.DataFrame(rows).groupby(["bucket", "series"]).size().rename("count").reset_index()
    return df.sort_values("bucket")


# --------------------------------------------------------------------------- needs attention
@dataclass
class AttentionItem:
    kind: str
    priority: int  # 1 = most urgent
    title: str
    detail: str
    inquiry_id: str | None = None
    followup_id: str | None = None
    quote_id: str | None = None
    page: str = "inquiries"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "detail": self.detail, "inquiry_id": self.inquiry_id,
                "followup_id": self.followup_id, "quote_id": self.quote_id}


def needs_attention(session: Session, now: datetime | None = None, limit: int = 50) -> list[AttentionItem]:
    """Actionable items, most urgent first. Derived only from stored records."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    items: list[AttentionItem] = []
    end_of_today = datetime.combine(now.date(), time.max, tzinfo=UTC)
    for f in session.scalars(select(FollowUp).where(FollowUp.due_at.is_not(None)).order_by(FollowUp.due_at)
                             .options(selectinload(FollowUp.inquiry))):
        if FollowUpStatus(f.status) in FOLLOWUP_CLOSED_STATUSES or f.due_at is None:
            continue
        who = f.inquiry.customer_label
        label = FOLLOWUP_TYPE_LABELS.get(FollowUpType(f.followup_type), f.followup_type).lower()
        if f.due_at < now:
            days = max(1, (now - f.due_at).days)
            items.append(AttentionItem("followup_overdue", 1, f"Overdue follow-up: {who}",
                                       f"{label.capitalize()} was due {days} day{'s' if days != 1 else ''} ago",
                                       f.inquiry_id, followup_id=f.id, page="followups"))
        elif f.due_at <= end_of_today:
            items.append(AttentionItem("followup_today", 2, f"Follow-up due today: {who}", label.capitalize(),
                                       f.inquiry_id, followup_id=f.id, page="followups"))
    for q in session.scalars(select(Quote).where(Quote.status == QuoteStatus.RECEIVED.value)
                             .options(selectinload(Quote.inquiry))):
        items.append(AttentionItem("quote_to_review", 3, f"Quote to review: {q.inquiry.customer_label}",
                                   f"{q.operator_label} · {q.all_in_price:,.0f} {q.currency} — not yet sent to the customer",
                                   q.inquiry_id, quote_id=q.id, page="quotes"))
    for q in session.scalars(select(Quote).where(Quote.status == QuoteStatus.PRESENTED.value)
                             .options(selectinload(Quote.inquiry))):
        if q.presented_at and q.presented_at < now - timedelta(days=STALE_QUOTE_DAYS) and q.inquiry.status == "quoted":
            days = (now - q.presented_at).days
            items.append(AttentionItem("quote_no_reply", 4, f"Waiting on a decision: {q.inquiry.customer_label}",
                                       f"Quote from {q.operator_label} sent {days} days ago", q.inquiry_id, quote_id=q.id,
                                       page="followups"))
    seen_inquiries: set[str] = set()
    for inq in session.scalars(select(Inquiry).where(Inquiry.status.in_(["new", "needs_information"]))
                               .order_by(Inquiry.created_at)):
        seen_inquiries.add(inq.id)
        if inq.status == "needs_information":
            missing = ", ".join(m["field"].replace("_", " ") for m in (inq.missing_information or [])) or "details"
            items.append(AttentionItem("missing_details", 5, f"Missing trip details: {inq.customer_label}",
                                       f"Still need: {missing}", inq.id, page="inquiries"))
        else:
            age = (now - inq.created_at).days
            items.append(AttentionItem("new_inquiry", 6, f"New inquiry to review: {inq.customer_label}",
                                       f"{inq.origin or '?'} → {inq.destination or '?'} · received "
                                       + ("today" if age == 0 else f"{age} day{'s' if age != 1 else ''} ago"),
                                       inq.id, page="inquiries"))
    open_statuses = {s.value for s in OPEN_INQUIRY_STATUSES}
    for inq in session.scalars(select(Inquiry).where(Inquiry.status.in_(open_statuses))
                               .options(selectinload(Inquiry.customer))):
        c = inq.customer
        if c is not None and not (c.email or c.phone):
            items.append(AttentionItem("missing_contact", 7, f"No contact details: {inq.customer_label}",
                                       "Add an email or phone number so you can reply", inq.id, page="customers"))
    items.sort(key=lambda a: (a.priority, a.title))
    return items[:limit]


# --------------------------------------------------------------------------- recent activity
def recent_activity(session: Session, limit: int = 12, now: datetime | None = None) -> list[dict[str, Any]]:
    """Latest business events, dated by the records themselves (received, sent, confirmed, completed…)."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    events: list[dict[str, Any]] = []

    def add(ts: datetime | None, text: str, who: str, kind: str) -> None:
        if ts is not None and ts <= now + timedelta(minutes=1):
            events.append({"time": ts, "text": text, "who": who, "kind": kind})

    for i in session.scalars(select(Inquiry).order_by(Inquiry.created_at.desc()).limit(limit)):
        add(i.created_at, f"New inquiry · {i.origin or '?'} → {i.destination or '?'}", i.customer_label, "inquiry")
    for q in session.scalars(select(Quote).where(Quote.presented_at.is_not(None)).order_by(Quote.presented_at.desc()).limit(limit)):
        add(q.presented_at, f"Quote sent · {q.operator_label}, {q.all_in_price:,.0f} {q.currency}", q.inquiry.customer_label, "quote")
    for b in session.scalars(select(Booking).order_by(Booking.confirmed_at.desc()).limit(limit)):
        add(b.confirmed_at, f"Booking confirmed · {b.booked_value:,.0f} {b.currency}", b.inquiry.customer_label, "booking")
        if b.cancelled_at:
            add(b.cancelled_at, "Booking cancelled", b.inquiry.customer_label, "booking")
        if b.completed_at and b.status == "completed":
            add(b.completed_at, "Trip completed", b.inquiry.customer_label, "booking")
    for f in session.scalars(select(FollowUp).where(FollowUp.completed_at.is_not(None))
                             .order_by(FollowUp.completed_at.desc()).limit(limit)):
        add(f.completed_at, f"Follow-up done · {f.subject}", f.inquiry.customer_label, "followup")
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:limit]


# --------------------------------------------------------------------------- summary facts
def summary_facts(session: Session, period: Period, now: datetime | None = None) -> dict[str, Any]:
    """Facts for the business summary (deterministic; the AI may only rephrase these)."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    k = business_kpis(session, period, now)
    attention = needs_attention(session, now)
    by_kind: dict[str, int] = defaultdict(int)
    for a in attention:
        by_kind[a.kind] += 1
    return {"kpis": k.as_dict(), "attention_counts": dict(by_kind),
            "open_inquiries": sum(1 for (s,) in session.execute(select(Inquiry.status))
                                  if s in {x.value for x in OPEN_INQUIRY_STATUSES}),
            "today": now.date().isoformat()}


def plain_summary(facts: dict[str, Any]) -> list[str]:
    """Deterministic plain-English summary sentences (used when AI is unavailable, and as the AI's ground truth)."""
    k = facts["kpis"]
    prev = k.get("previous_period") or {}
    out: list[str] = []
    n = k["new_inquiries"]
    trend = ""
    where = (k.get("previous_period_is") or "previous period").split(" (")[0]
    if "new_inquiries" in prev:
        p = prev["new_inquiries"]
        if n > p:
            trend = f" — up from {p} in the {where}"
        elif n < p:
            trend = f" — down from {p} in the {where}"
        else:
            trend = f" — the same as in the {where}"
    out.append(f"{n} new inquir{'y' if n == 1 else 'ies'} {k['period'].lower()}{trend}.")
    out.append(f"{k['quotes_sent']} quote{'s' if k['quotes_sent'] != 1 else ''} sent and {k['confirmed_bookings']} "
               f"booking{'s' if k['confirmed_bookings'] != 1 else ''} confirmed in the same period.")
    if isinstance(k["booked_value_by_currency"], dict) and k["booked_value_by_currency"]:
        out.append("Confirmed booking value: " + ", ".join(f"{v:,.0f} {c}" for c, v in k["booked_value_by_currency"].items()) + ".")
    a = facts["attention_counts"]
    todo = []
    if a.get("followup_overdue"):
        todo.append(f"{a['followup_overdue']} overdue follow-up{'s' if a['followup_overdue'] != 1 else ''}")
    if a.get("quote_to_review"):
        todo.append(f"{a['quote_to_review']} quote{'s' if a['quote_to_review'] != 1 else ''} to review")
    if a.get("missing_details"):
        todo.append(f"{a['missing_details']} inquir{'y' if a['missing_details'] == 1 else 'ies'} waiting for details")
    if a.get("new_inquiry"):
        todo.append(f"{a['new_inquiry']} new inquir{'y' if a['new_inquiry'] == 1 else 'ies'} to review")
    if todo:
        out.append("Needs attention: " + ", ".join(todo) + ".")
    else:
        out.append("Nothing urgent needs attention right now.")
    return out


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


# --------------------------------------------------------------------------- funnel and demand
def booking_funnel(session: Session, period: Period) -> list[tuple[str, int]]:
    """One cohort — inquiries received in the period — and how far each got. Each stage is a subset of the previous."""
    require(Permission.VIEW_OPERATIONS)
    value_statuses = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
    cohort = [i for i in session.scalars(select(Inquiry).options(selectinload(Inquiry.quotes),
                                                                 selectinload(Inquiry.bookings)))
              if period.contains(i.created_at)]
    booked = {i.id for i in cohort if any(b.status in value_statuses for b in i.bookings)}
    # "reached at least this stage": a booking recorded without a sent quote still counts as having passed earlier stages
    sent = {i.id for i in cohort if any(q.presented_at is not None or q.status in ("presented", "accepted")
                                       for q in i.quotes)} | booked
    quoted = {i.id for i in cohort if i.quotes} | sent
    return [("Inquiries received", len(cohort)), ("Quotes recorded", len(quoted)), ("Quote sent to customer", len(sent)),
            ("Booked", len(booked))]


def requested_routes(session: Session, period: Period) -> list[tuple[str | None, str | None]]:
    """(origin, destination) of inquiries received in the period, as entered."""
    require(Permission.VIEW_OPERATIONS)
    return [(i.origin, i.destination) for i in session.scalars(select(Inquiry)) if period.contains(i.created_at)]
