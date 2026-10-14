"""Team workspace: the work queue ("what needs my attention?"), next actions, inquiry activity history and a
plain-English inquiry summary. Everything is derived from stored records; nothing is estimated."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.auth import Permission, require
from src.domain import (
    FOLLOWUP_CLOSED_STATUSES,
    FOLLOWUP_TYPE_LABELS,
    INQUIRY_STATUS_LABELS,
    FollowUpStatus,
    FollowUpType,
)
from src.models import AuditEvent, Booking, FollowUp, Inquiry, utcnow

RECENT_DAYS = 7
SOURCE_LABELS = {"customer_portal": "From website", "intake": "From email/message", "manual": "Entered by team",
                 "seed": "Sample", "csv": "Imported"}


@dataclass
class QueueItem:
    kind: str
    inquiry_id: str
    title: str
    detail: str
    tag: str = ""  # short label, e.g. "From website", "Overdue"
    followup_id: str | None = None
    urgent: bool = False


@dataclass
class WorkQueue:
    new: list[QueueItem] = field(default_factory=list)
    in_progress: list[QueueItem] = field(default_factory=list)
    awaiting_customer: list[QueueItem] = field(default_factory=list)
    followups_due: list[QueueItem] = field(default_factory=list)
    recently_completed: list[QueueItem] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {"new": len(self.new), "in_progress": len(self.in_progress), "awaiting_customer": len(self.awaiting_customer),
                "followups_due": len(self.followups_due), "recently_completed": len(self.recently_completed)}


def _ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return ""
    delta = now - ts
    if delta < timedelta(hours=1):
        return "just now"
    if delta < timedelta(days=1):
        h = int(delta.total_seconds() // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = delta.days
    return f"{d} day{'s' if d != 1 else ''} ago"


def trip_line(inq: Inquiry) -> str:
    parts = [f"{inq.origin or '?'} → {inq.destination or '?'}"]
    if inq.departure_date:
        parts.append(f"{inq.departure_date:%d %b %Y}")
    if inq.passenger_count:
        parts.append(f"{inq.passenger_count} passenger{'s' if inq.passenger_count != 1 else ''}")
    return " · ".join(parts)


def next_action(inq: Inquiry) -> str:
    """The one thing to do next for an inquiry, in plain words."""
    missing = [m["field"].replace("_", " ") for m in (inq.missing_information or [])]
    has_live_quote = any(q.status in ("received", "presented") for q in inq.quotes)
    if inq.status == "new":
        return "Check the details, then request quotes" if not missing else "Ask the customer for: " + ", ".join(missing[:3])
    if inq.status == "needs_information":
        return "Ask the customer for: " + ", ".join(missing[:3]) if missing else "Confirm the details with the customer"
    if inq.status == "qualified":
        return "Send a quote to the customer" if has_live_quote else "Record operator quotes"
    if inq.status == "quoted":
        return "Follow up on the quote you sent"
    if inq.status == "booked":
        return "Prepare for the trip"
    if inq.status == "completed":
        return "Send a thank-you"
    return "No action needed"


def work_queue(session: Session, now: datetime | None = None) -> WorkQueue:
    """The team inbox. Each inquiry appears once in new/in progress/awaiting customer; follow-ups are listed separately."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    end_of_today = datetime.combine(now.date(), time.max, tzinfo=UTC)
    q = WorkQueue()
    for inq in session.scalars(select(Inquiry).where(Inquiry.status.in_(["new", "needs_information", "qualified", "quoted"]))
                               .options(selectinload(Inquiry.quotes))
                               .order_by(Inquiry.created_at)):
        tag = SOURCE_LABELS.get(inq.source, "")
        if inq.status == "new":
            q.new.append(QueueItem("new", inq.id, inq.customer_label, f"{trip_line(inq)} · received {_ago(inq.created_at, now)}",
                                   tag=tag))
        elif inq.status == "quoted":
            sent = [x.presented_at for x in inq.quotes if x.status == "presented" and x.presented_at]
            when = _ago(max(sent), now) if sent else "recently"
            q.awaiting_customer.append(QueueItem("awaiting_customer", inq.id, inq.customer_label,
                                                 f"{trip_line(inq)} · quote sent {when}", tag="Waiting on customer"))
        else:
            q.in_progress.append(QueueItem("in_progress", inq.id, inq.customer_label,
                                           f"{trip_line(inq)} · next: {next_action(inq).lower()}",
                                           tag=INQUIRY_STATUS_LABELS.get(inq.status, inq.status)))
    q.new.sort(key=lambda i: i.tag != "From website")  # stable: website requests first, then oldest first
    for f in session.scalars(select(FollowUp).where(FollowUp.due_at.is_not(None), FollowUp.due_at <= end_of_today)
                             .options(selectinload(FollowUp.inquiry))
                             .order_by(FollowUp.due_at)):
        if FollowUpStatus(f.status) in FOLLOWUP_CLOSED_STATUSES:
            continue
        overdue = f.due_at is not None and f.due_at < datetime.combine(now.date(), time.min, tzinfo=UTC)
        label = FOLLOWUP_TYPE_LABELS.get(FollowUpType(f.followup_type), f.followup_type)
        q.followups_due.append(QueueItem("followup", f.inquiry_id, f.inquiry.customer_label, f"{label} · {f.subject}",
                                         tag="Overdue" if overdue else "Due today", followup_id=f.id, urgent=overdue))
    since = now - timedelta(days=RECENT_DAYS)
    done: list[tuple[datetime, QueueItem]] = []
    for f in session.scalars(select(FollowUp).where(FollowUp.completed_at.is_not(None), FollowUp.completed_at >= since)):
        assert f.completed_at is not None
        done.append((f.completed_at, QueueItem("done", f.inquiry_id, f.inquiry.customer_label, f"Follow-up done · {f.subject}",
                                               tag=_ago(f.completed_at, now), followup_id=f.id)))
    for b in session.scalars(select(Booking).where(Booking.confirmed_at >= since, Booking.status.in_(["confirmed", "completed"]))
                             .options(selectinload(Booking.inquiry))):
        if b.confirmed_at is None:
            continue
        done.append((b.confirmed_at, QueueItem("done", b.inquiry_id, b.inquiry.customer_label,
                                               f"Booking confirmed · {trip_line(b.inquiry)}", tag=_ago(b.confirmed_at, now))))
    for inq in session.scalars(select(Inquiry).where(Inquiry.status.in_(["closed", "completed"]), Inquiry.updated_at >= since)):
        done.append((inq.updated_at, QueueItem("done", inq.id, inq.customer_label,
                                               f"{INQUIRY_STATUS_LABELS[inq.status]} · {trip_line(inq)}",
                                               tag=_ago(inq.updated_at, now))))
    q.recently_completed = [item for _, item in sorted(done, key=lambda x: x[0], reverse=True)[:8]]
    return q


