"""Evidence-based recommendations.

Each rule fires only when stored records support it, and every recommendation carries evidence
references ``{source, record_id, field, value}``. Before anything is stored, :func:`verify_evidence`
re-reads each referenced record and confirms the value is really there — so a recommendation
(from a rule *or* from a model) with unsupported evidence is rejected.

No relevance score is stored: there is no calibrated way to compute one, and a made-up number would
mislead. Rules carry a documented priority instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ai.text_parsing import find_services
from src.auth import Permission, require
from src.domain import RECOMMENDATION_DECISIONS, FollowUpType, QuoteStatus, RecommendationStatus
from src.exceptions import DuplicateRecord, NotFound, PermissionDenied, ValidationFailure
from src.models import FollowUp, Inquiry, Quote, Recommendation, utcnow
from src.services.audit_service import record_audit

QUOTE_FOLLOWUP_AFTER_DAYS = 3
LARGE_GROUP_PAX = 19
CELEBRATION_RE = re.compile(r"\b(birthday|anniversary|celebrat\w*|honeymoon|engagement|graduation|proposal)\b", re.I)

SERVICE_RULES: dict[str, tuple[str, str, str]] = {
    # service -> (suggested action, explanation, next step)
    "accommodation": ("Coordinate the requested accommodation",
                      "The customer explicitly asked for help with accommodation.",
                      "Confirm check-in/check-out dates, number of rooms and preferences."),
    "ground_transport": ("Arrange the requested ground transport",
                         "The customer explicitly asked for ground transport.",
                         "Confirm pick-up locations, times and vehicle size."),
    "catering": ("Confirm catering requirements with the operator",
                 "The customer mentioned catering or dietary needs.",
                 "Collect menu preferences and dietary requirements; check operator catering options."),
    "pet_travel": ("Confirm pet travel policies with operators",
                   "The customer plans to travel with a pet.",
                   "Ask about the pet's size/species and check each operator's pet policy and destination rules."),
    "special_luggage": ("Confirm special luggage dimensions",
                        "The customer mentioned special or oversized luggage.",
                        "Collect item dimensions and weights; confirm hold capacity with operators."),
    "experiences": ("Follow up on requested experiences",
                    "The customer asked for help with experiences or reservations.",
                    "Clarify which experiences or reservations are wanted and for which dates."),
}


@dataclass
class EvidenceRef:
    source: str  # inquiry | quote | followup
    record_id: str
    field: str
    value: Any
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "record_id": self.record_id, "field": self.field, "value": self.value, "note": self.note}


@dataclass
class RecommendationCandidate:
    rule_key: str
    category: str
    suggested_action: str
    explanation: str
    next_step: str
    evidence: list[EvidenceRef]
    missing_information: list[str] = field(default_factory=list)
    priority: str = "medium"


_SOURCES = {"inquiry": Inquiry, "quote": Quote, "followup": FollowUp}
_VERIFIABLE_FIELDS = {
    "inquiry": {"requested_services", "missing_information", "date_flexibility", "trip_type", "passenger_count",
                "budget_amount", "currency", "original_text", "status", "destination", "origin"},
    "quote": {"status", "total_price", "currency", "passenger_capacity", "inquiry_id", "updated_at"},
    "followup": {"status", "followup_type"},
}


def _contains(actual: Any, value: Any) -> bool:
    if isinstance(actual, list):
        return any(a == value or (isinstance(a, dict) and value in a.values()) for a in actual)
    if isinstance(actual, str) and isinstance(value, str):
        return value.strip().lower() in actual.lower() if len(value) > 2 else actual.lower() == value.lower()
    if isinstance(actual, int | float) and isinstance(value, int | float):
        return abs(actual - value) < 0.005
    if isinstance(actual, datetime):
        return actual.isoformat() == str(value)
    return actual == value


def verify_evidence(session: Session, inquiry_id: str, evidence: Sequence[EvidenceRef | dict[str, Any]]) -> list[str]:
    """Return a list of problems (empty if every evidence reference is supported by stored data)."""
    require(Permission.VIEW_OPERATIONS)
    if not evidence:
        return ["No evidence supplied."]
    problems: list[str] = []
    for raw in evidence:
        ref = raw if isinstance(raw, EvidenceRef) else EvidenceRef(
            source=str(raw.get("source")), record_id=str(raw.get("record_id")), field=str(raw.get("field")),
            value=raw.get("value"), note=raw.get("note"))
        model = _SOURCES.get(ref.source)
        if model is None or ref.field not in _VERIFIABLE_FIELDS.get(ref.source, set()):
            problems.append(f"Evidence source/field not allowed: {ref.source}.{ref.field}")
            continue
        record: Any = session.get(model, ref.record_id)
        if record is None:
            problems.append(f"Referenced {ref.source} {ref.record_id} does not exist.")
            continue
        owner = record.id if ref.source == "inquiry" else getattr(record, "inquiry_id", None)
        if owner != inquiry_id:
            problems.append(f"{ref.source} {ref.record_id} does not belong to inquiry {inquiry_id}.")
            continue
        if not _contains(getattr(record, ref.field), ref.value):
            problems.append(f"{ref.source} {ref.record_id}.{ref.field} does not contain {ref.value!r}.")
    return problems


def inquiry_rule_candidates(inquiry: Inquiry) -> list[RecommendationCandidate]:
    """Rules that depend only on the inquiry record (pure; also used by the evaluation framework)."""
    out: list[RecommendationCandidate] = []
    iid = inquiry.id
    services = list(inquiry.requested_services or [])
    text = inquiry.original_text or ""
    snippets = find_services(text) if text else {}

    missing = [m["field"] for m in (inquiry.missing_information or [])]
    if missing and inquiry.status in ("new", "needs_information", "qualified"):
        out.append(RecommendationCandidate(
            "missing_information", "missing_information", "Request the missing trip details",
            "Operators cannot quote accurately without these details.",
            "Draft a 'missing information' follow-up for advisor review.",
            [EvidenceRef("inquiry", iid, "missing_information", f) for f in missing], missing, "high"))

    for svc in services:
        if svc not in SERVICE_RULES:
            continue
        action, why, nxt = SERVICE_RULES[svc]
        ev = [EvidenceRef("inquiry", iid, "requested_services", svc)]
        if svc in snippets:
            ev.append(EvidenceRef("inquiry", iid, "original_text", snippets[svc][0], note="customer wording"))
        out.append(RecommendationCandidate(f"service:{svc}", svc, action, why, nxt, ev, [], "medium"))

    if "catering" not in services:
        m = CELEBRATION_RE.search(text)
        if m:
            out.append(RecommendationCandidate(
                "catering_question", "catering", "Ask whether catering is wanted for the occasion",
                "The customer mentioned a celebration; catering was not requested, so ask rather than assume.",
                "Add an optional catering question to the next follow-up.",
                [EvidenceRef("inquiry", iid, "original_text", m.group(0), note="customer wording")], [], "low"))

    if inquiry.date_flexibility == "flexible" and inquiry.trip_type in (None, "one_way"):
        out.append(RecommendationCandidate(
            "empty_leg", "empty_leg", "Investigate empty-leg options",
            "The customer said their dates are flexible, which is when empty-leg (repositioning) flights can fit.",
            "Check empty-leg listings for the route and date window before requesting full charter quotes.",
            [EvidenceRef("inquiry", iid, "date_flexibility", "flexible")],
            ["trip_type"] if inquiry.trip_type is None else [], "medium"))

    if inquiry.passenger_count and inquiry.passenger_count > LARGE_GROUP_PAX:
        out.append(RecommendationCandidate(
            "large_group", "quote_request", "Confirm group size and consider larger charter categories",
            f"{inquiry.passenger_count} passengers exceeds typical private-jet capacity ({LARGE_GROUP_PAX}).",
            "Confirm the group size and whether splitting across aircraft is acceptable.",
            [EvidenceRef("inquiry", iid, "passenger_count", inquiry.passenger_count)], [], "medium"))
    return out


def candidate_recommendations(session: Session, inquiry: Inquiry, now: datetime | None = None) -> list[RecommendationCandidate]:
    """Run all deterministic rules (inquiry-only + quote/follow-up rules) for one inquiry."""
    require(Permission.VIEW_OPERATIONS)
    now = now or utcnow()
    out = inquiry_rule_candidates(inquiry)
    iid = inquiry.id

    quotes = list(session.scalars(select(Quote).where(Quote.inquiry_id == iid)))
    open_quotes = [q for q in quotes if q.status in (QuoteStatus.RECEIVED.value, QuoteStatus.PRESENTED.value)]
    if len(open_quotes) >= 2:
        out.append(RecommendationCandidate(
            "compare_quotes", "quote_comparison", "Compare the recorded quotes in Quote Copilot",
            f"{len(open_quotes)} quotes are awaiting a decision.",
            "Review the deterministic comparison, then share options with the customer.",
            [EvidenceRef("quote", q.id, "status", q.status) for q in open_quotes], [], "medium"))

    cutoff = now - timedelta(days=QUOTE_FOLLOWUP_AFTER_DAYS)
    for q in quotes:
        if q.status == QuoteStatus.PRESENTED.value and q.updated_at < cutoff:
            later = session.scalar(select(FollowUp.id).where(
                FollowUp.inquiry_id == iid, FollowUp.followup_type == FollowUpType.QUOTE_FOLLOWUP.value,
                FollowUp.created_at >= q.updated_at, FollowUp.status != "cancelled"))
            if not later:
                out.append(RecommendationCandidate(
                    f"quote_followup:{q.id}", "followup", f"Follow up on the quote from {q.operator_label}",
                    f"The quote was presented more than {QUOTE_FOLLOWUP_AFTER_DAYS} days ago with no recorded follow-up.",
                    "Draft a quote follow-up for advisor review.",
                    [EvidenceRef("quote", q.id, "status", "presented"), EvidenceRef("quote", q.id, "updated_at", q.updated_at.isoformat())],
                    [], "high"))
                break

    if inquiry.budget_amount and inquiry.currency:
        same_ccy = [q for q in open_quotes if q.currency == inquiry.currency]
        if same_ccy and all(q.total_price > inquiry.budget_amount for q in same_ccy):
            out.append(RecommendationCandidate(
                "budget_gap", "budget", "Discuss the gap between the budget and recorded quotes",
                f"All {len(same_ccy)} open quote(s) in {inquiry.currency} exceed the stated budget.",
                "Ask whether the budget is flexible or whether a smaller aircraft / alternative dates are acceptable.",
                [EvidenceRef("inquiry", iid, "budget_amount", inquiry.budget_amount),
                 *[EvidenceRef("quote", q.id, "total_price", q.total_price) for q in same_ccy]], [], "medium"))

    if inquiry.passenger_count:
        known = [q for q in open_quotes if q.passenger_capacity is not None]
        if known and all((q.passenger_capacity or 0) < inquiry.passenger_count for q in known):
            out.append(RecommendationCandidate(
                "capacity_gap", "quote_request", "Request quotes with adequate capacity",
                f"No open quote with documented capacity seats {inquiry.passenger_count} passengers.",
                "Request quotes for larger aircraft.",
                [EvidenceRef("inquiry", iid, "passenger_count", inquiry.passenger_count),
                 *[EvidenceRef("quote", q.id, "passenger_capacity", q.passenger_capacity) for q in known]], [], "high"))
    return out


def create_recommendation(session: Session, inquiry_id: str, candidate: RecommendationCandidate, *, source: str = "rule_engine",
                          actor: str = "system", is_sample: bool = False) -> Recommendation:
    """Store a recommendation after verifying its evidence. Unsupported recommendations are rejected."""
    require(Permission.EDIT_OPERATIONS)
    problems = verify_evidence(session, inquiry_id, candidate.evidence)
    if problems:
        record_audit(session, "recommendation_rejected", actor=actor, entity_type="inquiry", entity_id=inquiry_id,
                     metadata={"rule_key": candidate.rule_key, "problems": problems}, outcome="rejected")
        raise ValidationFailure("Recommendation rejected — unsupported evidence: " + "; ".join(problems))
    existing = session.scalar(select(Recommendation).where(Recommendation.inquiry_id == inquiry_id,
                                                            Recommendation.rule_key == candidate.rule_key))
    if existing:
        raise DuplicateRecord(f"Recommendation '{candidate.rule_key}' already exists for this inquiry ({existing.id}).")
    rec = Recommendation(
        inquiry_id=inquiry_id, rule_key=candidate.rule_key, category=candidate.category,
        suggested_action=candidate.suggested_action, explanation=candidate.explanation, next_step=candidate.next_step,
        evidence=[e.as_dict() for e in candidate.evidence], missing_information=candidate.missing_information,
        priority=candidate.priority, relevance_score=None, source=source, is_sample=is_sample,
    )
    session.add(rec)
    session.flush()
    record_audit(session, "recommendation_created", actor=actor, entity_type="recommendation", entity_id=rec.id,
                 metadata={"inquiry_id": inquiry_id, "rule_key": candidate.rule_key, "source": source})
    return rec


def generate_recommendations(session: Session, inquiry_id: str, *, actor: str = "system") -> dict[str, list[Any]]:
    """Create recommendations for new rule matches. Existing ones (and advisor decisions) are preserved."""
    require(Permission.EDIT_OPERATIONS)
    inquiry = session.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise NotFound(f"Inquiry {inquiry_id} not found.")
    created, existing, rejected = [], [], []
    for cand in candidate_recommendations(session, inquiry):
        try:
            # Duplicate and evidence checks run before any insert, so a failure leaves no partial row.
            created.append(create_recommendation(session, inquiry_id, cand, actor=actor))
        except DuplicateRecord:
            existing.append(cand.rule_key)
        except ValidationFailure as exc:
            rejected.append({"rule_key": cand.rule_key, "reason": str(exc)})
    return {"created": created, "existing": existing, "rejected": rejected}


def decide_recommendation(session: Session, recommendation_id: str, decision: RecommendationStatus | str, *,
                          feedback: str | None = None, actor: str = "advisor") -> Recommendation:
    """Record the advisor's decision. Only advisors decide (model requests go through approvals)."""
    require(Permission.EDIT_OPERATIONS)
    if actor == "model":
        raise PermissionDenied("The model cannot record recommendation decisions directly.")
    decision = RecommendationStatus(decision)
    if decision not in RECOMMENDATION_DECISIONS:
        raise ValidationFailure(f"'{decision}' is not a decision. Use accepted, dismissed or irrelevant.")
    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise NotFound(f"Recommendation {recommendation_id} not found.")
    if rec.status != RecommendationStatus.OPEN.value:
        raise ValidationFailure(f"Recommendation already decided ({rec.status}).")
    rec.status = decision.value
    rec.decided_at = utcnow()
    rec.advisor_feedback = (feedback or "").strip()[:2000] or None
    session.flush()
    record_audit(session, "recommendation_decided", actor=actor, entity_type="recommendation", entity_id=rec.id,
                 metadata={"decision": decision.value, "has_feedback": bool(rec.advisor_feedback)})
    return rec


def list_recommendations(session: Session, inquiry_id: str | None = None, statuses: list[str] | None = None) -> list[Recommendation]:
    require(Permission.VIEW_OPERATIONS)
    stmt = select(Recommendation).order_by(Recommendation.created_at.desc())
    if inquiry_id:
        stmt = stmt.where(Recommendation.inquiry_id == inquiry_id)
    if statuses:
        stmt = stmt.where(Recommendation.status.in_(statuses))
    return list(session.scalars(stmt))


def recommendation_to_dict(r: Recommendation) -> dict[str, Any]:
    return {
        "recommendation_id": r.id, "inquiry_id": r.inquiry_id, "rule_key": r.rule_key, "category": r.category,
        "suggested_action": r.suggested_action, "explanation": r.explanation, "next_step": r.next_step,
        "evidence": r.evidence, "missing_information": r.missing_information, "priority": r.priority,
        "status": r.status, "created_at": r.created_at.isoformat(),
        "decided_at": r.decided_at.isoformat() if r.decided_at else None, "advisor_feedback": r.advisor_feedback,
        "source": r.source, "is_sample": r.is_sample,
    }
