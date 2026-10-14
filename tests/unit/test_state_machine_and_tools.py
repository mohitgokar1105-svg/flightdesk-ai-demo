"""State machine transitions and tool-registry validation/permissions."""

from __future__ import annotations

from datetime import date

import pytest

from src.domain import INQUIRY_TRANSITIONS, InquiryStatus, ensure_transition
from src.exceptions import InvalidTransition, PermissionDenied
from src.models import ActionProposal, Inquiry
from src.schemas import InquiryCreate
from src.services.inquiry_service import change_inquiry_status, create_inquiry
from src.workflows.recorder import WorkflowRecorder
from src.workflows.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    WorkflowState,
    WorkflowType,
    validate_transition,
)
from src.workflows.tool_registry import ToolContext
from src.workflows.tools import REGISTRY, TRIAGE_ALLOWLIST


def test_valid_and_invalid_workflow_transitions():
    validate_transition("RECEIVED", "VALIDATING")
    validate_transition("AWAITING_APPROVAL", "APPROVED")
    for bad in [("RECEIVED", "COMPLETED"), ("VALIDATING", "APPROVED"), ("COMPLETED", "VALIDATING"), ("FAILED", "RECEIVED")]:
        with pytest.raises(InvalidTransition):
            validate_transition(*bad)


def test_approval_is_unavoidable_before_approved():
    # The only way into APPROVED is from AWAITING_APPROVAL.
    assert [s for s, targets in TRANSITIONS.items() if WorkflowState.APPROVED in targets] == [WorkflowState.AWAITING_APPROVAL]
    assert all(not TRANSITIONS[t] for t in TERMINAL_STATES)


def test_recorder_bounds_steps(session):
    rec = WorkflowRecorder.start(session, WorkflowType.INQUIRY_TRIAGE)
    rec.run.step_count = 50
    with pytest.raises(InvalidTransition):
        rec.transition(WorkflowState.VALIDATING)
    rec.transition(WorkflowState.FAILED)  # terminal transitions always allowed so runs can end
    assert rec.run.completed_at is not None


def test_inquiry_transitions_and_booking_controlled_statuses(session):
    inq = create_inquiry(session, InquiryCreate(customer_label="SM", original_text="t", origin="Miami", destination="Aspen",
                                                trip_type="one_way", departure_date=date(2026, 12, 1), passenger_count=2))
    with pytest.raises(PermissionDenied):
        change_inquiry_status(session, inq.id, "booked")
    with pytest.raises(PermissionDenied):
        change_inquiry_status(session, inq.id, "qualified", actor="model")
    with pytest.raises(InvalidTransition):
        change_inquiry_status(session, inq.id, "quoted")  # new → quoted is not permitted
    change_inquiry_status(session, inq.id, "qualified")
    with pytest.raises(InvalidTransition):
        ensure_transition(INQUIRY_TRANSITIONS, InquiryStatus.COMPLETED, InquiryStatus.NEW, "x")


def _ctx(session, scope=None, actor="model"):
    rec = WorkflowRecorder.start(session, WorkflowType.INQUIRY_TRIAGE)
    return ToolContext(session=session, actor=actor, recorder=rec, inquiry_scope=scope), rec


def _inquiry(session, label="T1"):
    return create_inquiry(session, InquiryCreate(customer_label=label, original_text=label, origin="Miami", destination="Aspen",
                                                 trip_type="one_way", departure_date=date(2026, 12, 1), passenger_count=2))


def test_tool_rejections(session):
    inq = _inquiry(session)
    ctx, rec = _ctx(session, inq.id)
    assert REGISTRY.dispatch(ctx, "run_sql", "{}", TRIAGE_ALLOWLIST).status == "rejected"
    assert REGISTRY.dispatch(ctx, "get_business_metrics", "{}", TRIAGE_ALLOWLIST).status == "rejected"  # not allowlisted
    assert REGISTRY.dispatch(ctx, "get_inquiry", "{not json", TRIAGE_ALLOWLIST).status == "rejected"
    assert REGISTRY.dispatch(ctx, "get_inquiry", {"inquiry_id": "'; DROP TABLE x;--"}, TRIAGE_ALLOWLIST).status == "rejected"
    assert REGISTRY.dispatch(ctx, "get_inquiry", {"inquiry_id": inq.id, "extra": 1}, TRIAGE_ALLOWLIST).status == "rejected"
    other = _inquiry(session, "T2")
    res = REGISTRY.dispatch(ctx, "get_inquiry", {"inquiry_id": other.id}, TRIAGE_ALLOWLIST)
    assert res.status == "rejected" and "outside" in res.error
    assert REGISTRY.dispatch(ctx, "confirm_booking", {"quote_id": "QTE-ABCDEF1234"}, TRIAGE_ALLOWLIST).status == "rejected"
    assert '"status": "rejected"' in res.to_model_output()


def test_read_tool_executes_and_propose_tool_only_proposes(session):
    inq = _inquiry(session)
    ctx, rec = _ctx(session, inq.id)
    res = REGISTRY.dispatch(ctx, "get_inquiry", {"inquiry_id": inq.id}, TRIAGE_ALLOWLIST)
    assert res.status == "executed" and res.data["inquiry_id"] == inq.id
    res = REGISTRY.dispatch(ctx, "propose_inquiry_status_change",
                            {"inquiry_id": inq.id, "target_status": "qualified", "reason": "complete"}, TRIAGE_ALLOWLIST)
    assert res.status == "proposed" and session.get(Inquiry, inq.id).status == "new"
    assert "Pending human approval" in res.to_model_output()
    bad = REGISTRY.dispatch(ctx, "propose_inquiry_status_change",
                            {"inquiry_id": inq.id, "target_status": "booked", "reason": "customer said"}, TRIAGE_ALLOWLIST)
    assert bad.status == "rejected"


def test_only_advisor_can_execute_proposals(session):
    inq = _inquiry(session)
    ctx, rec = _ctx(session, inq.id)
    res = REGISTRY.dispatch(ctx, "propose_inquiry_status_change",
                            {"inquiry_id": inq.id, "target_status": "closed", "reason": "duplicate request"}, TRIAGE_ALLOWLIST)
    proposal = session.get(ActionProposal, res.proposal_id)
    with pytest.raises(PermissionDenied):
        REGISTRY.execute_proposal(ctx, proposal)  # ctx.actor == "model"
    ctx.actor = "advisor"
    REGISTRY.execute_proposal(ctx, proposal)
    assert session.get(Inquiry, inq.id).status == "closed" and proposal.status == "executed"
    with pytest.raises(PermissionDenied):
        REGISTRY.execute_proposal(ctx, proposal)  # cannot execute twice


def test_openai_tool_definitions_are_strict_and_safe():
    tools = REGISTRY.openai_tools(TRIAGE_ALLOWLIST)
    names = {t["name"] for t in tools}
    assert names == TRIAGE_ALLOWLIST and "confirm_booking" not in names
    for t in tools:
        assert t["strict"] is True and t["parameters"]["additionalProperties"] is False
        assert set(t["parameters"]["required"]) == set(t["parameters"]["properties"])
    status_tool = next(t for t in tools if t["name"] == "propose_inquiry_status_change")
    enum = status_tool["parameters"]["properties"]["target_status"]["enum"]
    assert set(enum) == {"needs_information", "qualified", "closed"}