# ---------------------------------------------------------------------------- activity history
_EVENT_TEXT = {
    "inquiry_created": "Inquiry created", "customer_request_submitted": "Customer submitted the request on the website",
    "inquiry_status_changed": "Status changed", "inquiry_updated": "Details edited", "quote_recorded": "Quote recorded",
    "quote_status_changed": "Quote status changed", "followup_created": "Email draft saved",
    "followup_updated": "Email draft edited", "followup_status_changed": "Follow-up status changed",
    "booking_confirmed": "Booking confirmed", "booking_created": "Booking recorded", "booking_cancelled": "Booking cancelled",
    "booking_completed": "Trip completed", "booking_recorded": "Booking recorded", "recommendation_created": "Next steps suggested",
    "recommendation_decided": "Suggestion reviewed", "recommendation_rejected": "Suggestion dismissed", "proposal_failed": "AI suggestion could not be applied",
    "quote_pdf_created": "Customer PDF created (not sent)", "proposal_approved": "AI suggestion approved", "proposal_rejected": "AI suggestion declined",
}
_WHO = {"advisor": "team", "model": "AI (with approval)", "system": "system"}


def _describe(e: AuditEvent) -> str:
    meta = e.event_metadata or {}
    text = _EVENT_TEXT.get(e.event_type, e.event_type.replace("_", " ").capitalize())
    if "to" in meta and "status" in e.event_type:
        target = str(meta["to"])
        text += f" to “{INQUIRY_STATUS_LABELS.get(target, target.replace('_', ' '))}”"
    if meta.get("source") == "customer_portal" and e.event_type == "inquiry_created":
        text = "Inquiry created from the website form"
    return text


