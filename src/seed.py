"""Fictional demonstration data.

* Customers are "Demo Customer NNN"; operators are "Demo Operator X (fictional)". No real people,
  bookings or company performance figures are represented.
* Inquiry fields are produced by running the real rule-based extractor over generated texts, so seeded
  records are internally consistent with the application's own logic.
* Seeded workflow runs are *real executions* of the deterministic planner / intake workflows.
* Every seeded row has ``is_sample = True``. Seeding is idempotent and never deletes data.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.ai.extraction import run_rule_based
from src.auth import Permission, require
from src.config import Settings
from src.database import delete_all_records
from src.domain import (
    BookingStatus,
    FollowUpStatus,
    FollowUpType,
    InquiryStatus,
    QuoteStatus,
    RecommendationStatus,
    ServiceCategory,
    Tone,
)
from src.models import AuditEvent, Inquiry, Quote, WorkflowRun, utcnow
from src.schemas import BookingCreate, FollowUpCreate, InquiryCreate, QuoteCreate
from src.services.booking_service import create_booking_record
from src.services.followup_service import build_followup_facts, create_followup
from src.services.inquiry_service import create_inquiry
from src.services.quote_service import create_quote
from src.services.recommendation_service import decide_recommendation, generate_recommendations

ROUTES = [
    ("Los Angeles", "Aspen", "USD"), ("Teterboro", "Palm Beach", "USD"), ("London", "Nice", "EUR"),
    ("Miami", "Nassau", "USD"), ("Dallas", "Cabo San Lucas", "USD"), ("San Francisco", "Las Vegas", "USD"),
    ("New York", "Nantucket", "USD"), ("Geneva", "Ibiza", "EUR"), ("Chicago", "Jackson Hole", "USD"),
    ("London", "Geneva", "GBP"), ("Boston", "Martha's Vineyard", "USD"), ("Van Nuys", "Sun Valley", "USD"),
    ("Paris", "Mykonos", "EUR"), ("Houston", "Telluride", "USD"), ("Seattle", "Vail", "USD"),
    ("Atlanta", "Turks and Caicos", "USD"), ("Zurich", "Olbia", "EUR"), ("White Plains", "St. Barts", "USD"),
]
SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}
TEMPLATES = [
    "Hi, we need a private flight from {o} to {d} for {pax} people departing {dep}, returning {ret}. Budget is around {sym}{budget}.{extra}",
    "Looking for a round trip {o} to {d}. {pax} passengers, leaving {dep} and back on {ret}.{extra}",
    "One way {o} to {d} on {dep} for {pax} passengers please.{extra}",
    "Could you quote a jet from {o} to {d}? We are {pax} travelling, dates are flexible, ideally {dep}.{extra}",
    "Party of {pax} flying {o} to {d} {dep}. Return {ret}. Our budget is {sym}{budget}.{extra}",
    "Need a flight to {d} sometime {month}, not sure about numbers yet.{extra}",
    "Hello, interested in flying from {o} to {d} for a family trip.{extra}",
]
EXTRAS = [
    "", " Morning departure preferred.", " Could you also help us arrange a hotel?", " We will need a car service on arrival.",
    " We're bringing our dog.", " We have skis and golf clubs.", " Catering with vegetarian options please.",
    " It's for a birthday celebration.", " Please also suggest a villa nearby.", " We'd like to leave around 2pm.",
]
AIRCRAFT = [("Light jet", 6), ("Midsize jet", 8), ("Super-midsize jet", 9), ("Heavy jet", 14), ("Turboprop", 7)]
OPERATORS = ["Demo Operator A (fictional)", "Demo Operator B (fictional)", "Demo Operator C (fictional)",
             "Demo Operator D (fictional)"]
# Fictional operator directory (555-01xx numbers are reserved for fiction).
OPERATOR_DIRECTORY = [
    ("Demo Operator A (fictional)", "Alex Rivera", "ops-a@example.com", "+1 555 0101", "Teterboro Airport (TEB)",
     "Light & midsize jets"),
    ("Demo Operator B (fictional)", "Sam Patel", "ops-b@example.com", "+1 555 0102", "Van Nuys Airport (VNY)",
     "Midsize & super-midsize jets"),
    ("Demo Operator C (fictional)", "Jordan Lee", "ops-c@example.com", "+44 20 7946 0103", "Farnborough Airport (FAB)",
     "Super-midsize & heavy jets"),
    ("Demo Operator D (fictional)", "Morgan Blake", "ops-d@example.com", "+1 555 0104",
     "Charlotte Douglas International Airport (CLT)", "Turboprops & light jets"),
    ("Demo Operator E (fictional)", "Casey Moore", "ops-e@example.com", "+33 1 70 00 01 05",
     "Paris-Le Bourget International Airport (LBG)", "Heavy jets"),
    ("Demo Operator F (fictional)", "Riley Chen", "ops-f@example.com", "+1 555 0106", "Miami-Opa Locka Executive Airport (OPF)",
     "Light jets & turboprops"),
]
STATUS_PLAN = (["new"] * 5 + ["needs_information"] * 6 + ["qualified"] * 5 + ["quoted"] * 7 + ["booked"] * 5
               + ["completed"] * 4 + ["closed"] * 3 + ["cancelled_booking"] * 3)


def sample_data_present(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.is_sample.is_(True))))


def _shift(obj: Any, created: datetime, *fields: str) -> None:
    for f in fields:
        setattr(obj, f, created)


def seed_demo_data(session: Session, settings: Settings | None = None, *, rng_seed: int = 42,
                   now: datetime | None = None) -> dict[str, Any]:
    """Insert the fictional sample dataset if it is not already present. Never deletes anything."""
    require(Permission.ADMIN_DATA)
    if sample_data_present(session):
        return {"skipped": True, "reason": "Sample data already present."}
    rng = random.Random(rng_seed)
    now = now or utcnow()
    seeding_started = utcnow()
    created_ids: list[str] = []
    counts = {"inquiries": 0, "quotes": 0, "bookings": 0, "followups": 0, "recommendations": 0, "workflow_runs": 0}

    for idx, plan in enumerate(STATUS_PLAN, start=1):
        o, d, ccy = ROUTES[idx % len(ROUTES)]
        created_at = now - timedelta(days=rng.randint(2, 115), hours=rng.randint(0, 23))
        if plan in ("new", "needs_information"):
            created_at = now - timedelta(days=rng.randint(0, 20), hours=rng.randint(0, 23))
        dep = (created_at + timedelta(days=rng.randint(10, 60))).date()
        ret = dep + timedelta(days=rng.randint(3, 10))
        pax = rng.choice([2, 3, 4, 5, 6, 8, 10])
        budget = rng.choice([25, 30, 40, 55, 70, 90, 120]) * 1000
        if plan == "needs_information":
            tmpl = TEMPLATES[5] if idx % 2 else TEMPLATES[6]
        elif plan == "new":
            tmpl = TEMPLATES[idx % 5]  # some "new" texts are incomplete and land in needs_information
        else:
            tmpl = TEMPLATES[[0, 1, 2, 4][idx % 4]]  # complete texts for inquiries that progressed
        text = tmpl.format(o=o, d=d, pax=pax, dep=dep.strftime("%B %d %Y"), ret=ret.strftime("%B %d %Y"),
                           sym=SYMBOL[ccy], budget=f"{budget:,}", month=dep.strftime("in %B"), extra=EXTRAS[idx % len(EXTRAS)])
        outcome = run_rule_based(text, created_at.date())
        ex = outcome.extraction
        assert ex is not None
        has_contact = idx % 5 != 0  # some customers have no contact details, to show the "missing contact" state
        data = InquiryCreate(
            customer_label=f"Demo Customer {idx:03d}", original_text=text,
            customer_email=f"demo.customer{idx:03d}@example.com" if has_contact else None,
            customer_phone=f"+1 555 01{idx % 100:02d}" if has_contact and idx % 2 else None,
            departure_time_preference=ex.departure_time_preference,
            special_requirements="; ".join(ex.special_requirements) or None, origin=ex.origin, destination=ex.destination,
            trip_type=ex.trip_type, departure_date=ex.departure.exact, departure_earliest=ex.departure.earliest,
            departure_latest=ex.departure.latest, return_date=ex.return_date.exact, return_earliest=ex.return_date.earliest,
            return_latest=ex.return_date.latest, passenger_count=ex.passenger_count, trip_purpose=ex.trip_purpose,
            date_flexibility=ex.date_flexibility, budget_amount=ex.budget_amount, currency=ex.currency or ccy,
            requested_services=ex.requested_services,
            estimated_opportunity_value=float(ex.budget_amount or budget),
            advisor_notes="Seeded fictional record.")
        inq = create_inquiry(session, data, source="seed", is_sample=True, actor="system",
                             extraction_metadata={"method": "rule_based", "seeded": True})
        _shift(inq, created_at, "created_at", "updated_at")
        created_ids.append(inq.id)
        if plan not in ("new", "needs_information") and inq.missing_information:
            plan = "needs_information"  # keep statuses consistent with what the extractor actually found
        counts["inquiries"] += 1

        needs_quotes = plan in ("quoted", "booked", "completed", "cancelled_booking") or (plan == "qualified" and idx % 2 == 0)
        if plan in ("qualified", "quoted", "booked", "completed", "closed", "cancelled_booking") and not inq.missing_information:
            inq.status = InquiryStatus.QUALIFIED.value
        quotes: list[Quote] = []
        if needs_quotes:
            for qn in range(rng.choice([2, 3])):
                ac, cap = rng.choice(AIRCRAFT)
                q_ccy = data.currency or "USD"
                if qn == 2 and idx % 4 == 0:
                    q_ccy = "EUR" if q_ccy != "EUR" else "GBP"
                price = round(budget * rng.uniform(0.75, 1.35) / 100) * 100
                fees = rng.choice([None, None, round(price * 0.04, -1)])
                inc = rng.sample(["catering", "wifi", "taxes", "landing_and_handling_fees", "ground_transport"], k=rng.randint(1, 3))
                exc = [x for x in ["de_icing", "crew_overnight", "accommodation"] if x not in inc][: rng.randint(0, 2)]
                q = create_quote(session, QuoteCreate(
                    inquiry_id=inq.id, operator_label=OPERATORS[qn], total_price=price, currency=q_ccy,
                    additional_fees=fees,
                    fees_description="Fictional handling and de-icing fees" if fees else None,
                    aircraft_type=ac if rng.random() > 0.15 else None, passenger_capacity=cap if rng.random() > 0.1 else None,
                    included_services=inc, excluded_services=exc,
                    restrictions=rng.choice([None, None, "Departure slot subject to airport curfew", "Max 2 pets in cabin"]),
                    valid_until=(created_at + timedelta(days=14)).date() if rng.random() > 0.3 else None,
                    notes="Fictional demonstration quote.", source_description="Seeded fictional demo quote"),
                    is_sample=True, actor="system")
                q_created = created_at + timedelta(days=1 + qn)
                _shift(q, q_created, "created_at", "updated_at")
                quotes.append(q)
                counts["quotes"] += 1

        if plan in ("quoted", "booked", "completed", "cancelled_booking"):
            inq.status = InquiryStatus.QUOTED.value
            for q in quotes:
                q.status = QuoteStatus.PRESENTED.value
                q.presented_at = q.created_at + timedelta(hours=rng.randint(2, 30))
        if plan == "closed":
            inq.status = InquiryStatus.CLOSED.value
        if plan in ("booked", "completed", "cancelled_booking") and quotes:
            chosen = min((q for q in quotes if q.currency == quotes[0].currency), key=lambda q: q.total_price)
            for q in quotes:
                if q is not chosen:
                    q.status = QuoteStatus.DECLINED.value
            confirmed_at = created_at + timedelta(days=rng.randint(3, 12), hours=rng.randint(1, 20))
            status = {"booked": BookingStatus.CONFIRMED, "completed": BookingStatus.COMPLETED,
                      "cancelled_booking": BookingStatus.CANCELLED}[plan]
            b = create_booking_record(session, BookingCreate(
                inquiry_id=inq.id, quote_id=chosen.id, booked_value=chosen.all_in_price, currency=chosen.currency,
                service_category=ServiceCategory.CHARTER_FLIGHT, notes="Fictional booking record."),
                status=status, is_sample=True, actor="system", created_at=confirmed_at, confirmed_at=confirmed_at)
            counts["bookings"] += 1
            if status == BookingStatus.CANCELLED:
                chosen.status = QuoteStatus.PRESENTED.value
                inq.status = InquiryStatus.QUOTED.value if idx % 2 else InquiryStatus.CLOSED.value
                b.cancelled_at = confirmed_at + timedelta(days=2)
                b.notes = "Fictional booking record.\nCancelled: customer changed plans (fictional)."
            else:
                chosen.status = QuoteStatus.ACCEPTED.value
                inq.status = InquiryStatus.BOOKED.value if status == BookingStatus.CONFIRMED else InquiryStatus.COMPLETED.value
            if "accommodation" in (inq.requested_services or []) and status != BookingStatus.CANCELLED:
                create_booking_record(session, BookingCreate(
                    inquiry_id=inq.id, booked_value=round(chosen.total_price * 0.18, -2), currency=chosen.currency,
                    service_category=ServiceCategory.ACCOMMODATION, notes="Fictional hotel arrangement."),
                    status=status, is_sample=True, actor="system", created_at=confirmed_at, confirmed_at=confirmed_at)
                counts["bookings"] += 1

        session.flush()
        # Follow-ups
        facts = build_followup_facts(session, inq.id)
        fu_plan: list[tuple[FollowUpType, FollowUpStatus, int]] = []
        if inq.missing_information:
            fu_plan.append((FollowUpType.MISSING_INFORMATION, rng.choice([FollowUpStatus.DRAFT, FollowUpStatus.APPROVED,
                                                                            FollowUpStatus.SCHEDULED]), rng.randint(-4, 3)))
        if inq.status == InquiryStatus.QUOTED.value:
            fu_plan.append((FollowUpType.QUOTE_FOLLOWUP, rng.choice([FollowUpStatus.SCHEDULED, FollowUpStatus.COMPLETED,
                                                                       FollowUpStatus.PENDING_APPROVAL]), rng.randint(-6, 5)))
        if inq.status == InquiryStatus.COMPLETED.value:
            fu_plan.append((FollowUpType.POST_TRIP, rng.choice([FollowUpStatus.COMPLETED, FollowUpStatus.SCHEDULED]),
                            rng.randint(-3, 6)))
        if "accommodation" in (inq.requested_services or []):
            fu_plan.append((FollowUpType.ACCOMMODATION, rng.choice([FollowUpStatus.COMPLETED, FollowUpStatus.CANCELLED,
                                                                      FollowUpStatus.APPROVED]), rng.randint(-2, 4)))
        from src.ai.drafting import template_draft

        for ftype, fstatus, due_offset in fu_plan:
            draft = template_draft(facts, ftype, Tone.PROFESSIONAL)
            fu_created = created_at + timedelta(hours=rng.randint(2, 30))
            fu = create_followup(session, FollowUpCreate(
                inquiry_id=inq.id, followup_type=ftype, subject=draft.subject, body=draft.body,
                due_at=now + timedelta(days=due_offset, hours=rng.randint(1, 8)), generation_method="template"),
                is_sample=True, actor="system", status=fstatus, created_at=fu_created)
            if fstatus == FollowUpStatus.COMPLETED:
                fu.completed_at = min(now, fu_created + timedelta(days=rng.randint(1, 4)))
                fu.advisor_notes = "Recorded as completed (fictional). No email is sent by this application."
            counts["followups"] += 1

        # Recommendations with a mix of advisor decisions
        res = generate_recommendations(session, inq.id, actor="system")
        for r in res["created"]:
            r.is_sample = True
            r.created_at = created_at + timedelta(hours=1)
            roll = rng.random()
            if roll < 0.45:
                decide_recommendation(session, r.id, RecommendationStatus.ACCEPTED, feedback="Useful (fictional feedback)")
            elif roll < 0.62:
                decide_recommendation(session, r.id, RecommendationStatus.DISMISSED, feedback="Not now (fictional feedback)")
            elif roll < 0.70:
                decide_recommendation(session, r.id, RecommendationStatus.IRRELEVANT, feedback="Not relevant (fictional)")
            if r.decided_at:
                r.decided_at = r.created_at + timedelta(hours=rng.randint(1, 48))
            counts["recommendations"] += 1

    session.flush()
    # Everything recorded while seeding is sample noise, not business activity.
    for ae in session.scalars(select(AuditEvent).where(AuditEvent.timestamp >= seeding_started)):
        ae.event_metadata = {**(ae.event_metadata or {}), "seeded": True}
    session.commit()

    # Real deterministic workflow executions for the inspector / success-rate metric.
    if settings is not None:
        from src.workflows.workflow_engine import run_triage, start_intake

        for iid in created_ids[:6]:
            run_triage(session, settings, iid, client=None)
            counts["workflow_runs"] += 1
        start_intake(session, settings, text="Need a jet from Miami to Aspen next Friday for 4 people.",
                     customer_label="Demo Customer 900", reference_date=now.date(), method="rule_based")
        start_intake(session, settings, text="   ", customer_label="Demo Customer 901", reference_date=now.date(),
                     method="rule_based")  # genuinely fails validation (empty input)
        counts["workflow_runs"] += 2
        for run in session.scalars(select(WorkflowRun)):
            run.is_sample = True
        session.commit()
    counts["operators"] = _seed_operators(session, now)
    session.commit()
    return {"skipped": False, **counts}


def _seed_operators(session: Session, now: datetime) -> int:
    """Fictional operator directory plus a few quote requests still waiting for a reply (so "chase" shows up)."""
    from src.models import Operator, OperatorRequest

    if session.scalar(select(func.count()).select_from(Operator)):
        return 0
    ops = []
    for name, contact, email, phone, base, fleet in OPERATOR_DIRECTORY:
        op = Operator(name=name, contact_name=contact, email=email, phone=phone, base_airport=base, fleet=fleet,
                      is_sample=True)
        session.add(op)
        ops.append(op)
    session.flush()
    waiting = list(session.scalars(select(Inquiry).where(Inquiry.status.in_(["new", "qualified"]), Inquiry.is_sample.is_(True))
                                   .order_by(Inquiry.created_at.desc()).limit(3)))
    for n, inq in enumerate(waiting):
        for op in ops[n:n + 3]:
            session.add(OperatorRequest(inquiry_id=inq.id, operator_id=op.id, channel="whatsapp", is_sample=True,
                                        message="(sample request)", status="sent",
                                        sent_at=now - timedelta(hours=20 + 9 * n)))
    return len(ops)


def reset_demo_data(session: Session, confirmation: str, settings: Settings | None = None) -> dict[str, Any]:
    """Delete ALL records (requires confirmation token) and re-seed the fictional dataset."""
    deleted = delete_all_records(session, confirmation)
    session.flush()
    seeded = seed_demo_data(session, settings)
    return {"deleted": deleted, "seeded": seeded}
