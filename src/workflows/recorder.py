"""Persistence for workflow runs: state transitions, timeline events, usage and outcomes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.ai.client import TokenUsage, estimate_cost_usd
from src.config import Settings
from src.exceptions import FlightDeskError, InvalidTransition
from src.models import WorkflowEvent, WorkflowRun, utcnow
from src.security import redact_secrets
from src.workflows.state_machine import (
    MAX_STATE_TRANSITIONS,
    TERMINAL_STATES,
    WorkflowState,
    WorkflowType,
    validate_transition,
)

# Event sources shown in the inspector to separate model output, deterministic logic, tools and humans.
SOURCE_MODEL = "model"
SOURCE_APP = "application"
SOURCE_TOOL = "tool"
SOURCE_HUMAN = "human"


def _safe_payload(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return redact_secrets(value)[:2000]
    if isinstance(value, dict):
        return {str(k): _safe_payload(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list | tuple):
        return [_safe_payload(v, depth + 1) for v in list(value)[:60]]
    return value


class WorkflowRecorder:
    def __init__(self, session: Session, run: WorkflowRun, settings: Settings | None = None) -> None:
        self.session = session
        self.run = run
        self.settings = settings
        # Read from the database (not the possibly stale relationship collection).
        self._seq = int(session.scalar(select(func.max(WorkflowEvent.seq)).where(WorkflowEvent.run_id == run.id)) or 0)

    @classmethod
    def start(cls, session: Session, workflow_type: WorkflowType, *, settings: Settings | None = None,
              inquiry_id: str | None = None, input_summary: str | None = None, ai_mode: str = "none",
              model: str | None = None, context: dict[str, Any] | None = None) -> WorkflowRecorder:
        run = WorkflowRun(workflow_type=workflow_type.value, inquiry_id=inquiry_id, state=WorkflowState.RECEIVED.value,
                          input_summary=(input_summary or "")[:300], ai_mode=ai_mode, model=model, context=context or {})
        session.add(run)
        session.flush()
        rec = cls(session, run, settings)
        rec.event("STATE_TRANSITION", SOURCE_APP, "Run started in RECEIVED", {"to": "RECEIVED", "workflow_type": workflow_type.value})
        return rec

    @classmethod
    def load(cls, session: Session, run_id: str, settings: Settings | None = None) -> WorkflowRecorder:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            raise FlightDeskError(f"Workflow run {run_id} not found.")
        return cls(session, run, settings)

    @property
    def state(self) -> WorkflowState:
        return WorkflowState(self.run.state)

    def event(self, kind: str, source: str, summary: str, payload: dict[str, Any] | None = None) -> WorkflowEvent:
        self._seq += 1
        ev = WorkflowEvent(run_id=self.run.id, seq=self._seq, kind=kind, source=source,
                           summary=redact_secrets(summary)[:500], payload=_safe_payload(payload or {}))
        self.session.add(ev)
        self.session.flush()
        return ev

    def transition(self, target: WorkflowState, reason: str = "") -> None:
        validate_transition(self.state, target)
        if self.run.step_count >= MAX_STATE_TRANSITIONS and target not in TERMINAL_STATES:
            raise InvalidTransition("Workflow exceeded its maximum number of steps.")
        previous = self.state
        self.run.state = target.value
        self.run.step_count += 1
        if target in TERMINAL_STATES:
            self.run.completed_at = utcnow()
            self.run.duration_ms = int((self.run.completed_at - self.run.started_at).total_seconds() * 1000)
        self.event("STATE_TRANSITION", SOURCE_APP, f"{previous} → {target}" + (f": {reason}" if reason else ""),
                   {"from": previous.value, "to": target.value, "reason": reason})

    def add_usage(self, usage: TokenUsage, model: str | None = None) -> None:
        self.run.input_tokens = (self.run.input_tokens or 0) + usage.input_tokens
        self.run.output_tokens = (self.run.output_tokens or 0) + usage.output_tokens
        if model:
            self.run.model = model
        if self.settings is not None and (self.run.input_tokens or self.run.output_tokens):
            self.run.estimated_cost_usd = estimate_cost_usd(TokenUsage(self.run.input_tokens, self.run.output_tokens), self.settings)

    def update_context(self, **values: Any) -> None:
        ctx = dict(self.run.context or {})
        ctx.update(values)
        self.run.context = ctx

    def fail(self, error: Exception | str, category: str | None = None) -> None:
        message = redact_secrets(str(error))[:500]
        self.run.error_category = category or getattr(error, "category", None) or "unexpected_error"
        self.run.error_message = message
        self.event("ERROR", SOURCE_APP, message, {"category": self.run.error_category})
        if self.state not in TERMINAL_STATES:
            self.transition(WorkflowState.FAILED, self.run.error_category)
        self.run.final_outcome = f"Failed: {message[:250]}"

    def finish(self, target: WorkflowState, outcome: str) -> None:
        self.transition(target, outcome[:200])
        self.run.final_outcome = outcome[:300]
