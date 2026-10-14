"""Controlled workflows built on the explicit state machine.

Every public function here is a unit of work that owns its commit points: the run (and its state) is
committed *before* business writes, so if a business write fails the transaction is rolled back and
the failure is still recorded on the run. Business failures are returned as ``WorkflowResult(ok=False)``.

Bounds: max state transitions per run, max agent turns, max tool calls, duplicate-call rejection and
bounded network retries in the AI client. Validation failures are never retried.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.ai.client import AIClient, TokenUsage
from src.ai.drafting import llm_draft, template_draft
from src.ai.extraction import METHOD_OPENAI, METHOD_RULE_BASED, run_openai, run_rule_based
from src.ai.prompts import (
    AGENT_INSTRUCTIONS,
    AGENT_PROMPT_VERSION,
    ASSISTANT_PROMPT_VERSION,
    EXPLANATION_INSTRUCTIONS,
    FOLLOWUP_PROMPT_VERSION,
    assistant_instructions,
    explanation_input,
)
from src.auth import Permission, require
from src.config import Settings
from src.domain import FollowUpType, InquiryStatus, Tone
from src.exceptions import AIServiceError, FlightDeskError, InvalidTransition, NotFound
from src.models import ActionProposal, FollowUp, utcnow
from src.schemas import FollowUpCreate, InquiryCreate, InquiryExtraction, LLMExplanation
from src.security import detect_prompt_injection, summarise_untrusted
from src.services.audit_service import record_audit
from src.services.booking_service import check_booking_from_quote
from src.services.followup_service import build_followup_facts, create_followup
from src.services.inquiry_service import create_inquiry, get_inquiry
from src.services.quote_service import compare_quotes, list_quotes_for_inquiry
from src.services.recommendation_service import QUOTE_FOLLOWUP_AFTER_DAYS, generate_recommendations
from src.workflows.recorder import SOURCE_APP, SOURCE_HUMAN, SOURCE_MODEL, WorkflowRecorder
from src.workflows.state_machine import MAX_AGENT_TURNS, MAX_TOOL_CALLS, WorkflowState, WorkflowType
from src.workflows.tool_registry import ToolContext, ToolResult
from src.workflows.tools import ASSISTANT_ALLOWLIST, BOOKING_ALLOWLIST, REGISTRY, TRIAGE_ALLOWLIST

logger = logging.getLogger(__name__)
WS = WorkflowState


@dataclass
class WorkflowResult:
    run_id: str
    state: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _result(rec: WorkflowRecorder, ok: bool, message: str = "", **data: Any) -> WorkflowResult:
    return WorkflowResult(run_id=rec.run.id, state=rec.run.state, ok=ok, message=message, data=data)


def _audit_ai(session: Session, purpose: str, model: str | None, ok: bool, usage: TokenUsage | None = None,
              error_category: str | None = None, run_id: str | None = None) -> None:
    record_audit(session, "ai_request", actor="system", entity_type="workflow_run", entity_id=run_id,
                 metadata={"purpose": purpose, "model": model, "input_tokens": usage.input_tokens if usage else 0,
                           "output_tokens": usage.output_tokens if usage else 0, "error_category": error_category},
                 outcome="success" if ok else "failure")


def _fail(session: Session, run_id: str, exc: Exception | str, settings: Settings | None, category: str | None = None) -> WorkflowRecorder:
    """Roll back pending business writes, then record the failure on the run and commit."""
    session.rollback()
    rec = WorkflowRecorder.load(session, run_id, settings)
    for p in rec.run.proposals:
        if p.status == "pending":
            p.status, p.error = "rejected", "Workflow failed before approval."
    rec.fail(exc, category)
    session.commit()
    return rec


# =========================================================================== inquiry intake
def start_intake(session: Session, settings: Settings, *, text: str, customer_label: str, reference_date: date,
                 method: str, client: AIClient | None = None) -> WorkflowResult:
    """RECEIVED → VALIDATING → (NEEDS_INFORMATION | READY_FOR_REVIEW) → AWAITING_APPROVAL, or FAILED."""
    require(Permission.EDIT_OPERATIONS)
    if method not in (METHOD_RULE_BASED, METHOD_OPENAI):
        raise ValueError(f"Unknown extraction method {method}")
    rec = WorkflowRecorder.start(
        session, WorkflowType.INQUIRY_INTAKE, settings=settings, ai_mode=method,
        model=client.model if (client and method == METHOD_OPENAI) else None,
        input_summary=f"{len(text or '')} chars · {summarise_untrusted(text, 70)}",
        context={"customer_label": customer_label, "reference_date": reference_date.isoformat(), "method": method,
                 "inquiry_text": text},
    )
    session.commit()
    try:
        rec.transition(WS.VALIDATING, f"extracting with {method}")
        if method == METHOD_OPENAI:
            if client is None:
                raise AIServiceError("Live AI client unavailable.")
            outcome = run_openai(text, reference_date, client)
            _audit_ai(session, "inquiry_extraction", outcome.model, outcome.ok, outcome.usage, outcome.error_category, rec.run.id)
        else:
            outcome = run_rule_based(text, reference_date)
    except ValueError as exc:  # empty / oversized input
        rec = _fail(session, rec.run.id, str(exc), settings, "validation_error")
        return _result(rec, False, str(exc))
    rec.add_usage(outcome.usage, outcome.model)
    if not outcome.ok:
        session.commit()
        rec = _fail(session, rec.run.id, outcome.error or "Extraction failed", settings, outcome.error_category)
        return _result(rec, False, outcome.error or "Extraction failed", error_category=outcome.error_category)

    ex = outcome.extraction
    assert ex is not None
    fields = {k: v for k, v in ex.model_dump(mode="json", include={
        "origin", "destination", "trip_type", "departure", "return_date", "passenger_count", "trip_purpose",
        "date_flexibility", "budget_amount", "currency", "requested_services"}).items()}
    rec.event("MODEL_OUTPUT" if method == METHOD_OPENAI else "RULE_BASED_EXTRACTION",
              SOURCE_MODEL if method == METHOD_OPENAI else SOURCE_APP,
              f"Extraction via {method}" + (f" ({outcome.model}, {outcome.attempts} attempt(s))" if method == METHOD_OPENAI else ""),
              {"fields": fields, "schema_valid": outcome.schema_valid, "latency_ms": round(outcome.latency_ms, 1),
               "prompt_version": outcome.prompt_version})
    ungrounded = [k for k, v in ex.grounding.items() if not v]
    rec.event("DETERMINISTIC_CHECK", SOURCE_APP,
              f"Validation: {len(ex.all_issues())} issue(s), {len(ex.missing_information)} missing, {len(ungrounded)} ungrounded",
              {"issues": [i.model_dump() for i in ex.all_issues()], "missing": [m.field for m in ex.missing_information],
               "ungrounded_fields": ungrounded, "injection_signals": ex.injection_signals})
    if ex.missing_information:
        rec.event("DECISION", SOURCE_APP, "Rule: required details missing → next step is a missing-information follow-up",
                  {"rule": "missing_information_non_empty"})
        rec.transition(WS.NEEDS_INFORMATION, "required details missing")
    else:
        rec.event("DECISION", SOURCE_APP, "Rule: all required details present → next step is advisor qualification",
                  {"rule": "missing_information_empty"})
        rec.transition(WS.READY_FOR_REVIEW, "all required details present")
    rec.transition(WS.AWAITING_APPROVAL, "advisor must review and approve before saving")
    rec.update_context(extraction=ex.model_dump(mode="json"), model=outcome.model, schema_valid=outcome.schema_valid,
                       prompt_version=outcome.prompt_version)
    session.commit()
    return _result(rec, True, "Extraction ready for review", extraction=ex, outcome=outcome)


def extraction_to_create(ex: InquiryExtraction, customer_label: str, text: str) -> InquiryCreate:
    """Pre-fill the review form. Invalid values are left for the advisor to correct (validated on approval)."""
    placeholder = customer_label.strip().lower() in ("", "new customer")
    return InquiryCreate.model_construct(
        customer_label=(ex.customer_name if ex.customer_name and placeholder else customer_label),
        customer_email=ex.contact_email, customer_phone=ex.contact_phone,
        departure_time_preference=ex.departure_time_preference,
        special_requirements="; ".join(ex.special_requirements) or None,
        original_text=text, origin=ex.origin, destination=ex.destination,
        trip_type=ex.trip_type, departure_date=ex.departure.exact, departure_earliest=ex.departure.earliest,
        departure_latest=ex.departure.latest, return_date=ex.return_date.exact, return_earliest=ex.return_date.earliest,
        return_latest=ex.return_date.latest, passenger_count=ex.passenger_count, trip_purpose=ex.trip_purpose,
        date_flexibility=ex.date_flexibility, budget_amount=ex.budget_amount, currency=ex.currency,
        requested_services=cast(Any, list(ex.requested_services)), estimated_opportunity_value=ex.budget_amount, advisor_notes=None)


def approve_intake(session: Session, settings: Settings, run_id: str, approved: InquiryCreate, *,
                   generate_recs: bool = True, draft_missing_info_followup: bool = True) -> WorkflowResult:
    """AWAITING_APPROVAL → APPROVED → COMPLETED: persist exactly what the advisor approved."""
    require(Permission.EDIT_OPERATIONS)
    rec = WorkflowRecorder.load(session, run_id, settings)
    if rec.state != WS.AWAITING_APPROVAL:
        raise InvalidTransition(f"Run {run_id} is {rec.state}; only runs awaiting approval can be approved.")
    ctx = rec.run.context or {}
    extracted = ctx.get("extraction") or {}
    suggested = extraction_to_create(InquiryExtraction.model_validate(extracted), approved.customer_label,
                                     approved.original_text) if extracted else None
    edited = [f for f in InquiryCreate.model_fields if suggested is not None and f not in ("advisor_notes", "original_text")
              and getattr(suggested, f, None) != getattr(approved, f)]
    rec.event("HUMAN_DECISION", SOURCE_HUMAN, "Advisor approved the reviewed inquiry" + (f" (edited: {', '.join(edited)})" if edited else ""),
              {"decision": "approved", "edited_fields": edited})
    rec.transition(WS.APPROVED, "advisor approval")
    session.commit()
    try:
        meta = {"method": ctx.get("method"), "model": ctx.get("model"), "schema_valid": ctx.get("schema_valid"),
                "prompt_version": ctx.get("prompt_version"), "workflow_run_id": run_id, "edited_fields": edited,
                "issue_codes": sorted({i["code"] for k in ("validation_issues", "ambiguities", "inconsistencies")
                                       for i in extracted.get(k, [])}),
                "ungrounded_fields": [k for k, v in (extracted.get("grounding") or {}).items() if not v]}
        inquiry = create_inquiry(session, approved, extraction_metadata=meta, source="intake")
        rec.run.inquiry_id = inquiry.id
        rec.event("APPLICATION_ACTION", SOURCE_APP, f"Saved inquiry {inquiry.id} (status {inquiry.status})",
                  {"inquiry_id": inquiry.id, "status": inquiry.status})
        created_recs: list[str] = []
        if generate_recs:
            res = generate_recommendations(session, inquiry.id)
            created_recs = [r.id for r in res["created"]]
            rec.event("APPLICATION_ACTION", SOURCE_APP, f"Rule engine created {len(created_recs)} recommendation(s)",
                      {"created": [r.rule_key for r in res["created"]], "rejected": res["rejected"]})
        followup_id = None
        if draft_missing_info_followup and inquiry.missing_information:
            facts = build_followup_facts(session, inquiry.id)
            draft = template_draft(facts, FollowUpType.MISSING_INFORMATION, Tone.PROFESSIONAL)
            fu = create_followup(session, FollowUpCreate(
                inquiry_id=inquiry.id, followup_type=FollowUpType.MISSING_INFORMATION, subject=draft.subject,
                body=draft.body, due_at=utcnow() + timedelta(days=1), generation_method="template",
                generation_metadata={"workflow_run_id": run_id, "warnings": draft.warnings}))
            followup_id = fu.id
            rec.event("APPLICATION_ACTION", SOURCE_APP, f"Saved missing-information draft {fu.id} (status draft, not sent)",
                      {"followup_id": fu.id})
        rec.finish(WS.COMPLETED, f"Inquiry {inquiry.id} saved; {len(created_recs)} recommendation(s)"
                   + (f"; draft {followup_id}" if followup_id else ""))
        session.commit()
        return _result(rec, True, f"Inquiry {inquiry.id} saved.", inquiry_id=inquiry.id, recommendation_ids=created_recs,
                       followup_id=followup_id)
    except FlightDeskError as exc:
        rec = _fail(session, run_id, exc, settings)
        return _result(rec, False, str(exc))


def cancel_run(session: Session, run_id: str, reason: str, settings: Settings | None = None) -> WorkflowResult:
    """Advisor discards a run: AWAITING_APPROVAL → REJECTED, other non-terminal states → CANCELLED."""
    require(Permission.EDIT_OPERATIONS)
    rec = WorkflowRecorder.load(session, run_id, settings)
    target = WS.REJECTED if rec.state == WS.AWAITING_APPROVAL else WS.CANCELLED
    rec.event("HUMAN_DECISION", SOURCE_HUMAN, f"Advisor discarded the run: {reason}", {"decision": "discarded"})
    for p in rec.run.proposals:
        if p.status == "pending":
            p.status, p.decided_at, p.decided_by = "rejected", utcnow(), "advisor"
    rec.finish(target, reason or "discarded by advisor")
    session.commit()
    return _result(rec, True, f"Run {run_id} {target.value.lower()}.")


# =========================================================================== proposals (approvals)
def decide_proposal(session: Session, settings: Settings | None, proposal_id: str, *, approve: bool,
                    note: str | None = None) -> WorkflowResult:
    """Human decision on one proposal. Approved proposals execute with re-validated arguments."""
    require(Permission.EDIT_OPERATIONS)
    proposal = session.get(ActionProposal, proposal_id)
    if proposal is None:
        raise NotFound(f"Proposal {proposal_id} not found.")
    rec = WorkflowRecorder.load(session, proposal.run_id, settings)
    if rec.state != WS.AWAITING_APPROVAL:
        raise InvalidTransition(f"Run {rec.run.id} is {rec.state}; proposals can only be decided while awaiting approval.")
    if proposal.status != "pending":
        raise InvalidTransition(f"Proposal {proposal_id} is already {proposal.status}.")
    message = ""
    if approve:
        ctx = ToolContext(session=session, actor="advisor", settings=settings, recorder=rec, inquiry_scope=rec.run.inquiry_id)
        try:
            try:
                REGISTRY.execute_proposal(ctx, proposal)
                session.commit()
            except SQLAlchemyError as db_exc:  # failed database write: roll back and report it as a failure
                logger.exception("Database error while executing proposal %s", proposal_id)
                raise FlightDeskError("The change couldn't be saved because of a database error. Nothing was changed.") from db_exc
            message = f"Executed {proposal.tool_name}."
        except FlightDeskError as exc:
            session.rollback()
            rec = WorkflowRecorder.load(session, proposal.run_id, settings)
            reloaded = session.get(ActionProposal, proposal_id)
            assert reloaded is not None
            proposal = reloaded
            proposal.status, proposal.error, proposal.decided_at, proposal.decided_by = "failed", str(exc)[:500], utcnow(), "advisor"
            rec.event("HUMAN_DECISION", SOURCE_HUMAN, f"Advisor approved {proposal_id}", {"decision": "approved"})
            rec.event("ERROR", SOURCE_APP, f"Execution of {proposal.tool_name} failed validation: {exc}",
                      {"proposal_id": proposal_id, "category": exc.category})
            record_audit(session, "proposal_failed", actor="advisor", entity_type="action_proposal", entity_id=proposal_id,
                         metadata={"tool": proposal.tool_name, "error": str(exc)}, outcome="failure")
            message = f"Not executed: {exc}"
    else:
        proposal.status, proposal.decided_at, proposal.decided_by = "rejected", utcnow(), "advisor"
        proposal.error = (note or "Rejected by advisor")[:500]
        rec.event("HUMAN_DECISION", SOURCE_HUMAN, f"Advisor rejected {proposal_id} ({proposal.tool_name})",
                  {"proposal_id": proposal_id, "decision": "rejected", "note": note or ""})
        record_audit(session, "proposal_rejected", actor="advisor", entity_type="action_proposal", entity_id=proposal_id,
                     metadata={"tool": proposal.tool_name}, outcome="rejected")
        message = "Proposal rejected; nothing was changed."

    session.flush()
    statuses = [p.status for p in rec.run.proposals]
    if "pending" not in statuses:
        executed, failed = statuses.count("executed"), statuses.count("failed")
        rejected = statuses.count("rejected")
        summary = f"{executed} executed, {rejected} rejected, {failed} failed"
        if executed:
            rec.transition(WS.APPROVED, "advisor decisions recorded")
            rec.finish(WS.COMPLETED, summary)
        elif failed:
            rec.run.error_category = "execution_failed"
            rec.finish(WS.FAILED, summary)
        else:
            rec.finish(WS.REJECTED, summary)
    session.commit()
    ok = proposal.status in ("executed", "rejected")
    return _result(rec, ok, message, proposal_status=proposal.status, result=proposal.result)


# =========================================================================== triage agent
def _finish_agent_run(session: Session, rec: WorkflowRecorder, summary: str) -> WorkflowResult:
    inquiry = get_inquiry(session, rec.run.inquiry_id) if rec.run.inquiry_id else None
    pending = [p for p in rec.run.proposals if p.status == "pending"]
    needs_info = bool(inquiry and inquiry.missing_information)
    rec.transition(WS.NEEDS_INFORMATION if needs_info else WS.READY_FOR_REVIEW,
                   "inquiry has missing information" if needs_info else "facts gathered")
    if pending:
        rec.transition(WS.AWAITING_APPROVAL, f"{len(pending)} proposal(s) need advisor approval")
        rec.run.final_outcome = summary[:300]
    else:
        rec.finish(WS.COMPLETED, summary or "No actions proposed.")
    session.commit()
    return _result(rec, True, summary, proposal_ids=[p.id for p in pending])


def _planner(session: Session, ctx: ToolContext, inquiry_id: str) -> str:
    """Deterministic triage planner used when live AI is unavailable. Same registry, same permissions."""
    def call(name: str, args: dict[str, Any]) -> ToolResult:
        return REGISTRY.dispatch(ctx, name, args, TRIAGE_ALLOWLIST)

    inq = call("get_inquiry", {"inquiry_id": inquiry_id}).data
    missing = call("find_missing_information", {"inquiry_id": inquiry_id}).data.get("missing_information", [])
    quotes = call("list_quotes_for_inquiry", {"inquiry_id": inquiry_id}).data.get("quotes", [])
    due = call("list_due_followups", {"within_days": 14, "inquiry_id": inquiry_id}).data.get("followups", [])
    open_types = {f["followup_type"] for f in due} | {
        f.followup_type for f in session.query(FollowUp).filter(FollowUp.inquiry_id == inquiry_id,
                                                                 FollowUp.status.notin_(["completed", "cancelled"]))}
    notes: list[str] = [f"Inquiry status {inq.get('status')}; {len(missing)} missing item(s); {len(quotes)} quote(s)."]
    if len([q for q in quotes if q["status"] in ("received", "presented")]) >= 2:
        comp = call("compare_quotes", {"inquiry_id": inquiry_id}).data
        if comp.get("lowest_quote_id"):
            notes.append(f"Lowest comparable quote: {comp['lowest_quote_id']}.")
    if missing:
        if "missing_information" not in open_types:
            call("draft_followup", {"inquiry_id": inquiry_id, "followup_type": "missing_information", "tone": "professional"})
            notes.append("Proposed a missing-information follow-up draft.")
        if inq.get("status") == InquiryStatus.NEW.value:
            call("propose_inquiry_status_change", {"inquiry_id": inquiry_id, "target_status": "needs_information",
                                                   "reason": "Required trip details are missing."})
            notes.append("Proposed status needs_information.")
    elif inq.get("status") in (InquiryStatus.NEW.value, InquiryStatus.NEEDS_INFORMATION.value):
        call("propose_inquiry_status_change", {"inquiry_id": inquiry_id, "target_status": "qualified",
                                               "reason": "All required trip details are recorded."})
        notes.append("Proposed status qualified.")
    stale = [q for q in quotes if q["status"] == "presented"
             and utcnow().isoformat() > (date.fromisoformat(q["created_at"][:10]) + timedelta(days=QUOTE_FOLLOWUP_AFTER_DAYS)).isoformat()]
    if stale and "quote_followup" not in open_types:
        call("draft_followup", {"inquiry_id": inquiry_id, "followup_type": "quote_followup", "tone": "professional"})
        notes.append("Proposed a quote follow-up draft.")
    return " ".join(notes)


def _tool_loop(session: Session, rec: WorkflowRecorder, client: AIClient, ctx: ToolContext, allowlist: set[str],
               instructions: str, user_message: str, purpose: str, prior_items: list[Any] | None = None,
               tool_log: list[dict[str, Any]] | None = None) -> tuple[str, list[Any]]:
    """Bounded model ↔ tool loop. Python executes every call; duplicates and over-budget calls are rejected.

    Returns the final text and the new conversation items produced in this turn (for multi-turn chat history)."""
    tools = REGISTRY.openai_tools(allowlist)
    prior = list(prior_items or [])
    input_items: list[Any] = [*prior, {"role": "user", "content": user_message}]
    log = tool_log if tool_log is not None else []
    seen: set[str] = set()
    used = 0
    for turn_no in range(1, MAX_AGENT_TURNS + 1):
        turn = client.tool_turn(instructions=instructions, input_items=input_items, tools=tools, purpose=purpose)
        rec.add_usage(turn.usage, turn.model)
        _audit_ai(session, purpose, turn.model, True, turn.usage, None, rec.run.id)
        input_items.extend(turn.output_items)
        if not turn.tool_calls:
            rec.event("MODEL_OUTPUT", SOURCE_MODEL, f"Model final answer (turn {turn_no})", {"text": turn.text[:2000]})
            return turn.text.strip(), input_items[len(prior):]
        for call in turn.tool_calls:
            try:
                key = call.name + json.dumps(json.loads(call.arguments_json or "{}"), sort_keys=True)
            except ValueError:
                key = call.name + (call.arguments_json or "")
            if used >= MAX_TOOL_CALLS:
                result = REGISTRY._reject(ctx, call.name, f"Tool-call budget of {MAX_TOOL_CALLS} exhausted.")
            elif key in seen:
                result = REGISTRY._reject(ctx, call.name, "Duplicate call with identical arguments; reuse the earlier result.")
            else:
                result = REGISTRY.dispatch(ctx, call.name, call.arguments_json, allowlist)
                used += 1
            seen.add(key)
            try:
                parsed_args = json.loads(call.arguments_json or "{}")
            except ValueError:
                parsed_args = {"_unparseable": (call.arguments_json or "")[:200]}
            log.append({"tool": call.name, "args": parsed_args, "status": result.status, "error": result.error,
                        "proposal_id": result.proposal_id, "data": result.data})
            input_items.append({"type": "function_call_output", "call_id": call.call_id, "output": result.to_model_output()})
        session.commit()
    rec.event("LIMIT_REACHED", SOURCE_APP, f"Stopped after {MAX_AGENT_TURNS} model turns (bounded execution).",
              {"max_turns": MAX_AGENT_TURNS, "tool_calls": used})
    text = "I stopped before finishing because this request needed too many steps. Could you narrow it down?"
    return text, [*input_items[len(prior):], {"role": "assistant", "content": text}]


def run_triage(session: Session, settings: Settings, inquiry_id: str, *, client: AIClient | None = None) -> WorkflowResult:
    """Agentic triage: gather facts with tools, propose actions, wait for approval. ``client=None`` → deterministic planner."""
    require(Permission.EDIT_OPERATIONS)
    mode = "openai" if client else "rule_planner"
    rec = WorkflowRecorder.start(session, WorkflowType.INQUIRY_TRIAGE, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode=mode, model=client.model if client else None,
                                 input_summary=f"Triage {inquiry_id}", context={"prompt_version": AGENT_PROMPT_VERSION if client else None})
    session.commit()
    try:
        get_inquiry(session, inquiry_id)
        rec.transition(WS.VALIDATING, "gathering facts with tools")
        ctx = ToolContext(session=session, actor="model" if client else "planner", settings=settings, recorder=rec,
                          inquiry_scope=inquiry_id)
        if client:
            summary, _ = _tool_loop(session, rec, client, ctx, TRIAGE_ALLOWLIST, AGENT_INSTRUCTIONS,
                                    f"Triage inquiry {inquiry_id}. Today's date is {utcnow().date().isoformat()}.", "triage_agent")
        else:
            rec.event("DECISION", SOURCE_APP, "Live AI unavailable: using the deterministic rule planner (labelled)", {})
            summary = _planner(session, ctx, inquiry_id)
        return _finish_agent_run(session, rec, summary)
    except AIServiceError as exc:
        _audit_ai(session, "triage_agent", client.model if client else None, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))
    except FlightDeskError as exc:
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))


MAX_CHAT_MESSAGE_CHARS = 2000


def run_chat_turn(session: Session, settings: Settings, history_items: list[Any], message: str,
                  client: AIClient, *, tool_failures: frozenset[str] = frozenset()) -> WorkflowResult:
    """One AI Assistant turn: the model answers from tool results; write tools only create proposals.

    ``data``: answer, items (new conversation items to append to history), tool_log, proposals (pending approvals),
    intake_run_ids (inquiries prepared for review)."""
    require(Permission.USE_ASSISTANT)
    message = (message or "").strip()
    rec = WorkflowRecorder.start(session, WorkflowType.ASSISTANT_QUERY, settings=settings, ai_mode="openai",
                                 model=client.model, input_summary=summarise_untrusted(message, 120),
                                 context={"prompt_version": ASSISTANT_PROMPT_VERSION})
    session.commit()
    tool_log: list[dict[str, Any]] = []
    try:
        if not message or len(message) > MAX_CHAT_MESSAGE_CHARS:
            raise FlightDeskError(f"Please keep messages between 1 and {MAX_CHAT_MESSAGE_CHARS} characters.")
        rec.transition(WS.VALIDATING, "answering with allowlisted tools")
        signals = detect_prompt_injection(message)
        if signals:
            rec.event("DETERMINISTIC_CHECK", SOURCE_APP, f"Instruction-like content detected: {', '.join(signals)}",
                      {"signals": signals})
        ctx = ToolContext(session=session, actor="model", settings=settings, recorder=rec, injected_failures=tool_failures)
        instructions = assistant_instructions(utcnow())
        answer, items = _tool_loop(session, rec, client, ctx, ASSISTANT_ALLOWLIST, instructions, message,
                                   "assistant_chat", prior_items=history_items, tool_log=tool_log)
        if not answer:
            answer = "I couldn't produce an answer for that. Could you rephrase it?"
        pending = [p for p in rec.run.proposals if p.status == "pending"]
        rec.transition(WS.READY_FOR_REVIEW, "answer ready")
        if pending:
            rec.transition(WS.AWAITING_APPROVAL, f"{len(pending)} action(s) need your approval")
            rec.run.final_outcome = "Answered; awaiting approval of proposed actions"
        else:
            rec.finish(WS.COMPLETED, "Answered")
        session.commit()
        proposals = [{"proposal_id": p.id, "tool": p.tool_name, "arguments": dict(p.arguments or {}),
                      "rationale": p.rationale} for p in pending]
        intake_runs = [t["data"].get("review_run_id") for t in tool_log
                       if t["tool"] == "prepare_inquiry" and t["status"] == "executed" and t["data"].get("review_run_id")]
        return _result(rec, True, "ok", answer=answer, items=items, tool_log=tool_log, proposals=proposals,
                       intake_run_ids=intake_runs)
    except AIServiceError as exc:
        _audit_ai(session, "assistant_chat", client.model, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc), tool_log=tool_log, error_category=exc.category)
    except FlightDeskError as exc:
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc), tool_log=tool_log, error_category=exc.category)


# =========================================================================== follow-up drafting
def start_followup_draft(session: Session, settings: Settings, inquiry_id: str, followup_type: FollowUpType, tone: Tone, *,
                         client: AIClient | None = None, supersede_run_id: str | None = None) -> WorkflowResult:
    require(Permission.EDIT_OPERATIONS)
    if supersede_run_id:
        try:
            old = WorkflowRecorder.load(session, supersede_run_id, settings)
            if old.state == WS.AWAITING_APPROVAL:
                old.event("HUMAN_DECISION", SOURCE_HUMAN, "Superseded by a regenerated draft", {"decision": "regenerate"})
                old.finish(WS.CANCELLED, "superseded by regeneration")
        except FlightDeskError:
            pass
    method = "openai" if client else "template"
    rec = WorkflowRecorder.start(session, WorkflowType.FOLLOWUP_DRAFT, settings=settings, inquiry_id=inquiry_id, ai_mode=method,
                                 model=client.model if client else None,
                                 input_summary=f"{followup_type.value} · {tone.value}",
                                 context={"followup_type": followup_type.value, "tone": tone.value,
                                          "prompt_version": FOLLOWUP_PROMPT_VERSION if client else None})
    session.commit()
    try:
        rec.transition(WS.VALIDATING, "collecting verified facts")
        facts = build_followup_facts(session, inquiry_id)
        rec.event("DETERMINISTIC_CHECK", SOURCE_APP, "Facts assembled from records",
                  {"fact_keys": sorted(k for k, v in facts["inquiry"].items() if v not in (None, [], "")),
                   "quotes": len(facts["quotes"])})
        if client:
            draft, usage, model = llm_draft(facts, followup_type, tone, client)
            rec.add_usage(usage, model)
            _audit_ai(session, "followup_draft", model, True, usage, None, rec.run.id)
            rec.event("MODEL_OUTPUT", SOURCE_MODEL, f"Draft generated by {model}", {"subject": draft.subject})
        else:
            draft = template_draft(facts, followup_type, tone)
            rec.event("TEMPLATE_OUTPUT", SOURCE_APP, "Draft generated from deterministic template", {"subject": draft.subject})
        rec.event("DETERMINISTIC_CHECK", SOURCE_APP,
                  f"Claims check: {len(draft.warnings)} warning(s), {len(draft.placeholders)} placeholder(s)",
                  {"warnings": draft.warnings, "placeholders": draft.placeholders})
        rec.transition(WS.READY_FOR_REVIEW, "draft ready")
        rec.transition(WS.AWAITING_APPROVAL, "advisor edits and saves the draft")
        rec.update_context(draft=draft.model_dump())
        session.commit()
        return _result(rec, True, "Draft ready for review", draft=draft)
    except AIServiceError as exc:
        _audit_ai(session, "followup_draft", client.model if client else None, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))
    except FlightDeskError as exc:
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))


def save_followup_from_run(session: Session, settings: Settings, run_id: str, data: FollowUpCreate) -> WorkflowResult:
    require(Permission.EDIT_OPERATIONS)
    rec = WorkflowRecorder.load(session, run_id, settings)
    if rec.state != WS.AWAITING_APPROVAL:
        raise InvalidTransition(f"Run {run_id} is {rec.state}; the draft can no longer be saved from it.")
    original = (rec.run.context or {}).get("draft", {})
    edited = original.get("subject") != data.subject or original.get("body") != data.body
    rec.event("HUMAN_DECISION", SOURCE_HUMAN, "Advisor saved the draft" + (" (edited)" if edited else ""),
              {"decision": "approved", "edited": edited})
    rec.transition(WS.APPROVED, "advisor saved")
    session.commit()
    try:
        meta = dict(data.generation_metadata)
        meta.update({"workflow_run_id": run_id, "edited_by_advisor": edited, "warnings": original.get("warnings", [])})
        data = data.model_copy(update={"generation_metadata": meta})
        fu = create_followup(session, data)
        rec.finish(WS.COMPLETED, f"Follow-up {fu.id} saved as draft (not sent)")
        session.commit()
        return _result(rec, True, f"Follow-up {fu.id} saved.", followup_id=fu.id)
    except FlightDeskError as exc:
        rec = _fail(session, run_id, exc, settings)
        return _result(rec, False, str(exc))


# =========================================================================== booking confirmation
def start_booking_confirmation(session: Session, settings: Settings, quote_id: str, notes: str | None = None) -> WorkflowResult:
    require(Permission.MANAGE_BOOKINGS)
    from src.models import Quote

    quote = session.get(Quote, quote_id)
    rec = WorkflowRecorder.start(session, WorkflowType.BOOKING_CONFIRMATION, settings=settings,
                                 inquiry_id=quote.inquiry_id if quote else None, input_summary=f"Confirm booking for {quote_id}")
    session.commit()
    rec.transition(WS.VALIDATING, "checking quote, inquiry, capacity and duplicates")
    check = check_booking_from_quote(session, quote_id)
    rec.event("DETERMINISTIC_CHECK", SOURCE_APP, "Booking checks " + ("passed" if check.ok else "blocked"),
              {"blocking": check.blocking, "warnings": check.warnings, "value": check.booked_value, "currency": check.currency})
    if not check.ok:
        rec.transition(WS.NEEDS_INFORMATION, "blocking issues")
        rec.finish(WS.CANCELLED, "Blocked: " + " ".join(check.blocking))
        session.commit()
        return _result(rec, False, " ".join(check.blocking), blocking=check.blocking)
    rec.transition(WS.READY_FOR_REVIEW, "checks passed")
    ctx = ToolContext(session=session, actor="advisor", settings=settings, recorder=rec, inquiry_scope=rec.run.inquiry_id)
    res = REGISTRY.dispatch(ctx, "confirm_booking", {"quote_id": quote_id, "notes": notes}, BOOKING_ALLOWLIST)
    if res.status != "proposed":
        rec = _fail(session, rec.run.id, res.error or "Could not create approval request", settings, "validation_error")
        return _result(rec, False, res.error or "")
    rec.transition(WS.AWAITING_APPROVAL, "advisor must approve the booking record")
    session.commit()
    return _result(rec, True, "Awaiting approval", proposal_id=res.proposal_id, warnings=check.warnings,
                   booked_value=check.booked_value, currency=check.currency)


# =========================================================================== quote explanation
_NUM_RE = re.compile(r"(?<![A-Za-z-])\d[\d,]*(?:\.\d+)?")


def _numbers_in(obj: Any) -> set[float]:
    out: set[float] = set()
    for m in _NUM_RE.finditer(json.dumps(obj, default=str)):
        try:
            out.add(round(float(m.group(0).replace(",", "")), 2))
        except ValueError:
            continue
    return out


def unsupported_numbers(text: str, facts: dict[str, Any]) -> list[str]:
    """Numbers in ``text`` that do not appear in ``facts`` (small counts ≤ number of quotes are allowed)."""
    allowed = _numbers_in(facts)
    allowed |= set(range(0, len(facts.get("quotes", [])) + 2))
    bad = []
    for m in _NUM_RE.finditer(text):
        try:
            v = round(float(m.group(0).replace(",", "")), 2)
        except ValueError:
            continue
        if v not in allowed and not any(abs(v - a) < 0.051 for a in allowed):
            bad.append(m.group(0))
    return bad


def explain_quotes(session: Session, settings: Settings, inquiry_id: str, client: AIClient,
                   comparison_currency: str | None = None) -> WorkflowResult:
    require(Permission.VIEW_OPERATIONS)
    rec = WorkflowRecorder.start(session, WorkflowType.QUOTE_EXPLANATION, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode="openai", model=client.model, input_summary=f"Explain quotes for {inquiry_id}")
    session.commit()
    try:
        rec.transition(WS.VALIDATING, "deterministic comparison")
        inquiry = get_inquiry(session, inquiry_id)
        comp = compare_quotes(inquiry, list_quotes_for_inquiry(session, inquiry_id), comparison_currency)
        facts = {"comparison_currency": comp.comparison_currency, "passenger_count": comp.passenger_count,
                 "budget": comp.budget_amount, "budget_currency": comp.budget_currency, "notes": comp.notes,
                 "quotes": [{"operator": a.operator_label, "price": a.total_price, "currency": a.currency,
                             "comparable": a.comparable, "is_lowest": a.is_lowest, "difference_from_lowest": a.difference_from_lowest,
                             "difference_pct": a.difference_from_lowest_pct, "capacity": a.passenger_capacity,
                             "advantages": a.advantages, "disadvantages": a.disadvantages} for a in comp.assessments]}
        rec.event("DETERMINISTIC_CHECK", SOURCE_APP, "Comparison computed in Python", {"lowest_quote_id": comp.lowest_quote_id})
        res = client.parse(instructions=EXPLANATION_INSTRUCTIONS, user_input=explanation_input(facts), schema=LLMExplanation,
                           purpose="quote_explanation")
        rec.add_usage(res.usage, res.model)
        _audit_ai(session, "quote_explanation", res.model, True, res.usage, None, rec.run.id)
        rec.event("MODEL_OUTPUT", SOURCE_MODEL, "Explanation generated", {"summary": res.parsed.summary})
        bad = unsupported_numbers(res.parsed.summary, facts)
        rec.event("DETERMINISTIC_CHECK", SOURCE_APP, f"Numeric claims check: {len(bad)} unsupported number(s)", {"unsupported": bad})
        rec.transition(WS.READY_FOR_REVIEW, "explanation checked")
        if bad:
            rec.finish(WS.COMPLETED, "Explanation withheld: contained figures not present in the comparison")
            session.commit()
            return _result(rec, True, "withheld", explanation=None, unsupported=bad)
        rec.finish(WS.COMPLETED, "Explanation passed numeric checks")
        session.commit()
        return _result(rec, True, "ok", explanation=res.parsed.summary, unsupported=[])
    except AIServiceError as exc:
        _audit_ai(session, "quote_explanation", client.model, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))
    except FlightDeskError as exc:
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))


# =========================================================================== inquiry workspace helpers
REREAD_FIELDS = ("origin", "destination", "trip_type", "departure_date", "return_date", "passenger_count",
                 "departure_time_preference", "special_requirements")


def reread_inquiry(session: Session, settings: Settings, inquiry_id: str, client: AIClient | None = None) -> WorkflowResult:
    """"Extract flight details": read the stored customer message again and suggest values for fields that are still
    empty. Nothing is changed here — the team applies suggestions with :func:`update_inquiry_fields`."""
    require(Permission.EDIT_OPERATIONS)
    inquiry = get_inquiry(session, inquiry_id)
    method = METHOD_OPENAI if client is not None else METHOD_RULE_BASED
    rec = WorkflowRecorder.start(session, WorkflowType.INQUIRY_REREAD, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode=method, model=client.model if client else None,
                                 input_summary=f"Re-read message for {inquiry_id}")
    session.commit()
    text = inquiry.original_text or ""
    try:
        rec.transition(WS.VALIDATING, f"extracting with {method}")
        if not text.strip():
            raise ValueError("There's no customer message stored for this inquiry, so there's nothing to read.")
        reference = inquiry.created_at.date()
        if client is not None:
            outcome = run_openai(text, reference, client)
            _audit_ai(session, "inquiry_reread", outcome.model, outcome.ok, outcome.usage, outcome.error_category, rec.run.id)
        else:
            outcome = run_rule_based(text, reference)
    except ValueError as exc:
        rec = _fail(session, rec.run.id, str(exc), settings, "validation_error")
        return _result(rec, False, str(exc))
    rec.add_usage(outcome.usage, outcome.model)
    if not outcome.ok or outcome.extraction is None:
        session.commit()
        rec = _fail(session, rec.run.id, outcome.error or "Reading failed", settings, outcome.error_category)
        return _result(rec, False, outcome.error or "Reading the message failed. Nothing was changed.",
                       error_category=outcome.error_category)
    proposed = extraction_to_create(outcome.extraction, inquiry.customer_label, text)
    suggestions: dict[str, Any] = {}
    differences: dict[str, tuple[Any, Any]] = {}
    for f in REREAD_FIELDS:
        current, new = getattr(inquiry, f), getattr(proposed, f)
        if new in (None, "", []):
            continue
        if current in (None, "", []):
            suggestions[f] = new
        elif str(current).strip().lower() != str(new).strip().lower():
            differences[f] = (current, new)
    rec.event("MODEL_OUTPUT" if client else "RULE_BASED_EXTRACTION", SOURCE_MODEL if client else SOURCE_APP,
              f"Read via {method}: {len(suggestions)} suggestion(s) for empty fields, {len(differences)} difference(s)",
              {"suggested_fields": list(suggestions), "different_fields": list(differences),
               "injection_signals": outcome.extraction.injection_signals})
    rec.transition(WS.READY_FOR_REVIEW, "suggestions ready for the team")
    rec.finish(WS.COMPLETED, "Suggestions shown; nothing changed automatically")
    session.commit()
    return _result(rec, True, "ok", suggestions=suggestions, differences=differences, method=method,
                   injection=bool(outcome.extraction.injection_signals))


def summarize_inquiry(session: Session, settings: Settings, inquiry_id: str, client: AIClient) -> WorkflowResult:
    """AI rephrasing of the deterministic inquiry facts; withheld if it adds any number that isn't in the facts."""
    from src.services.workspace_service import inquiry_facts

    require(Permission.VIEW_OPERATIONS)
    facts = inquiry_facts(session, inquiry_id)
    if not facts:
        raise NotFound(f"Inquiry {inquiry_id} not found.")
    rec = WorkflowRecorder.start(session, WorkflowType.INQUIRY_SUMMARY, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode="openai", model=client.model, input_summary=f"Summarize {inquiry_id}")
    session.commit()
    allowed = dict(facts)
    for key in ("departure_date", "return_date"):  # allow the date written as day / month / year
        if facts.get(key):
            y, m, d = (int(x) for x in str(facts[key]).split("-"))
            allowed[f"{key}_parts"] = [y, m, d]
    instructions = EXPLANATION_INSTRUCTIONS + ("\n- Summarize this charter inquiry for a team member in 2-3 sentences: who, "
                                               "what trip, what is missing, quotes so far, and the next step.")
    try:
        rec.transition(WS.VALIDATING, "facts computed in Python")
        res = client.parse(instructions=instructions, user_input=explanation_input(facts), schema=LLMExplanation,
                           purpose="inquiry_summary")
        rec.add_usage(res.usage, res.model)
        _audit_ai(session, "inquiry_summary", res.model, True, res.usage, None, rec.run.id)
        bad = unsupported_numbers(res.parsed.summary, allowed)
        rec.event("DETERMINISTIC_CHECK", SOURCE_APP, f"Numeric claims check: {len(bad)} unsupported number(s)", {"unsupported": bad})
        rec.transition(WS.READY_FOR_REVIEW, "summary checked")
        rec.finish(WS.COMPLETED, "Summary withheld" if bad else "Summary passed numeric checks")
        session.commit()
        return _result(rec, True, "withheld" if bad else "ok", summary=None if bad else res.parsed.summary, unsupported=bad)
    except AIServiceError as exc:
        _audit_ai(session, "inquiry_summary", client.model, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))



