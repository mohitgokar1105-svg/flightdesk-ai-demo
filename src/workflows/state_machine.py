"""Explicit workflow states and permitted transitions (validated in Python, never by the model)."""

from __future__ import annotations

from enum import StrEnum

from src.exceptions import InvalidTransition


class WorkflowState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


S = WorkflowState
TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    S.RECEIVED: {S.VALIDATING, S.FAILED, S.CANCELLED},
    S.VALIDATING: {S.NEEDS_INFORMATION, S.READY_FOR_REVIEW, S.FAILED, S.CANCELLED},
    S.NEEDS_INFORMATION: {S.AWAITING_APPROVAL, S.COMPLETED, S.FAILED, S.CANCELLED},
    S.READY_FOR_REVIEW: {S.AWAITING_APPROVAL, S.COMPLETED, S.FAILED, S.CANCELLED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.REJECTED, S.CANCELLED, S.FAILED},
    S.APPROVED: {S.COMPLETED, S.FAILED},
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
    S.REJECTED: set(),
}
TERMINAL_STATES = {S.COMPLETED, S.FAILED, S.CANCELLED, S.REJECTED}

# Hard bounds that keep every run finite.
MAX_STATE_TRANSITIONS = 12
MAX_AGENT_TURNS = 6
MAX_TOOL_CALLS = 8


class WorkflowType(StrEnum):
    INQUIRY_INTAKE = "inquiry_intake"
    INQUIRY_TRIAGE = "inquiry_triage"
    FOLLOWUP_DRAFT = "followup_draft"
    BOOKING_CONFIRMATION = "booking_confirmation"
    QUOTE_EXPLANATION = "quote_explanation"
    ASSISTANT_QUERY = "assistant_query"
    INQUIRY_REREAD = "inquiry_reread"
    INQUIRY_SUMMARY = "inquiry_summary"
    QUOTE_DOCUMENT = "quote_document"
    COVER_NOTE = "cover_note"


WORKFLOW_DESCRIPTIONS = {
    WorkflowType.INQUIRY_INTAKE: "Extract → validate → advisor review/edit → save inquiry (+ optional recommendations / draft)",
    WorkflowType.INQUIRY_TRIAGE: "Agent gathers facts with read-only tools and proposes actions for advisor approval",
    WorkflowType.FOLLOWUP_DRAFT: "Draft a follow-up from verified facts → claims check → advisor edits and saves",
    WorkflowType.BOOKING_CONFIRMATION: "Validate quote/inquiry → advisor approval → record fictional confirmed booking",
    WorkflowType.QUOTE_EXPLANATION: "Deterministic comparison → optional AI explanation → numeric claims check",
    WorkflowType.ASSISTANT_QUERY: "Read-only assistant answers a question using allowlisted read-only tools",
    WorkflowType.INQUIRY_REREAD: "Re-read the customer's message → suggest values only for empty fields → team applies them",
    WorkflowType.INQUIRY_SUMMARY: "Deterministic inquiry facts → optional AI rephrasing → numeric claims check",
    WorkflowType.QUOTE_DOCUMENT: "Operator PDF → text → AI reads the quote → grounding checks → advisor reviews and saves",
    WorkflowType.COVER_NOTE: "Proposal facts from records → AI cover note → numeric claims check (template if withheld)",
}


def validate_transition(current: WorkflowState | str, target: WorkflowState | str) -> None:
    current, target = WorkflowState(current), WorkflowState(target)
    if target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "none (terminal)"
        raise InvalidTransition(f"Workflow cannot move from {current} to {target}. Allowed: {allowed}.")
