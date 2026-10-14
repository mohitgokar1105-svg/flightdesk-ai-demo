"""Concrete tools: input schemas, handlers (read / propose) and approval-time executors."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.ai.drafting import template_draft
from src.auth import Permission as AuthPermission
from src.domain import (
    INQUIRY_TRANSITIONS,
    MODEL_PROPOSABLE_INQUIRY_STATUSES,
    FollowUpType,
    InquiryStatus,
    Tone,
    ensure_transition,
)
from src.exceptions import NotFound, PermissionDenied, ValidationFailure
from src.models import Recommendation
from src.schemas import FollowUpCreate
from src.services.analytics_service import DashboardFilters, compute_metrics
from src.services.booking_service import confirm_booking_from_quote
from src.services.followup_service import (
    build_followup_facts,
    create_followup,
    followup_to_dict,
    list_due_followups,
)
from src.services.inquiry_service import change_inquiry_status, get_inquiry, inquiry_to_dict, search_inquiries
from src.services.quote_service import compare_quotes, list_quotes_for_inquiry, quote_to_dict
from src.services.recommendation_service import decide_recommendation
from src.workflows.tool_registry import Permission, ToolContext, ToolRegistry, ToolSpec

InquiryId = Field(description="Inquiry ID, e.g. INQ-1A2B3C4D5E", min_length=5, max_length=20, pattern=r"^INQ-[A-Z0-9]{4,16}$")
FollowUpTypeLit = Literal["missing_information", "quote_followup", "alternatives", "accommodation", "additional_services",
                          "post_interaction", "post_trip"]
ProposableStatus = Literal["needs_information", "qualified", "closed"]
assert {s.value for s in MODEL_PROPOSABLE_INQUIRY_STATUSES} == set(ProposableStatus.__args__)  # type: ignore[attr-defined]


RESTRICTED = "restricted for your role (hidden, not zero)"

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetInquiryArgs(_Strict):
    inquiry_id: str = InquiryId


class QuotePdfArgs(_Strict):
    inquiry_id: str = InquiryId
    show_operator_names: bool = Field(default=False, description="Show operator names instead of Option A/B/C. "
                                                                 "Only true if the user explicitly asks for names.")


class SearchInquiriesArgs(_Strict):
    query: str = Field(default="", max_length=80, description="Text to match in ID, customer label, origin or destination")
    status: Literal["new", "needs_information", "qualified", "quoted", "booked", "completed", "closed"] | None = None
    limit: int = Field(default=10, ge=1, le=20)


class InquiryOnlyArgs(_Strict):
    inquiry_id: str = InquiryId


class CompareQuotesArgs(_Strict):
    inquiry_id: str = InquiryId
    comparison_currency: Literal["USD", "EUR", "GBP", "CHF", "CAD", "AUD", "AED", "SGD", "JPY"] | None = None


class BusinessMetricsArgs(_Strict):
    start_date: str | None = Field(default=None, description="YYYY-MM-DD inclusive; null = all time", max_length=10)
    end_date: str | None = Field(default=None, description="YYYY-MM-DD inclusive; null = today", max_length=10)


class DueFollowupsArgs(_Strict):
    within_days: int = Field(default=7, ge=0, le=30)
    inquiry_id: str | None = Field(default=None, max_length=20, pattern=r"^INQ-[A-Z0-9]{4,16}$")


class DraftFollowupArgs(_Strict):
    inquiry_id: str = InquiryId
    followup_type: FollowUpTypeLit
    tone: Literal["professional", "warm", "concise"] = "professional"


class StatusChangeArgs(_Strict):
    inquiry_id: str = InquiryId
    target_status: ProposableStatus
    reason: str = Field(min_length=3, max_length=300)


class RecommendationDecisionArgs(_Strict):
    recommendation_id: str = Field(min_length=5, max_length=20, pattern=r"^REC-[A-Z0-9]{4,16}$")
    decision: Literal["accepted", "dismissed", "irrelevant"]
    feedback: str | None = Field(default=None, max_length=500)


class ConfirmBookingArgs(_Strict):
    quote_id: str = Field(min_length=5, max_length=20, pattern=r"^QTE-[A-Z0-9]{4,16}$")
    notes: str | None = Field(default=None, max_length=500)


PeriodLit = Literal["this_week", "last_week", "this_month", "last_month", "last_30_days", "this_quarter", "this_year",
                   "all_time"]
DateStr = Field(default=None, description="YYYY-MM-DD; give both start_date and end_date for a custom range",
                pattern=r"^\d{4}-\d{2}-\d{2}$")
InquiryStatusLit = Literal["new", "needs_information", "qualified", "quoted", "booked", "completed", "closed"]
EmailTypeLit = Literal["initial_response", "missing_information", "quote_followup", "booking_confirmation", "general_update",
                       "alternatives", "accommodation", "additional_services", "post_interaction", "post_trip"]


class PeriodArgs(_Strict):
    period: PeriodLit = Field(default="this_month", description="Reporting period (ignored if a custom range is given)")
    start_date: str | None = DateStr
    end_date: str | None = DateStr


class GetInquiriesArgs(_Strict):
    status: InquiryStatusLit | None = Field(default=None, description=(
        "Filter by CURRENT status only. Inquiries change status after they arrive, so to count inquiries received in "
        "a date range (e.g. 'new inquiries in October') use received_from/received_to and leave status empty."))
    needs_attention_only: bool = Field(default=False, description="Only inquiries with something to do")
    query: str = Field(default="", max_length=80, description="Text to match in customer name, ID or route")
    received_from: str | None = DateStr
    received_to: str | None = DateStr
    limit: int = Field(default=25, ge=1, le=100)


class FollowUpsDueArgs(_Strict):
    within_days: int = Field(default=7, ge=0, le=30)


class GetQuotesArgs(_Strict):
    status: Literal["received", "presented", "accepted", "declined", "expired", "withdrawn"] | None = None
    inquiry_id: str | None = Field(default=None, max_length=20, pattern=r"^INQ-[A-Z0-9]{4,16}$")
    limit: int = Field(default=15, ge=1, le=30)


class SearchCustomersArgs(_Strict):
    query: str = Field(default="", max_length=80)
    limit: int = Field(default=25, ge=1, le=100)


class BookingSummaryArgs(PeriodArgs):
    trip_dates_from: str | None = Field(default=None, description="List bookings whose TRIP departs on/after this date "
                                                                  "(YYYY-MM-DD; future dates allowed)", max_length=10)
    trip_dates_to: str | None = Field(default=None, description="…and on/before this date (YYYY-MM-DD)", max_length=10)


class DraftEmailArgs(_Strict):
    inquiry_id: str = InquiryId
    email_type: EmailTypeLit
    tone: Literal["professional", "warm", "concise"] = "professional"
    instructions: str | None = Field(default=None, max_length=300,
                                     description="Optional owner guidance, e.g. 'mention we can hold the price until Friday'")


class MarkFollowUpArgs(_Strict):
    followup_id: str = Field(min_length=5, max_length=20, pattern=r"^FUP-[A-Z0-9]{4,16}$")
    note: str | None = Field(default=None, max_length=300)


class PrepareInquiryArgs(_Strict):
    message: str = Field(min_length=5, max_length=4000, description="The customer's message, verbatim")
    customer_name: str | None = Field(default=None, max_length=80)


# --------------------------------------------------------------------------- handlers (READ)
def _get_inquiry(ctx: ToolContext, a: GetInquiryArgs) -> dict[str, Any]:
    inq = get_inquiry(ctx.session, a.inquiry_id)
    d = inquiry_to_dict(inq)
    d["customer_message_untrusted"] = (inq.original_text or "")[:1500]
    return d


def _search(ctx: ToolContext, a: SearchInquiriesArgs) -> dict[str, Any]:
    rows = search_inquiries(ctx.session, a.query, a.status, a.limit)
    return {"results": [{k: v for k, v in inquiry_to_dict(i).items() if k in (
        "inquiry_id", "customer_label", "status", "origin", "destination", "departure_date", "passenger_count")} for i in rows]}


def _missing(ctx: ToolContext, a: InquiryOnlyArgs) -> dict[str, Any]:
    inq = get_inquiry(ctx.session, a.inquiry_id)
    return {"inquiry_id": inq.id, "missing_information": list(inq.missing_information or [])}


def _list_quotes(ctx: ToolContext, a: InquiryOnlyArgs) -> dict[str, Any]:
    get_inquiry(ctx.session, a.inquiry_id)
    return {"quotes": [quote_to_dict(q) for q in list_quotes_for_inquiry(ctx.session, a.inquiry_id)],
            "note": "All quotes are fictional demonstration records."}


def _compare(ctx: ToolContext, a: CompareQuotesArgs) -> dict[str, Any]:
    inq = get_inquiry(ctx.session, a.inquiry_id)
    comp = compare_quotes(inq, list_quotes_for_inquiry(ctx.session, a.inquiry_id), a.comparison_currency)
    return {
        "comparison_currency": comp.comparison_currency, "lowest_quote_id": comp.lowest_quote_id,
        "lowest_price": comp.lowest_price, "notes": comp.notes,
        "quotes": [{"quote_id": x.quote_id, "operator": x.operator_label, "price": x.total_price, "currency": x.currency,
                    "comparable": x.comparable, "capacity": x.capacity_status, "budget": x.budget_status,
                    "advantages": x.advantages, "disadvantages": x.disadvantages,
                    "operator_questions": x.operator_questions} for x in comp.assessments],
    }


def _metrics(ctx: ToolContext, a: BusinessMetricsArgs) -> dict[str, Any]:
    try:
        start = date.fromisoformat(a.start_date) if a.start_date else None
        end = date.fromisoformat(a.end_date) if a.end_date else None
    except ValueError as exc:
        raise ValidationFailure("Dates must be YYYY-MM-DD.") from exc
    m = compute_metrics(ctx.session, DashboardFilters(start=start, end=end))
    return {"counts": m.counts, "money_by_currency": m.money,
            "ratios": {k: {"numerator": r.numerator, "denominator": r.denominator, "value": r.value} for k, r in m.ratios.items()},
            "avg_days_to_confirmation": m.avg_days_to_confirmation,
            "note": "Opportunity and quoted values are not revenue. Currencies are never summed together."}


def _due(ctx: ToolContext, a: DueFollowupsArgs) -> dict[str, Any]:
    rows = list_due_followups(ctx.session, a.within_days)
    if a.inquiry_id:
        rows = [r for r in rows if r.inquiry_id == a.inquiry_id]
    elif ctx.inquiry_scope:
        rows = [r for r in rows if r.inquiry_id == ctx.inquiry_scope]
    return {"followups": [{k: v for k, v in followup_to_dict(r).items() if k != "body"} for r in rows]}


def _period(key: str | PeriodArgs) -> Any:
    from src.services.dashboard_service import period_from, resolve_period

    if isinstance(key, str):
        return resolve_period(key)
    try:
        start = date.fromisoformat(key.start_date) if key.start_date else None
        end = date.fromisoformat(key.end_date) if key.end_date else None
        if (start is None) != (end is None):
            raise ValidationFailure("Give both start_date and end_date for a custom range.")
        return period_from(key.period, start, end)
    except ValueError as exc:
        raise ValidationFailure(f"Invalid date range: {exc}") from exc


def _dashboard_summary(ctx: ToolContext, a: PeriodArgs) -> dict[str, Any]:
    from src.services.dashboard_service import needs_attention, plain_summary, summary_facts

    facts = summary_facts(ctx.session, _period(a))
    items = needs_attention(ctx.session)
    return {"facts": facts, "summary_sentences": plain_summary(facts),
            "attention_items_most_urgent_first": [{k: v for k, v in i.as_dict().items() if v is not None} for i in items[:12]],
            "attention_items_total": len(items)}


def _inquiries(ctx: ToolContext, a: GetInquiriesArgs) -> dict[str, Any]:
    from src.auth import has_permission
    from src.domain import OPEN_INQUIRY_STATUSES
    from src.services.dashboard_service import needs_attention

    try:
        lo = date.fromisoformat(a.received_from) if a.received_from else None
        hi = date.fromisoformat(a.received_to) if a.received_to else None
    except ValueError as exc:
        raise ValidationFailure(f"Invalid date: {exc}") from exc
    rows = search_inquiries(ctx.session, a.query, a.status, 1000, received_from=lo, received_to=hi)
    if a.needs_attention_only:
        wanted = {i.inquiry_id for i in needs_attention(ctx.session) if i.inquiry_id}
        rows = [r for r in rows if r.id in wanted]
    open_values = {s.value for s in OPEN_INQUIRY_STATUSES}
    show_money = has_permission(AuthPermission.VIEW_FINANCIALS)
    out = []
    for r in rows[: a.limit]:
        d = inquiry_to_dict(r)
        row = {k: d[k] for k in ("inquiry_id", "customer_label", "status", "origin", "destination", "departure_date",
                                 "departure_earliest", "departure_latest", "passenger_count", "missing_information",
                                 "created_at")}
        fus = [f for f in r.followups if f.status != "cancelled"]
        row["follow_ups_total"] = len(fus)
        row["follow_ups_open"] = sum(1 for f in fus if f.status not in ("completed", "cancelled"))
        row["is_open"] = r.status in open_values
        row["booking_statuses"] = [b.status for b in r.bookings]  # e.g. ["cancelled"] — values via get_booking_summary
        if show_money:
            row["estimated_opportunity_value"] = r.estimated_opportunity_value
            row["currency"] = r.currency
        else:
            row["estimated_opportunity_value"] = RESTRICTED
        out.append(row)
    return {"inquiries": out, "count": len(out), "total_matching": len(rows),
            "open_matching": sum(1 for r in rows if r.status in open_values),
            "definitions": {"open": "status new, needs_information, qualified or quoted (booked, completed and closed are "
                                    "not open)", "created_at": "when the inquiry was received"}}


def _inquiry_details(ctx: ToolContext, a: GetInquiryArgs) -> dict[str, Any]:
    from src.services.followup_service import list_followups
    from src.services.recommendation_service import list_recommendations

    inq = get_inquiry(ctx.session, a.inquiry_id)
    d = inquiry_to_dict(inq)
    d["customer_message_untrusted"] = (inq.original_text or "")[:1500]
    d["quotes"] = [quote_to_dict(q) for q in list_quotes_for_inquiry(ctx.session, inq.id)]
    d["follow_ups"] = [{k: v for k, v in followup_to_dict(f).items() if k != "body"} for f in list_followups(ctx.session, inq.id)]
    d["open_suggestions"] = [{"recommendation_id": r.id, "action": r.suggested_action, "why": r.explanation}
                             for r in list_recommendations(ctx.session, inq.id, ["open"])]
    from src.auth import has_permission

    show_money = has_permission(AuthPermission.VIEW_FINANCIALS)
    d["bookings"] = [{"booking_id": b.id, "status": b.status,
                      **({"value": b.booked_value, "currency": b.currency} if show_money else {"value": RESTRICTED})}
                     for b in inq.bookings]
    if not show_money:
        d["estimated_opportunity_value"] = RESTRICTED
    return d


def _followups_due(ctx: ToolContext, a: FollowUpsDueArgs) -> dict[str, Any]:
    from src.models import utcnow

    rows = list_due_followups(ctx.session, a.within_days)
    today = utcnow().date()

    def due_status(r: Any) -> str:
        if r.due_at is None:
            return "no due date"
        d = r.due_at.date()
        return "overdue" if d < today else ("due today" if d == today else "upcoming")

    out = [{**{k: v for k, v in followup_to_dict(r).items() if k != "body"}, "customer": r.inquiry.customer_label,
            "due_status": due_status(r)} for r in rows]
    return {"followups": out, "counts": {s: sum(1 for x in out if x["due_status"] == s)
                                         for s in ("overdue", "due today", "upcoming")}}


def _quotes(ctx: ToolContext, a: GetQuotesArgs) -> dict[str, Any]:
    from sqlalchemy import select

    from src.models import Quote

    stmt = select(Quote).order_by(Quote.created_at.desc())
    if a.status:
        stmt = stmt.where(Quote.status == a.status)
    if a.inquiry_id:
        stmt = stmt.where(Quote.inquiry_id == a.inquiry_id)
    rows = list(ctx.session.scalars(stmt.limit(a.limit)))
    return {"quotes": [{**quote_to_dict(q), "customer": q.inquiry.customer_label} for q in rows],
            "note": "Quotes are manually entered (fictional demo data). They are not revenue."}


def _booking_summary(ctx: ToolContext, a: BookingSummaryArgs) -> dict[str, Any]:
    from collections import Counter, defaultdict

    from sqlalchemy import select

    from src.domain import VALUE_COUNTED_BOOKING_STATUSES
    from src.models import Booking
    from src.services.dashboard_service import PROFIT_NOTE, business_kpis

    bookings = list(ctx.session.scalars(select(Booking)))
    if a.trip_dates_from or a.trip_dates_to:  # bookings by when the trip happens (future dates allowed)
        try:
            lo = date.fromisoformat(a.trip_dates_from) if a.trip_dates_from else None
            hi = date.fromisoformat(a.trip_dates_to) if a.trip_dates_to else None
        except ValueError as exc:
            raise ValidationFailure(f"Invalid date: {exc}") from exc
        counted = {s.value for s in VALUE_COUNTED_BOOKING_STATUSES}
        trips = []
        totals: dict[str, float] = defaultdict(float)
        for b in bookings:
            dep = b.inquiry.departure_date or b.inquiry.departure_earliest
            if dep is None or (lo and dep < lo) or (hi and dep > hi):
                continue
            trips.append({"booking_id": b.id, "customer": b.inquiry.customer_label, "status": b.status,
                          "route": f"{b.inquiry.origin or '?'} → {b.inquiry.destination or '?'}",
                          "departure_date": dep.isoformat(), "value": b.booked_value, "currency": b.currency})
            if b.status in counted:
                totals[b.currency] += b.booked_value
        return {"trip_dates": f"{a.trip_dates_from or 'any'} to {a.trip_dates_to or 'any'}", "bookings": trips,
                "value_by_currency_confirmed_or_completed": {c: round(v, 2) for c, v in totals.items()},
                "notes": ["Cancelled bookings are listed but excluded from value.", PROFIT_NOTE]}
    p = _period(a)
    k = business_kpis(ctx.session, p)
    statuses = Counter(b.status for b in bookings if p.contains(b.created_at))
    return {"period": p.label, "period_dates": p.describe(), "bookings_by_status_created_in_period": dict(statuses),
            "cancelled_bookings_in_period": statuses.get("cancelled", 0),
            "confirmed_bookings_in_period": k.confirmed_bookings, "booked_value_by_currency": k.booked_value,
            "booking_conversion": k.as_dict()["booking_conversion"],
            "notes": [PROFIT_NOTE, "Cancelled bookings are excluded from value.",
                      "start_date/end_date filter by when bookings were made; use trip_dates_from/trip_dates_to for "
                      "when trips happen."]}


def _performance(ctx: ToolContext, a: PeriodArgs) -> dict[str, Any]:
    from src.services.dashboard_service import business_kpis, trend_frame

    p = _period(a)
    df = trend_frame(ctx.session, p)
    trend = [{"bucket_start": str(r["bucket"]), "series": r["series"], "count": int(r["count"])} for _, r in df.iterrows()]
    return {"kpis": business_kpis(ctx.session, p).as_dict(), "trend": trend}


def _quote_pdf(ctx: ToolContext, a: QuotePdfArgs) -> dict[str, Any]:
    """Checks a customer PDF can be made; the chat shows the download button. Nothing is sent to anyone."""
    from src.services.document_service import proposal_facts

    facts = proposal_facts(ctx.session, a.inquiry_id, show_operator_names=a.show_operator_names)
    return {"pdf_ready": True, "inquiry_id": a.inquiry_id, "customer": facts["customer"],
            "options": len(facts["options"]), "currencies": sorted({o["currency"] for o in facts["options"]}),
            "operator_names_shown": a.show_operator_names, "left_out_expired_quotes": len(facts["left_out_expired"]),
            "note": "A 'Download PDF' button is shown under your answer. The PDF lists recorded quotes only, says nothing "
                    "is booked, and is not sent to anyone."}


def _search_customers(ctx: ToolContext, a: SearchCustomersArgs) -> dict[str, Any]:
    from src.auth import has_permission
    from src.services.customer_service import customer_summaries

    rows = customer_summaries(ctx.session, a.query, a.limit)
    show_money = has_permission(AuthPermission.VIEW_FINANCIALS)
    return {"customers": [{**r, "last_activity": r["last_activity"].isoformat(),
                           **({} if show_money else {"booked_value": RESTRICTED})} for r in rows]}


def _prepare_inquiry(ctx: ToolContext, a: PrepareInquiryArgs) -> dict[str, Any]:
    """Runs extraction into a review workflow. Nothing is saved until the owner reviews it on the Inquiries page."""
    from src.ai.client import build_ai_client
    from src.ai.extraction import METHOD_OPENAI, METHOD_RULE_BASED
    from src.exceptions import AINotConfigured
    from src.models import utcnow
    from src.workflows.workflow_engine import start_intake

    if ctx.settings is None:
        raise ValidationFailure("Settings unavailable.")
    try:
        client = build_ai_client(ctx.settings)
        method = METHOD_OPENAI
    except AINotConfigured:
        client, method = None, METHOD_RULE_BASED
    res = start_intake(ctx.session, ctx.settings, text=a.message, customer_label=(a.customer_name or "New customer")[:80],
                       reference_date=utcnow().date(), method=method, client=client)
    if not res.ok:
        raise ValidationFailure(f"Could not read the message: {res.message}")
    ex = res.data["extraction"]
    return {"review_run_id": res.run_id, "status": "ready_for_owner_review",
            "extracted": {"customer_name": ex.customer_name, "origin": ex.origin, "destination": ex.destination,
                          "departure": ex.departure.display(), "passengers": ex.passenger_count},
            "missing_information": [m.field for m in ex.missing_information],
            "note": "Not saved yet. The owner must review and save it on the Inquiries page."}


# --------------------------------------------------------------------------- handlers (PROPOSE) + executors
def _propose_email(ctx: ToolContext, a: DraftEmailArgs) -> dict[str, Any]:
    from src.ai.client import build_ai_client
    from src.ai.drafting import llm_draft
    from src.exceptions import AINotConfigured, AIServiceError

    facts = build_followup_facts(ctx.session, a.inquiry_id)
    if a.instructions:
        facts["owner_guidance_untrusted"] = a.instructions
    ftype, tone = FollowUpType(a.email_type), Tone(a.tone)
    note = ""
    try:
        if ctx.settings is None:
            raise AINotConfigured("no settings")
        draft, _, _ = llm_draft(facts, ftype, tone, build_ai_client(ctx.settings))
    except AINotConfigured:
        draft = template_draft(facts, ftype, tone)
        note = "Written from a template (AI drafting is not configured)."
    except AIServiceError as exc:
        draft = template_draft(facts, ftype, tone)
        note = f"AI drafting failed ({exc.category}); written from a template instead."
    return {"summary": f"Save a '{a.email_type.replace('_', ' ')}' email draft for {facts['inquiry']['customer_label']}",
            "rationale": note or "Draft written from the inquiry's records.",
            "draft_subject": draft.subject, "draft_warnings": draft.warnings,
            "proposal_payload": {"draft_subject": draft.subject, "draft_body": draft.body, "draft_warnings": draft.warnings,
                                 "draft_method": draft.method, "draft_note": note}}


def _exec_email(ctx: ToolContext, a: DraftEmailArgs, payload: dict[str, Any]) -> dict[str, Any]:
    from datetime import timedelta

    from src.models import utcnow

    f = create_followup(ctx.session, FollowUpCreate(
        inquiry_id=a.inquiry_id, followup_type=FollowUpType(a.email_type), tone=Tone(a.tone),
        subject=payload["draft_subject"], body=payload["draft_body"], due_at=utcnow() + timedelta(days=1),
        generation_method=payload.get("draft_method", "template"),
        generation_metadata={"proposed_by_workflow": ctx.recorder.run.id if ctx.recorder else None,
                             "warnings": payload.get("draft_warnings", [])}), actor="advisor")
    return {"followup_id": f.id, "status": f.status, "note": "Saved as a draft. Nothing was sent."}


def _propose_mark_done(ctx: ToolContext, a: MarkFollowUpArgs) -> dict[str, Any]:
    from src.domain import FOLLOWUP_TRANSITIONS, FollowUpStatus
    from src.services.followup_service import get_followup

    f = get_followup(ctx.session, a.followup_id)
    if FollowUpStatus.COMPLETED not in FOLLOWUP_TRANSITIONS[FollowUpStatus(f.status)]:
        raise ValidationFailure(f"Follow-up {f.id} is '{f.status}' and cannot be marked done.")
    return {"summary": f"Mark follow-up '{f.subject}' for {f.inquiry.customer_label} as done", "rationale": a.note or ""}


def _exec_mark_done(ctx: ToolContext, a: MarkFollowUpArgs, payload: dict[str, Any]) -> dict[str, Any]:
    from src.services.followup_service import change_followup_status

    f = change_followup_status(ctx.session, a.followup_id, "completed", notes=a.note, actor="advisor")
    return {"followup_id": f.id, "status": f.status}



def _propose_draft(ctx: ToolContext, a: DraftFollowupArgs) -> dict[str, Any]:
    facts = build_followup_facts(ctx.session, a.inquiry_id)
    draft = template_draft(facts, FollowUpType(a.followup_type), Tone(a.tone))
    return {"summary": f"Save a '{a.followup_type}' follow-up draft",
            "rationale": "Draft generated from verified records; saving requires advisor approval.",
            "draft_subject": draft.subject, "draft_warnings": draft.warnings,
            "proposal_payload": {"draft_subject": draft.subject, "draft_body": draft.body,
                                 "draft_warnings": draft.warnings, "due_at": None}}


def _exec_draft(ctx: ToolContext, a: DraftFollowupArgs, payload: dict[str, Any]) -> dict[str, Any]:
    f = create_followup(ctx.session, FollowUpCreate(
        inquiry_id=a.inquiry_id, followup_type=FollowUpType(a.followup_type), tone=Tone(a.tone),
        subject=payload["draft_subject"], body=payload["draft_body"], generation_method="template",
        generation_metadata={"proposed_by_workflow": ctx.recorder.run.id if ctx.recorder else None,
                             "warnings": payload.get("draft_warnings", [])}), actor="advisor")
    return {"followup_id": f.id, "status": f.status}


def _propose_status(ctx: ToolContext, a: StatusChangeArgs) -> dict[str, Any]:
    inq = get_inquiry(ctx.session, a.inquiry_id)
    ensure_transition(INQUIRY_TRANSITIONS, InquiryStatus(inq.status), InquiryStatus(a.target_status), f"Inquiry {inq.id}")
    return {"summary": f"Change inquiry status {inq.status} → {a.target_status}", "rationale": a.reason}


def _exec_status(ctx: ToolContext, a: StatusChangeArgs, payload: dict[str, Any]) -> dict[str, Any]:
    inq = change_inquiry_status(ctx.session, a.inquiry_id, a.target_status, actor="advisor",
                                reason=f"Approved proposal: {a.reason}")
    return {"inquiry_id": inq.id, "status": inq.status}


def _check_rec_scope(ctx: ToolContext, rec_id: str) -> Recommendation:
    rec = ctx.session.get(Recommendation, rec_id)
    if rec is None:
        raise NotFound(f"Recommendation {rec_id} not found.")
    if ctx.inquiry_scope and rec.inquiry_id != ctx.inquiry_scope:
        raise PermissionDenied("Recommendation belongs to a different inquiry.")
    return rec


def _propose_rec_decision(ctx: ToolContext, a: RecommendationDecisionArgs) -> dict[str, Any]:
    rec = _check_rec_scope(ctx, a.recommendation_id)
    if rec.status != "open":
        raise ValidationFailure(f"Recommendation already decided ({rec.status}).")
    return {"summary": f"Mark recommendation {rec.id} as {a.decision}", "rationale": a.feedback or ""}


def _exec_rec_decision(ctx: ToolContext, a: RecommendationDecisionArgs, payload: dict[str, Any]) -> dict[str, Any]:
    rec = decide_recommendation(ctx.session, a.recommendation_id, a.decision, feedback=a.feedback, actor="advisor")
    return {"recommendation_id": rec.id, "status": rec.status}


def _propose_booking(ctx: ToolContext, a: ConfirmBookingArgs) -> dict[str, Any]:
    return {"summary": f"Record confirmed (fictional) booking for quote {a.quote_id}",
            "rationale": "Advisor requested booking confirmation."}


def _exec_booking(ctx: ToolContext, a: ConfirmBookingArgs, payload: dict[str, Any]) -> dict[str, Any]:
    b = confirm_booking_from_quote(ctx.session, a.quote_id, actor="advisor", notes=a.notes,
                                   approved_via_workflow=ctx.recorder.run.id if ctx.recorder else None)
    return {"booking_id": b.id, "status": b.status, "booked_value": b.booked_value, "currency": b.currency}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    R, P, INTERNAL = Permission.READ, Permission.PROPOSE, Permission.INTERNAL
    reg.register(ToolSpec("get_inquiry", "Read one inquiry's structured fields and (untrusted) customer message.",
                          GetInquiryArgs, R, _get_inquiry))
    reg.register(ToolSpec("search_inquiries", "Search inquiries by text in ID/customer label/route, optionally by status.",
                          SearchInquiriesArgs, R, _search))
    reg.register(ToolSpec("find_missing_information", "List required information still missing for an inquiry.",
                          InquiryOnlyArgs, R, _missing))
    reg.register(ToolSpec("list_quotes_for_inquiry", "List the fictional quotes recorded for an inquiry.",
                          InquiryOnlyArgs, R, _list_quotes))
    reg.register(ToolSpec("compare_quotes", "Deterministic comparison of an inquiry's quotes (price, capacity, budget, services).",
                          CompareQuotesArgs, R, _compare))
    reg.register(ToolSpec("get_business_metrics", "Business metrics computed from records for a date range.",
                          BusinessMetricsArgs, R, _metrics, requires=AuthPermission.VIEW_FINANCIALS))
    reg.register(ToolSpec("list_due_followups", "Open follow-ups that are overdue or due within N days.",
                          DueFollowupsArgs, R, _due))
    reg.register(ToolSpec("draft_followup", "Propose saving a follow-up draft built from verified records (needs advisor approval).",
                          DraftFollowupArgs, P, _propose_draft, _exec_draft, mutates=True, requires=AuthPermission.EDIT_OPERATIONS))
    reg.register(ToolSpec("propose_inquiry_status_change",
                          "Propose an inquiry status change to needs_information, qualified or closed (needs advisor approval).",
                          StatusChangeArgs, P, _propose_status, _exec_status, mutates=True, requires=AuthPermission.EDIT_OPERATIONS))
    reg.register(ToolSpec("record_recommendation_decision",
                          "Propose recording a decision on a recommendation (needs advisor approval).",
                          RecommendationDecisionArgs, P, _propose_rec_decision, _exec_rec_decision, mutates=True, requires=AuthPermission.EDIT_OPERATIONS))
    reg.register(ToolSpec("confirm_booking", "Application-only: record a confirmed fictional booking for a quote.",
                          ConfirmBookingArgs, INTERNAL, _propose_booking, _exec_booking, mutates=True, requires=AuthPermission.MANAGE_BOOKINGS))
    # Business-owner assistant tools
    reg.register(ToolSpec("get_dashboard_summary", "Headline numbers for a reporting period plus the items needing "
                          "attention now, most urgent first, with customer names.",
                          PeriodArgs, R, _dashboard_summary))
    reg.register(ToolSpec("get_inquiries", "List inquiries, optionally by status, text search, or only those needing attention.",
                          GetInquiriesArgs, R, _inquiries))
    reg.register(ToolSpec("get_inquiry_details", "Full details of one inquiry: trip, customer contact, quotes, follow-ups, suggestions.",
                          GetInquiryArgs, R, _inquiry_details))
    reg.register(ToolSpec("get_follow_ups_due", "Open follow-ups that are overdue or due within N days.",
                          FollowUpsDueArgs, R, _followups_due))
    reg.register(ToolSpec("get_quotes", "List manually entered quotes, optionally by status (received = not yet sent, "
                          "presented = sent to customer) or inquiry.", GetQuotesArgs, R, _quotes))
    reg.register(ToolSpec("get_booking_summary", "Bookings for a reporting period: counts by status (including "
                          "cancellations), booked value per currency and conversion. With trip_dates_from/to it lists "
                          "bookings by when the trip happens.",
                          BookingSummaryArgs, R, _booking_summary, requires=AuthPermission.VIEW_FINANCIALS))
    reg.register(ToolSpec("get_business_performance", "KPIs plus weekly/monthly trend of inquiries, quotes sent and bookings.",
                          PeriodArgs, R, _performance))
    reg.register(ToolSpec("search_customers", "Find customers by name, email or company, with inquiry counts and booked value.",
                          SearchCustomersArgs, R, _search_customers))
    reg.register(ToolSpec("prepare_inquiry", "Read a pasted customer message into a new inquiry for the owner to review. "
                          "Nothing is saved until the owner reviews it.", PrepareInquiryArgs, R, _prepare_inquiry, requires=AuthPermission.EDIT_OPERATIONS))
    reg.register(ToolSpec("prepare_quote_pdf", "Prepare a customer-facing PDF of an inquiry's recorded quote options "
                          "(download only — never sent).", QuotePdfArgs, R, _quote_pdf))
    reg.register(ToolSpec("draft_email", "Propose saving an email draft for an inquiry (owner approves; never sent).",
                          DraftEmailArgs, P, _propose_email, _exec_email, mutates=True, requires=AuthPermission.EDIT_OPERATIONS))
    reg.register(ToolSpec("mark_follow_up_complete", "Propose marking a follow-up as done (owner approves).",
                          MarkFollowUpArgs, P, _propose_mark_done, _exec_mark_done, mutates=True, requires=AuthPermission.EDIT_OPERATIONS))
    return reg


REGISTRY = build_registry()

TRIAGE_ALLOWLIST = {"get_inquiry", "find_missing_information", "list_quotes_for_inquiry", "compare_quotes",
                    "list_due_followups", "draft_followup", "propose_inquiry_status_change", "record_recommendation_decision"}
ASSISTANT_ALLOWLIST = {"get_dashboard_summary", "get_inquiries", "get_inquiry_details", "get_follow_ups_due", "get_quotes",
                       "get_booking_summary", "get_business_performance", "search_customers", "compare_quotes",
                       "prepare_inquiry", "draft_email", "mark_follow_up_complete", "propose_inquiry_status_change",
                       "record_recommendation_decision", "prepare_quote_pdf"}
BOOKING_ALLOWLIST = {"confirm_booking"}
