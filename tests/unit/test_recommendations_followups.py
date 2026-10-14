"""Recommendations (evidence, decisions, unsupported rejection) and follow-ups (lifecycle, no false sending claims)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.ai.drafting import check_draft_claims, template_draft
from src.domain import FollowUpStatus, FollowUpType, Tone
from src.exceptions import DuplicateRecord, InvalidTransition, PermissionDenied, ValidationFailure
from src.models import AuditEvent, FollowUp, Recommendation
from src.schemas import FollowUpCreate, InquiryCreate
from src.services.followup_service import (
    build_followup_facts,
    change_followup_status,
    create_followup,
    is_overdue,
    list_due_followups,
    update_followup,
)
from src.services.inquiry_service import create_inquiry
from src.services.recommendation_service import (
    EvidenceRef,
    RecommendationCandidate,
    create_recommendation,
    decide_recommendation,
    generate_recommendations,
    verify_evidence,
)

TEXT = "Hi, I need a private flight from Los Angeles to Aspen for six people in December. Our dates are flexible. Could you also help us arrange a hotel? It's for my wife's birthday."


def _inq(session, text=TEXT, **kw):
    data = dict(customer_label="Demo Customer R1", original_text=text, origin="Los Angeles", destination="Aspen",
                departure_earliest=date(2026, 12, 1), departure_latest=date(2026, 12, 31), passenger_count=6,
                date_flexibility="flexible", requested_services=["accommodation"])
    data.update(kw)
    return create_inquiry(session, InquiryCreate(**data))


def test_recommendations_are_evidence_based(session):
    inq = _inq(session)
    res = generate_recommendations(session, inq.id)
    keys = {r.rule_key for r in res["created"]}
    assert {"service:accommodation", "empty_leg", "missing_information", "catering_question"} <= keys
    assert "service:catering" not in keys and "service:ground_transport" not in keys, "no recommendation without evidence"
    for r in res["created"]:
        assert r.evidence and verify_evidence(session, inq.id, r.evidence) == []
        assert r.relevance_score is None
    again = generate_recommendations(session, inq.id)
    assert not again["created"] and set(again["existing"]) == keys


def test_unsupported_recommendation_rejected_and_audited(session):
    inq = _inq(session)
    fake = RecommendationCandidate("service:catering", "catering", "Offer premium catering", "Customer loves caviar", "Upsell",
                                   [EvidenceRef("inquiry", inq.id, "requested_services", "catering")])
    with pytest.raises(ValidationFailure):
        create_recommendation(session, inq.id, fake, source="agent", actor="model")
    assert session.scalar(select(AuditEvent).where(AuditEvent.event_type == "recommendation_rejected")) is not None
    other = _inq(session, text="other", customer_label="Other")
    cross = RecommendationCandidate("x", "x", "x", "x", "x", [EvidenceRef("inquiry", other.id, "requested_services", "accommodation")])
    assert verify_evidence(session, inq.id, cross.evidence)
    assert verify_evidence(session, inq.id, []) == ["No evidence supplied."]
    assert verify_evidence(session, inq.id, [{"source": "inquiry", "record_id": inq.id, "field": "content_hash", "value": "x"}])


def test_accept_dismiss_irrelevant_and_feedback(session):
    inq = _inq(session)
    recs = generate_recommendations(session, inq.id)["created"]
    decide_recommendation(session, recs[0].id, "accepted", feedback="useful")
    decide_recommendation(session, recs[1].id, "dismissed")
    decide_recommendation(session, recs[2].id, "irrelevant", feedback="n/a")
    assert session.get(Recommendation, recs[0].id).decided_at is not None
    with pytest.raises(ValidationFailure):
        decide_recommendation(session, recs[0].id, "dismissed")
    with pytest.raises(ValidationFailure):
        decide_recommendation(session, recs[3].id, "open")
    with pytest.raises(PermissionDenied):
        decide_recommendation(session, recs[3].id, "accepted", actor="model")


# --------------------------------------------------------------------------- follow-ups
def _fu(session, inq, **kw):
    data = dict(inquiry_id=inq.id, followup_type=FollowUpType.MISSING_INFORMATION, subject="Details", body="Body text",
                due_at=datetime.now(UTC) + timedelta(days=1))
    data.update(kw)
    return create_followup(session, FollowUpCreate(**data))


def test_followup_create_edit_persist(session):
    inq = _inq(session)
    f = _fu(session, inq)
    assert f.status == "draft"
    update_followup(session, f.id, subject="New subject", body="Edited body")
    assert session.get(FollowUp, f.id).subject == "New subject"
    with pytest.raises(DuplicateRecord):
        _fu(session, inq, subject="New subject", body="Edited body")


def test_followup_lifecycle_and_locking(session):
    inq = _inq(session)
    f = _fu(session, inq)
    change_followup_status(session, f.id, "pending_approval")
    change_followup_status(session, f.id, "approved")
    with pytest.raises(ValidationFailure):
        update_followup(session, f.id, body="sneaky edit after approval")
    change_followup_status(session, f.id, "scheduled")
    change_followup_status(session, f.id, "completed", notes="Sent from shared mailbox (outside app)")
    done = session.get(FollowUp, f.id)
    assert done.completed_at is not None and "No email is sent by this application" in done.advisor_notes
    with pytest.raises(InvalidTransition):
        change_followup_status(session, f.id, "draft")


def test_followup_cancellation_and_due_tracking(session):
    inq = _inq(session)
    overdue = _fu(session, inq, subject="Late", due_at=datetime.now(UTC) - timedelta(days=2))
    future = _fu(session, inq, subject="Soon", due_at=datetime.now(UTC) + timedelta(days=3))
    assert is_overdue(overdue) and not is_overdue(future)
    assert {f.id for f in list_due_followups(session, 7)} == {overdue.id, future.id}
    change_followup_status(session, overdue.id, "cancelled")
    assert not is_overdue(session.get(FollowUp, overdue.id))
    with pytest.raises(ValidationFailure):
        no_due = _fu(session, inq, subject="No due", due_at=None)
        change_followup_status(session, no_due.id, "approved")
        change_followup_status(session, no_due.id, "scheduled")


def test_no_sent_status_and_no_delivery_claims(session):
    assert "sent" not in {s.value for s in FollowUpStatus}
    inq = _inq(session)
    facts = build_followup_facts(session, inq.id)
    for ftype in FollowUpType:
        for tone in Tone:
            d = template_draft(facts, ftype, tone)
            text = (d.subject + d.body).lower()
            assert "has been sent" not in text and "we sent" not in text and "is confirmed" not in text
            assert "discount" not in text


def test_claims_guard_flags_unsupported_statements(session):
    inq = _inq(session)
    facts = build_followup_facts(session, inq.id)
    warnings = check_draft_claims("Your booking is confirmed at $9,999 with 10% off. As discussed on our call, we have sent the invoice.", facts)
    joined = " ".join(warnings).lower()
    assert "booking is confirmed" in joined or "claims a booking" in joined
    assert "9,999" in joined and "discount" in joined and "prior message" in joined
    assert check_draft_claims("Could you confirm the number of passengers?", facts) == []


def test_model_followups_only_as_drafts(session):
    inq = _inq(session)
    with pytest.raises(PermissionDenied):
        create_followup(session, FollowUpCreate(inquiry_id=inq.id, followup_type=FollowUpType.POST_TRIP, subject="s", body="b"),
                        actor="model", status=FollowUpStatus.APPROVED)


def test_naive_due_date_rejected():
    with pytest.raises(ValidationError):
        FollowUpCreate(inquiry_id="INQ-X", followup_type=FollowUpType.POST_TRIP, subject="s", body="b", due_at=datetime(2026, 1, 1))
