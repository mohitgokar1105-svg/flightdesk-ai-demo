"""Quote recording and deterministic quote comparison.

All arithmetic happens here in Python. Quotes in different currencies are never compared on price
(no conversion process is configured). Nothing here claims a quote is available, valid or bookable.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Permission, require
from src.domain import (
    INQUIRY_TRANSITIONS,
    QUOTE_COMPARABLE_STATUSES,
    QUOTE_TRANSITIONS,
    InquiryStatus,
    QuoteStatus,
    ensure_transition,
)
from src.exceptions import DuplicateRecord, PermissionDenied, ValidationFailure
from src.models import Inquiry, Quote, utcnow
from src.schemas import QuoteCreate
from src.services.audit_service import record_audit
from src.services.inquiry_service import get_inquiry

SERVICE_LABELS = {
    "accommodation": "accommodation", "ground_transport": "ground transport", "catering": "catering",
    "pet_travel": "pet travel", "special_luggage": "special luggage", "experiences": "experiences", "wifi": "Wi-Fi",
    "landing_and_handling_fees": "landing & handling fees", "taxes": "taxes", "de_icing": "de-icing",
    "crew_overnight": "crew overnight costs", "positioning": "positioning",
}


def quote_dedupe_key(data: QuoteCreate) -> str:
    raw = "|".join([data.inquiry_id, data.operator_label.strip().lower(), f"{data.total_price:.2f}", data.currency,
                    (data.aircraft_type or "").strip().lower()])
    return hashlib.sha256(raw.encode()).hexdigest()


def create_quote(session: Session, data: QuoteCreate, *, actor: str = "advisor", is_sample: bool = False,
                 status: QuoteStatus = QuoteStatus.RECEIVED) -> Quote:
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model cannot record quotes.")
    inquiry = get_inquiry(session, data.inquiry_id)
    if inquiry.status in (InquiryStatus.COMPLETED.value, InquiryStatus.CLOSED.value):
        raise ValidationFailure(f"Inquiry {inquiry.id} is {inquiry.status}; quotes cannot be added.")
    key = quote_dedupe_key(data)
    if session.scalar(select(Quote.id).where(Quote.dedupe_key == key)):
        raise DuplicateRecord("An identical quote (operator, price, currency, aircraft) is already recorded for this inquiry.")
    quote = Quote(**data.model_dump(), status=status.value, dedupe_key=key, is_sample=is_sample)
    session.add(quote)
    session.flush()
    record_audit(session, "quote_recorded", actor=actor, entity_type="quote", entity_id=quote.id,
                 metadata={"inquiry_id": inquiry.id, "currency": quote.currency})
    from src.services.rfq_service import link_quote_to_request

    link_quote_to_request(session, inquiry.id, quote.operator_label, quote.id)  # "Operator replied" if we asked them
    return quote


def _advance_inquiry_to_quoted(session: Session, inquiry: Inquiry, actor: str) -> None:
    """Presenting a quote implies the inquiry is qualified and quoted. Each hop is validated + audited."""
    path = {InquiryStatus.NEW: [InquiryStatus.QUALIFIED, InquiryStatus.QUOTED],
            InquiryStatus.QUALIFIED: [InquiryStatus.QUOTED]}
    for target in path.get(InquiryStatus(inquiry.status), []):
        ensure_transition(INQUIRY_TRANSITIONS, InquiryStatus(inquiry.status), target, f"Inquiry {inquiry.id}")
        previous = inquiry.status
        inquiry.status = target.value
        record_audit(session, "inquiry_status_changed", actor=actor, entity_type="inquiry", entity_id=inquiry.id,
                     metadata={"from": previous, "to": target.value, "reason": "quote presented"})


def change_quote_status(session: Session, quote_id: str, target: QuoteStatus | str, *, actor: str = "advisor",
                        via_booking_service: bool = False) -> Quote:
    require(Permission.EDIT_OPERATIONS)
    target = QuoteStatus(target)
    if actor == "model":
        raise PermissionDenied("The model cannot change quote status.")
    if target == QuoteStatus.ACCEPTED and not via_booking_service:
        raise PermissionDenied("A quote becomes 'accepted' only through the booking confirmation workflow.")
    quote = session.get(Quote, quote_id)
    if quote is None:
        raise ValidationFailure(f"Quote {quote_id} not found.")
    current = QuoteStatus(quote.status)
    if current == QuoteStatus.ACCEPTED and not via_booking_service:
        raise PermissionDenied("An accepted quote changes only when its booking is cancelled.")
    ensure_transition(QUOTE_TRANSITIONS, current, target, f"Quote {quote_id}")
    quote.status = target.value
    quote.updated_at = utcnow()
    if target == QuoteStatus.PRESENTED:
        quote.presented_at = quote.updated_at
        _advance_inquiry_to_quoted(session, quote.inquiry, actor)
    session.flush()
    record_audit(session, "quote_status_changed", actor=actor, entity_type="quote", entity_id=quote_id,
                 metadata={"from": current.value, "to": target.value})
    return quote


def list_quotes_for_inquiry(session: Session, inquiry_id: str) -> list[Quote]:
    require(Permission.VIEW_OPERATIONS)
    return list(session.scalars(select(Quote).where(Quote.inquiry_id == inquiry_id).order_by(Quote.created_at)))


def quote_to_dict(q: Quote) -> dict[str, Any]:
    return {
        "quote_id": q.id, "inquiry_id": q.inquiry_id, "operator_label": q.operator_label, "total_price": q.total_price,
        "additional_fees": q.additional_fees, "fees_description": q.fees_description, "all_in_price": q.all_in_price,
        "presented_at": q.presented_at.isoformat() if q.presented_at else None, "currency": q.currency, "aircraft_type": q.aircraft_type, "passenger_capacity": q.passenger_capacity,
        "included_services": list(q.included_services or []), "excluded_services": list(q.excluded_services or []),
        "restrictions": q.restrictions, "valid_until": q.valid_until.isoformat() if q.valid_until else None,
        "status": q.status, "notes": q.notes, "source_description": q.source_description,
        "created_at": q.created_at.isoformat(), "is_sample": q.is_sample,
    }


# ---------------------------------------------------------------------------
# Deterministic comparison
# ---------------------------------------------------------------------------
@dataclass
class QuoteAssessment:
    quote_id: str
    operator_label: str
    total_price: float  # all-in: quoted total + documented additional fees
    currency: str
    aircraft_type: str | None
    passenger_capacity: int | None
    status: str
    comparable: bool
    exclusion_reason: str | None
    capacity_status: str  # sufficient | insufficient | unknown
    budget_status: str  # within | over | no_budget | currency_mismatch
    base_price: float = 0.0
    additional_fees: float | None = None
    is_lowest: bool = False
    difference_from_lowest: float | None = None
    difference_from_lowest_pct: float | None = None
    requested_included: list[str] = field(default_factory=list)
    requested_excluded: list[str] = field(default_factory=list)
    requested_unknown: list[str] = field(default_factory=list)
    advantages: list[str] = field(default_factory=list)
    disadvantages: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    operator_questions: list[str] = field(default_factory=list)
    restrictions: str | None = None


@dataclass
class QuoteComparison:
    inquiry_id: str
    comparison_currency: str | None
    currency_basis: str
    passenger_count: int | None
    budget_amount: float | None
    budget_currency: str | None
    requested_services: list[str]
    assessments: list[QuoteAssessment]
    lowest_quote_id: str | None
    lowest_price: float | None
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def choose_comparison_currency(inquiry_currency: str | None, quote_currencies: list[str]) -> tuple[str | None, str]:
    """Inquiry currency if any quote uses it, else the most common quote currency (ties: alphabetical)."""
    if not quote_currencies:
        return None, "no quotes"
    if inquiry_currency and inquiry_currency in quote_currencies:
        return inquiry_currency, "inquiry currency"
    counts = Counter(quote_currencies)
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, "most common quote currency"


def compare_quotes(inquiry: Inquiry, quotes: list[Quote], comparison_currency: str | None = None) -> QuoteComparison:
    """Compare quotes for an inquiry using only stored data. Pure function (no DB writes)."""
    requested = list(inquiry.requested_services or [])
    pax = inquiry.passenger_count
    candidates = [q for q in quotes if QuoteStatus(q.status) in QUOTE_COMPARABLE_STATUSES]
    if comparison_currency is None:
        comparison_currency, basis = choose_comparison_currency(inquiry.currency, [q.currency for q in candidates])
    else:
        basis = "selected by advisor"
    notes: list[str] = []
    assessments: list[QuoteAssessment] = []

    for q in quotes:
        status = QuoteStatus(q.status)
        if q.passenger_capacity is None or pax is None:
            capacity = "unknown"
        else:
            capacity = "sufficient" if q.passenger_capacity >= pax else "insufficient"
        if inquiry.budget_amount is None:
            budget_status = "no_budget"
        elif inquiry.currency != q.currency:
            budget_status = "currency_mismatch"
        else:
            budget_status = "within" if q.all_in_price <= inquiry.budget_amount else "over"

        exclusion = None
        if status not in QUOTE_COMPARABLE_STATUSES:
            exclusion = f"status is '{status.value}'"
        elif q.currency != comparison_currency:
            exclusion = f"currency {q.currency} differs from comparison currency {comparison_currency}; no conversion applied"
        elif capacity == "insufficient":
            exclusion = "passenger capacity is below the requested passenger count"

        included, excluded = set(q.included_services or []), set(q.excluded_services or [])
        a = QuoteAssessment(
            quote_id=q.id, operator_label=q.operator_label, total_price=q.all_in_price, currency=q.currency,
            base_price=round(q.total_price, 2), additional_fees=q.additional_fees,
            aircraft_type=q.aircraft_type, passenger_capacity=q.passenger_capacity, status=status.value,
            comparable=exclusion is None, exclusion_reason=exclusion, capacity_status=capacity,
            budget_status=budget_status, restrictions=q.restrictions,
            requested_included=[s for s in requested if s in included],
            requested_excluded=[s for s in requested if s in excluded],
            requested_unknown=[s for s in requested if s not in included and s not in excluded],
        )
        # Documented advantages / disadvantages (only from stored fields)
        if capacity == "sufficient":
            a.advantages.append(f"Capacity {q.passenger_capacity} covers {pax} passengers")
        elif capacity == "insufficient":
            a.disadvantages.append(f"Capacity {q.passenger_capacity} is below the {pax} passengers requested")
        if budget_status == "within":
            a.advantages.append(f"Within stated budget of {inquiry.budget_amount:,.0f} {inquiry.currency}")
        elif budget_status == "over":
            a.disadvantages.append(f"Exceeds stated budget by {q.all_in_price - (inquiry.budget_amount or 0):,.0f} {q.currency}")
        for s in a.requested_included:
            a.advantages.append(f"Includes requested {SERVICE_LABELS.get(s, s)}")
        for s in a.requested_excluded:
            a.disadvantages.append(f"Excludes requested {SERVICE_LABELS.get(s, s)}")
        if q.restrictions:
            a.disadvantages.append(f"Documented restriction: {q.restrictions}")
        # Missing information and operator questions
        if not q.aircraft_type:
            a.missing_information.append("aircraft type")
            a.operator_questions.append("Which aircraft type is quoted?")
        if q.passenger_capacity is None:
            a.missing_information.append("passenger capacity")
            a.operator_questions.append("What is the passenger capacity of the quoted aircraft?")
        if q.valid_until is None:
            a.missing_information.append("quote validity date")
            a.operator_questions.append("Until when is this quote valid?")
        for s in a.requested_unknown:
            a.operator_questions.append(f"Is {SERVICE_LABELS.get(s, s)} included, and if not, what does it cost?")
        if q.additional_fees:
            a.disadvantages.append(f"Additional fees of {q.additional_fees:,.0f} {q.currency}"
                                   + (f" ({q.fees_description})" if q.fees_description else ""))
        if not ({"taxes", "landing_and_handling_fees"} & (included | excluded)):
            a.operator_questions.append("Does the total include taxes and landing/handling fees?")
        if q.restrictions:
            a.operator_questions.append(f"Please clarify the restriction: \"{q.restrictions}\"")
        if exclusion and "currency" in exclusion and comparison_currency:
            a.operator_questions.append(f"Can this quote be provided in {comparison_currency}?")
        assessments.append(a)

    comparable = [a for a in assessments if a.comparable]
    lowest = min(comparable, key=lambda a: (a.total_price, a.quote_id)) if comparable else None
    if lowest:
        for a in comparable:
            a.difference_from_lowest = round(a.total_price - lowest.total_price, 2)
            a.difference_from_lowest_pct = round((a.total_price - lowest.total_price) / lowest.total_price * 100, 1)
            a.is_lowest = a.quote_id == lowest.quote_id
        lowest.advantages.insert(0, "Lowest recorded price among comparable quotes")
        if len(comparable) == 1:
            notes.append("Only one comparable quote; price comparison is limited.")
    other_currencies = sorted({a.currency for a in assessments if a.currency != comparison_currency})
    if other_currencies:
        notes.append(f"Quotes in {', '.join(other_currencies)} are not price-compared: no currency conversion is configured.")
    if pax is None:
        notes.append("Passenger count is unknown, so capacity cannot be checked.")
    if not quotes:
        notes.append("No quotes are recorded for this inquiry.")
    return QuoteComparison(
        inquiry_id=inquiry.id, comparison_currency=comparison_currency, currency_basis=basis, passenger_count=pax,
        budget_amount=inquiry.budget_amount, budget_currency=inquiry.currency, requested_services=requested,
        assessments=assessments, lowest_quote_id=lowest.quote_id if lowest else None,
        lowest_price=lowest.total_price if lowest else None, notes=notes,
    )