# =========================================================================== PDFs
FALSE_CLAIM_RE = re.compile(r"\b(?:(?:is|are|has been|have been|was|were|now)\s+(?:booked|confirmed|reserved|secured)"
                            r"|guarantee[ds]?|held for you)\b", re.I)


def read_operator_quote_pdf(session: Session, settings: Settings, inquiry_id: str, pdf_bytes: bytes,
                            client: AIClient | None = None) -> WorkflowResult:
    """Read an operator's quote PDF and pre-fill the quote form. Nothing is saved — the team checks and saves it.
    Without AI, the document text is returned so it can be copied into the form."""
    from src.ai.prompts import QUOTE_DOCUMENT_INSTRUCTIONS, document_input
    from src.exceptions import ValidationFailure
    from src.schemas import LLMQuoteExtraction
    from src.services.document_service import quote_prefill, read_pdf_text

    require(Permission.EDIT_OPERATIONS)
    get_inquiry(session, inquiry_id)
    rec = WorkflowRecorder.start(session, WorkflowType.QUOTE_DOCUMENT, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode="openai" if client else "none", model=client.model if client else None,
                                 input_summary=f"Operator quote PDF for {inquiry_id} ({len(pdf_bytes or b''):,} bytes)")
    session.commit()
    try:
        rec.transition(WS.VALIDATING, "reading the PDF's text")
        text = read_pdf_text(pdf_bytes)
    except ValidationFailure as exc:
        rec = _fail(session, rec.run.id, str(exc), settings, "validation_error")
        return _result(rec, False, str(exc))
    signals = detect_prompt_injection(text)
    rec.event("DETERMINISTIC_CHECK", SOURCE_APP, f"PDF text: {len(text):,} characters; injection signals: {len(signals)}",
              {"injection_signals": signals})
    if client is None:
        rec.transition(WS.READY_FOR_REVIEW, "text extracted; AI not available")
        rec.finish(WS.COMPLETED, "Text shown to the advisor; nothing pre-filled")
        session.commit()
        return _result(rec, True, "text_only", text=text, prefill={}, warnings=[], method="none")
    try:
        res = client.parse(instructions=QUOTE_DOCUMENT_INSTRUCTIONS, user_input=document_input(text),
                           schema=LLMQuoteExtraction, purpose="quote_document")
    except AIServiceError as exc:
        _audit_ai(session, "quote_document", client.model, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc), text=text)
    rec.add_usage(res.usage, res.model)
    _audit_ai(session, "quote_document", res.model, True, res.usage, None, rec.run.id)
    prefill, warnings = quote_prefill(res.parsed, text)
    if signals and not res.parsed.embedded_instructions_detected:
        warnings.append("The document contains instructions aimed at software. They were ignored.")
    rec.event("MODEL_OUTPUT", SOURCE_MODEL, f"Quote read: {len(prefill)} field(s) pre-filled, {len(warnings)} warning(s)",
              {"fields": sorted(prefill), "warnings": warnings})
    rec.transition(WS.READY_FOR_REVIEW, "pre-filled form ready for the advisor")
    rec.finish(WS.COMPLETED, "Advisor reviews and saves the quote")
    session.commit()
    return _result(rec, True, "ok", text=text, prefill=prefill, warnings=warnings, method="openai")