def inquiry_activity(session: Session, inquiry_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """Audit history for an inquiry and its quotes, follow-ups and bookings, newest first."""
    require(Permission.VIEW_OPERATIONS)
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        return []
    ids = {inq.id} | {x.id for x in inq.quotes} | {x.id for x in inq.followups} | {x.id for x in inq.bookings}
    events = session.scalars(select(AuditEvent).where(AuditEvent.entity_id.in_(ids))
                             .order_by(AuditEvent.timestamp.desc()).limit(limit))
    return [{"time": e.timestamp, "text": _describe(e), "who": _WHO.get(e.actor_type, e.actor_type),
             "outcome": e.outcome} for e in events if e.event_type not in ("ai_request",)]


# ---------------------------------------------------------------------------- summary
def inquiry_facts(session: Session, inquiry_id: str) -> dict[str, Any]:
    """Facts about one inquiry for a summary (the AI may only rephrase these)."""
    require(Permission.VIEW_OPERATIONS)
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        return {}
    live = [x for x in inq.quotes if x.status in ("received", "presented", "accepted")]
    cheapest = min(live, key=lambda x: x.all_in_price) if live else None
    open_fus = [f for f in inq.followups if FollowUpStatus(f.status) not in FOLLOWUP_CLOSED_STATUSES]
    return {
        "customer": inq.customer_label, "status": INQUIRY_STATUS_LABELS.get(inq.status, inq.status),
        "source": SOURCE_LABELS.get(inq.source, inq.source),
        "origin": inq.origin, "destination": inq.destination,
        "trip_type": (inq.trip_type or "").replace("_", " ") or None,
        "departure_date": inq.departure_date.isoformat() if inq.departure_date else None,
        "return_date": inq.return_date.isoformat() if inq.return_date else None,
        "passengers": inq.passenger_count, "preferred_time": inq.departure_time_preference,
        "special_requirements": inq.special_requirements,
        "missing_information": [m["question"] for m in (inq.missing_information or [])],
        "quotes_recorded": len(inq.quotes), "live_quotes": len(live),
        "cheapest_live_quote": {"operator": cheapest.operator_label, "all_in_price": cheapest.all_in_price,
                                "currency": cheapest.currency} if cheapest else None,
        "quotes_sent_to_customer": sum(1 for x in inq.quotes if x.status == "presented"),
        "open_follow_ups": len(open_fus), "next_action": next_action(inq),
        "has_confirmed_booking": any(b.status in ("confirmed", "completed") for b in inq.bookings),
    }


def plain_inquiry_summary(facts: dict[str, Any]) -> list[str]:
    """Deterministic summary sentences (shown when AI is off, and the ground truth for the AI version)."""
    if not facts:
        return []
    f = facts
    trip = f"{f['customer']} wants to fly {f['origin'] or '(origin not given)'} → {f['destination'] or '(destination not given)'}"
    if f["departure_date"]:
        trip += f" on {f['departure_date']}" + (f" ({f['preferred_time']})" if f["preferred_time"] else "")
    if f["return_date"]:
        trip += f", returning {f['return_date']}"
    if f["passengers"]:
        trip += f", {f['passengers']} passenger{'s' if f['passengers'] != 1 else ''}"
    out = [trip + "."]
    if f["special_requirements"]:
        out.append(f"Requests: {f['special_requirements']}.")
    if f["missing_information"]:
        out.append("Still missing: " + "; ".join(f["missing_information"]) + ".")
    if f["quotes_recorded"]:
        line = f"{f['quotes_recorded']} quote{'s' if f['quotes_recorded'] != 1 else ''} recorded"
        if f["cheapest_live_quote"]:
            c = f["cheapest_live_quote"]
            line += f"; the lowest live all-in price is {c['all_in_price']:,.0f} {c['currency']} ({c['operator']})"
        out.append(line + ".")
    else:
        out.append("No quotes recorded yet.")
    out.append(f"Status: {f['status']}. Next: {f['next_action']}.")
    return out

