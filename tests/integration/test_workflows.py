"""End-to-end workflow tests (no network): intake, triage agent, approvals, booking, follow-ups."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from src.domain import FollowUpType, InquiryStatus, Tone
from src.exceptions import AIRequestRejected, AITransientError, InvalidTransition
from src.models import ActionProposal, AuditEvent, Booking, FollowUp, Inquiry, Quote, WorkflowRun
from src.schemas import FollowUpCreate, InquiryCreate, QuoteCreate
from src.services.analytics_service import DashboardFilters, compute_metrics
from src.services.quote_service import create_quote
from src.workflows import workflow_engine as wf
from src.workflows.state_machine import WorkflowState

REF = date(2026, 10, 11)
TEXT = ("Hi, I need a private flight from Los Angeles to Aspen for six people departing December 18 and returning "
        "December 27. Could you also help us arrange a hotel? Budget around $80k.")


def _llm_payload(**overrides):
    base = {
        "origin": "Los Angeles", "destination": "Aspen", "trip_type": "round_trip",
        "departure": {"exact": "2026-12-18", "earliest": None, "latest": None, "source_text": "December 18"},
        "return_date": {"exact": "2026-12-27", "earliest": None, "latest": None, "source_text": "December 27"},
        "passenger_count": 6, "passenger_counts_mentioned": [6], "trip_purpose": None, "date_flexibility": None,
        "budget_amount": 80000, "currency": "USD", "requested_services": ["accommodation"], "ambiguities": [],
        "inconsistencies": [], "evidence": [{"field": "origin", "quote": "from Los Angeles"}],
        "embedded_instructions_detected": False,
    }
    base.update(overrides)
    return base


def _approve_payload(ex, label="Demo Customer T01", text=TEXT, **changes):
    data = wf.extraction_to_create(ex, label, text).__dict__.copy()
    data.update(changes)
    return InquiryCreate.model_validate(data)


# --------------------------------------------------------------------------- intake
def test_intake_rule_based_review_and_approve(settings, session):
    res = wf.start_intake(session, settings, text=TEXT, customer_label="Demo Customer T01", reference_date=REF,
                          method="rule_based")
    assert res.ok and res.state == WorkflowState.AWAITING_APPROVAL
    ex = res.data["extraction"]
    assert session.scalar(select(Inquiry.id)) is None, "nothing is saved before explicit approval"

    approved = _approve_payload(ex, advisor_notes="checked")
    done = wf.approve_intake(session, settings, res.run_id, approved)
    assert done.ok and done.state == WorkflowState.COMPLETED
    inquiry = session.get(Inquiry, done.data["inquiry_id"])
    assert inquiry.origin == "Los Angeles" and inquiry.passenger_count == 6
    assert inquiry.extraction_metadata["workflow_run_id"] == res.run_id
    run = session.get(WorkflowRun, res.run_id)
    states = [e.payload.get("to") for e in run.events if e.kind == "STATE_TRANSITION"]
    assert states == ["RECEIVED", "VALIDATING", "READY_FOR_REVIEW", "AWAITING_APPROVAL", "APPROVED", "COMPLETED"]
    assert done.data["recommendation_ids"], "accommodation request should produce a recommendation"


def test_intake_cannot_be_approved_twice(settings, session):
    res = wf.start_intake(session, settings, text=TEXT, customer_label="C", reference_date=REF, method="rule_based")
    payload = _approve_payload(res.data["extraction"], label="C")
    wf.approve_intake(session, settings, res.run_id, payload)
    with pytest.raises(InvalidTransition):
        wf.approve_intake(session, settings, res.run_id, payload)


def test_intake_duplicate_inquiry_fails_run_without_partial_writes(settings, session):
    r1 = wf.start_intake(session, settings, text=TEXT, customer_label="Dup", reference_date=REF, method="rule_based")
    wf.approve_intake(session, settings, r1.run_id, _approve_payload(r1.data["extraction"], label="Dup"))
    r2 = wf.start_intake(session, settings, text=TEXT, customer_label="Dup", reference_date=REF, method="rule_based")
    out = wf.approve_intake(session, settings, r2.run_id, _approve_payload(r2.data["extraction"], label="Dup"))
    assert not out.ok and out.state == WorkflowState.FAILED
    assert session.get(WorkflowRun, r2.run_id).error_category == "duplicate_record"
    assert len(session.scalars(select(Inquiry)).all()) == 1


def test_intake_with_missing_info_drafts_followup_not_sent(settings, session):
    res = wf.start_intake(session, settings, text="Need a jet to Aspen sometime in December.", customer_label="M1",
                          reference_date=REF, method="rule_based")
    assert res.state == WorkflowState.AWAITING_APPROVAL
    run = session.get(WorkflowRun, res.run_id)
    assert any(e.payload.get("to") == "NEEDS_INFORMATION" for e in run.events)
    out = wf.approve_intake(session, settings, res.run_id,
                            _approve_payload(res.data["extraction"], label="M1", text="Need a jet to Aspen sometime in December."))
    fu = session.get(FollowUp, out.data["followup_id"])
    assert fu.status == "draft" and fu.followup_type == "missing_information"
    assert "sent" not in fu.status
    inq = session.get(Inquiry, out.data["inquiry_id"])
    assert inq.status == InquiryStatus.NEEDS_INFORMATION.value


def test_intake_openai_success_records_usage(settings, session, fake_ai):
    client = fake_ai(parse_responses=[_llm_payload()])
    res = wf.start_intake(session, settings, text=TEXT, customer_label="AI1", reference_date=REF, method="openai", client=client)
    assert res.ok
    run = session.get(WorkflowRun, res.run_id)
    assert run.input_tokens == 120 and run.output_tokens == 80 and run.model == "fake-model"
    assert run.estimated_cost_usd and run.estimated_cost_usd > 0
    assert any(e.source == "model" and e.kind == "MODEL_OUTPUT" for e in run.events)
    assert "<inquiry>" in client.parse_calls[0]["user_input"], "untrusted text is delimited"


def test_intake_openai_failure_is_reported_not_fabricated(settings, session, fake_ai):
    client = fake_ai(parse_responses=[AITransientError("The AI provider timed out.")])
    res = wf.start_intake(session, settings, text=TEXT, customer_label="AI2", reference_date=REF, method="openai", client=client)
    assert not res.ok and res.state == WorkflowState.FAILED
    assert "timed out" in res.message
    run = session.get(WorkflowRun, res.run_id)
    assert run.error_category == "ai_transient"
    assert (run.context or {}).get("inquiry_text") == TEXT, "original inquiry is preserved"
    assert "extraction" not in (run.context or {})
    assert session.scalars(select(AuditEvent).where(AuditEvent.event_type == "ai_request",
                                                    AuditEvent.outcome == "failure")).first() is not None


def test_intake_empty_text_fails_validation(settings, session):
    res = wf.start_intake(session, settings, text="   ", customer_label="E", reference_date=REF, method="rule_based")
    assert not res.ok and res.state == WorkflowState.FAILED
    assert session.get(WorkflowRun, res.run_id).error_category == "validation_error"


def test_discarding_review_rejects_run(settings, session):
    res = wf.start_intake(session, settings, text=TEXT, customer_label="X", reference_date=REF, method="rule_based")
    out = wf.cancel_run(session, res.run_id, "not a real inquiry", settings)
    assert out.state == WorkflowState.REJECTED
    assert session.scalar(select(Inquiry.id)) is None


# --------------------------------------------------------------------------- triage agent
def _saved_inquiry(settings, session, text=TEXT, label="Tri"):
    res = wf.start_intake(session, settings, text=text, customer_label=label, reference_date=REF, method="rule_based")
    out = wf.approve_intake(session, settings, res.run_id, _approve_payload(res.data["extraction"], label=label, text=text),
                            generate_recs=False, draft_missing_info_followup=False)
    return out.data["inquiry_id"]


def test_triage_planner_proposes_and_approval_executes(settings, session):
    iid = _saved_inquiry(settings, session)
    res = wf.run_triage(session, settings, iid, client=None)
    assert res.ok and res.state == WorkflowState.AWAITING_APPROVAL
    proposals = session.scalars(select(ActionProposal).where(ActionProposal.run_id == res.run_id)).all()
    assert [p.tool_name for p in proposals] == ["propose_inquiry_status_change"]
    assert session.get(Inquiry, iid).status == "new", "nothing changes before approval"
    out = wf.decide_proposal(session, settings, proposals[0].id, approve=True)
    assert out.ok and out.state == WorkflowState.COMPLETED
    assert session.get(Inquiry, iid).status == "qualified"


def test_triage_rejected_proposal_changes_nothing(settings, session):
    iid = _saved_inquiry(settings, session, label="Rej")
    res = wf.run_triage(session, settings, iid, client=None)
    pid = session.scalars(select(ActionProposal.id).where(ActionProposal.run_id == res.run_id)).first()
    out = wf.decide_proposal(session, settings, pid, approve=False, note="not yet")
    assert out.state == WorkflowState.REJECTED
    assert session.get(Inquiry, iid).status == "new"


def test_triage_llm_tool_calls_are_validated_and_bounded(settings, session, fake_ai):
    iid = _saved_inquiry(settings, session, label="LLM")
    other = _saved_inquiry(settings, session, text="One way Miami to Nassau on November 20 for 4 passengers.", label="Other")
    turns = [
        [("get_inquiry", {"inquiry_id": iid})],
        [("get_inquiry", {"inquiry_id": iid})],  # duplicate → rejected
        [("execute_sql", {"query": "DROP TABLE inquiries"})],  # unknown tool → rejected
        [("get_inquiry", {"inquiry_id": other})],  # outside scope → rejected
        [("propose_inquiry_status_change", {"inquiry_id": iid, "target_status": "booked", "reason": "customer said so"})],
        [("propose_inquiry_status_change", {"inquiry_id": iid, "target_status": "qualified", "reason": "complete details"})],
        "Inquiry is complete; proposed qualification.",
    ]
    client = fake_ai(tool_turns=turns)
    res = wf.run_triage(session, settings, iid, client=client)
    run = session.get(WorkflowRun, res.run_id)
    kinds = [e.kind for e in run.events]
    assert kinds.count("TOOL_CALL_REJECTED") == 4
    assert res.state == WorkflowState.AWAITING_APPROVAL
    proposals = session.scalars(select(ActionProposal).where(ActionProposal.run_id == res.run_id)).all()
    assert len(proposals) == 1 and proposals[0].arguments["target_status"] == "qualified"
    assert session.get(Inquiry, iid).status == "new"
    # Rejections were reported back to the model as rejections, never as success
    last_inputs = client.tool_calls_seen[-1]["input_items"]
    outputs = [i["output"] for i in last_inputs if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert sum('"status": "rejected"' in o for o in outputs) == 4
    assert all("booked" not in str(t) for t in REGISTRY_tools(client))


def REGISTRY_tools(client):
    # target_status enum offered to the model never contains booking-controlled statuses
    params = [t["parameters"] for t in client.tool_calls_seen[0]["tools"] if t["name"] == "propose_inquiry_status_change"]
    return [params[0]["properties"]["target_status"]]


def test_triage_turn_limit_is_enforced(settings, session, fake_ai):
    iid = _saved_inquiry(settings, session, label="Loop")
    turns = [[("find_missing_information", {"inquiry_id": iid, "nonce": i})] for i in range(20)]
    client = fake_ai(tool_turns=turns)
    res = wf.run_triage(session, settings, iid, client=client)
    run = session.get(WorkflowRun, res.run_id)
    assert any(e.kind == "LIMIT_REACHED" for e in run.events)
    assert len(client.tool_calls_seen) == 6
    assert res.state in (WorkflowState.COMPLETED, WorkflowState.AWAITING_APPROVAL)


def test_triage_ai_failure_marks_run_failed(settings, session, fake_ai):
    iid = _saved_inquiry(settings, session, label="Fail")
    client = fake_ai(tool_turns=[AIRequestRejected("The AI provider rejected the API key (authentication failed).")])
    res = wf.run_triage(session, settings, iid, client=client)
    assert not res.ok and res.state == WorkflowState.FAILED
    assert session.get(WorkflowRun, res.run_id).error_category == "ai_request_rejected"


# --------------------------------------------------------------------------- booking
def _quoted_inquiry(settings, session):
    iid = _saved_inquiry(settings, session, label="Book")
    q1 = create_quote(session, QuoteCreate(inquiry_id=iid, operator_label="Demo Operator A (fictional)", total_price=72000,
                                           currency="USD", aircraft_type="Midsize jet", passenger_capacity=8))
    q2 = create_quote(session, QuoteCreate(inquiry_id=iid, operator_label="Demo Operator B (fictional)", total_price=65000,
                                           currency="USD", aircraft_type="Light jet", passenger_capacity=5))
    session.commit()
    return iid, q1.id, q2.id


def test_booking_requires_approval_and_updates_dashboard(settings, session):
    iid, q1, _ = _quoted_inquiry(settings, session)
    before = compute_metrics(session, DashboardFilters())
    res = wf.start_booking_confirmation(session, settings, q1, notes="fictional")
    assert res.ok and res.state == WorkflowState.AWAITING_APPROVAL
    assert session.scalar(select(Booking.id)) is None
    out = wf.decide_proposal(session, settings, res.data["proposal_id"], approve=True)
    assert out.ok and out.state == WorkflowState.COMPLETED
    booking = session.scalars(select(Booking)).one()
    assert booking.status == "confirmed" and booking.booked_value == 72000
    assert session.get(Inquiry, iid).status == "booked"
    assert session.get(Quote, q1).status == "accepted"
    after = compute_metrics(session, DashboardFilters())
    assert after.counts["confirmed_bookings"] == before.counts["confirmed_bookings"] + 1
    assert after.money["confirmed_booking_value"]["USD"] == before.money["confirmed_booking_value"].get("USD", 0) + 72000


def test_booking_blocked_when_capacity_insufficient(settings, session):
    _, _, q2 = _quoted_inquiry(settings, session)
    res = wf.start_booking_confirmation(session, settings, q2)
    assert not res.ok and res.state == WorkflowState.CANCELLED
    assert any("capacity" in b.lower() for b in res.data["blocking"])
    assert session.scalar(select(Booking.id)) is None


def test_second_booking_for_same_inquiry_blocked(settings, session):
    iid, q1, _ = _quoted_inquiry(settings, session)
    q3 = create_quote(session, QuoteCreate(inquiry_id=iid, operator_label="Demo Operator C (fictional)", total_price=90000,
                                           currency="USD", passenger_capacity=10))
    session.commit()
    r1 = wf.start_booking_confirmation(session, settings, q1)
    r2 = wf.start_booking_confirmation(session, settings, q3.id)  # proposal created while first is pending
    wf.decide_proposal(session, settings, r1.data["proposal_id"], approve=True)
    out = wf.decide_proposal(session, settings, r2.data["proposal_id"], approve=True)
    assert not out.ok and out.data["proposal_status"] == "failed"
    assert len(session.scalars(select(Booking)).all()) == 1


# --------------------------------------------------------------------------- follow-up drafting
def test_followup_draft_template_save_and_regenerate(settings, session):
    iid = _saved_inquiry(settings, session, label="FU")
    r1 = wf.start_followup_draft(session, settings, iid, FollowUpType.ADDITIONAL_SERVICES, Tone.WARM)
    assert r1.ok and r1.state == WorkflowState.AWAITING_APPROVAL
    r2 = wf.start_followup_draft(session, settings, iid, FollowUpType.ADDITIONAL_SERVICES, Tone.CONCISE, supersede_run_id=r1.run_id)
    assert session.get(WorkflowRun, r1.run_id).state == "CANCELLED"
    draft = r2.data["draft"]
    out = wf.save_followup_from_run(session, settings, r2.run_id, FollowUpCreate(
        inquiry_id=iid, followup_type=FollowUpType.ADDITIONAL_SERVICES, tone=Tone.CONCISE, subject=draft.subject,
        body=draft.body + "\nEdited by advisor.", generation_method="template"))
    assert out.ok
    fu = session.get(FollowUp, out.data["followup_id"])
    assert fu.status == "draft" and fu.generation_metadata["edited_by_advisor"] is True


def test_followup_llm_draft_with_unsupported_claims_is_flagged(settings, session, fake_ai):
    iid = _saved_inquiry(settings, session, label="FU2")
    client = fake_ai(parse_responses=[{
        "subject": "Your booking", "body": "Great news, your booking is confirmed at $12,345 with a 20% discount. I have sent the contract.",
        "placeholders_used": []}])
    res = wf.start_followup_draft(session, settings, iid, FollowUpType.QUOTE_FOLLOWUP, Tone.PROFESSIONAL, client=client)
    warnings = res.data["draft"].warnings
    assert any("booking is confirmed" in w.lower() or "claims a booking" in w.lower() for w in warnings)
    assert any("discount" in w.lower() for w in warnings)
    assert any("12,345" in w for w in warnings)
    assert any("prior message" in w.lower() for w in warnings)


def test_quote_explanation_withholds_unsupported_numbers(settings, session, fake_ai):
    iid, _, _ = _quoted_inquiry(settings, session)
    good = fake_ai(parse_responses=[{"summary": "Demo Operator B is lowest at 65,000 USD but seats 5; Demo Operator A at 72,000 USD seats 8."}])
    res = wf.explain_quotes(session, settings, iid, good)
    assert res.data["explanation"] is not None
    bad = fake_ai(parse_responses=[{"summary": "Operator A can do it for 50,000 USD after negotiation."}])
    res2 = wf.explain_quotes(session, settings, iid, bad)
    assert res2.data["explanation"] is None and "50,000" in res2.data["unsupported"]