def write_cover_note(session: Session, settings: Settings, inquiry_id: str, client: AIClient) -> WorkflowResult:
    """AI-written cover note for the customer PDF; withheld (template used) if it mentions numbers not in the records."""
    from src.ai.prompts import COVER_NOTE_INSTRUCTIONS
    from src.schemas import LLMCoverNote
    from src.services.document_service import proposal_facts

    require(Permission.VIEW_OPERATIONS)
    facts = proposal_facts(session, inquiry_id)
    rec = WorkflowRecorder.start(session, WorkflowType.COVER_NOTE, settings=settings, inquiry_id=inquiry_id,
                                 ai_mode="openai", model=client.model, input_summary=f"Cover note for {inquiry_id}")
    session.commit()
    model_facts = {k: v for k, v in facts.items() if k != "show_operator_names"}
    for opt in model_facts["options"]:  # the note never needs operator names
        opt.pop("title", None)
    try:
        rec.transition(WS.VALIDATING, "facts from records")
        res = client.parse(instructions=COVER_NOTE_INSTRUCTIONS, user_input=explanation_input(model_facts),
                           schema=LLMCoverNote, purpose="cover_note")
    except AIServiceError as exc:
        _audit_ai(session, "cover_note", client.model, False, None, exc.category, rec.run.id)
        rec = _fail(session, rec.run.id, exc, settings)
        return _result(rec, False, str(exc))
    rec.add_usage(res.usage, res.model)
    _audit_ai(session, "cover_note", res.model, True, res.usage, None, rec.run.id)
    note = res.parsed.note.strip()
    bad = unsupported_numbers(note, model_facts)
    claims = FALSE_CLAIM_RE.findall(note)  # "is booked", "has been confirmed", "guaranteed"; "subject to confirmation" is fine
    rec.event("DETERMINISTIC_CHECK", SOURCE_APP, f"Cover note checks: {len(bad)} unsupported number(s), {len(claims)} claim word(s)",
              {"unsupported": bad, "claims": claims})
    rec.transition(WS.READY_FOR_REVIEW, "note checked")
    withheld = bool(bad or claims)
    rec.finish(WS.COMPLETED, "Note withheld; template used" if withheld else "Note passed checks")
    session.commit()
    return _result(rec, True, "withheld" if withheld else "ok", note=None if withheld else note, unsupported=bad, claims=claims)
