"""Registry of approved application tools.

The model may *request* a tool call; Python decides what happens:

* unknown tools, tools outside the workflow's allowlist, malformed JSON and invalid arguments are rejected;
* tools that accept an ``inquiry_id`` are confined to the inquiry the workflow is scoped to (least privilege);
* READ tools execute immediately and return data;
* PROPOSE tools never mutate anything — they create an :class:`ActionProposal` that a human must approve,
  and the mutation runs (with arguments re-validated against current state) only after approval;
* INTERNAL tools are never exposed to the model.

There is no SQL tool, no filesystem tool and no code-execution tool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from src.auth import Permission as AuthPermission
from src.auth import current_principal, has_permission
from src.config import Settings
from src.exceptions import FlightDeskError, PermissionDenied
from src.models import ActionProposal, utcnow
from src.services.audit_service import record_audit
from src.workflows.recorder import SOURCE_APP, SOURCE_HUMAN, SOURCE_TOOL, WorkflowRecorder

logger = logging.getLogger(__name__)

_STRIP_KEYS = {"default", "title", "maxLength", "minLength", "minimum", "maximum", "maxItems", "minItems"}
MAX_RESULT_CHARS = 6000


class Permission(StrEnum):
    READ = "read"
    PROPOSE = "propose"  # creates a proposal; mutation requires human approval
    INTERNAL = "internal"  # application-only (never offered to the model)


AGENT_ACTORS = {"model", "planner"}


@dataclass
class ToolContext:
    session: Session
    actor: str  # model | planner | advisor | system
    settings: Settings | None = None
    recorder: WorkflowRecorder | None = None
    inquiry_scope: str | None = None
    injected_failures: frozenset[str] = frozenset()  # evaluation-only fault injection


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    permission: Permission
    handler: Callable[[ToolContext, Any], dict[str, Any]]
    executor: Callable[[ToolContext, Any, dict[str, Any]], dict[str, Any]] | None = None
    mutates: bool = False
    requires: AuthPermission = AuthPermission.VIEW_OPERATIONS  # role permission needed to use this tool

    @property
    def exposed_to_model(self) -> bool:
        return self.permission != Permission.INTERNAL


@dataclass
class ToolResult:
    status: str  # executed | proposed | rejected
    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    proposal_id: str | None = None

    def to_model_output(self) -> str:
        """What the model sees. Rejections are explicit — a rejected call is never reported as success."""
        payload: dict[str, Any] = {"status": self.status}
        if self.error:
            payload["error"] = self.error
        if self.proposal_id:
            payload["proposal_id"] = self.proposal_id
            payload["note"] = "Pending human approval. Nothing has been changed yet."
        if self.data:
            payload["data"] = self.data
        text = json.dumps(payload, default=str)
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '…"}'


def _strip(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k not in _STRIP_KEYS}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name} already registered")
        if spec.permission == Permission.PROPOSE and spec.executor is None:
            raise ValueError(f"Propose tool {spec.name} needs an executor")
        if spec.mutates and spec.permission == Permission.READ:
            raise ValueError(f"Tool {spec.name} mutates data and cannot be READ")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self, allowlist: set[str]) -> list[dict[str, Any]]:
        tools = []
        for name in sorted(allowlist):
            spec = self._tools[name]
            if not spec.exposed_to_model:
                continue
            if current_principal() is not None and not has_permission(spec.requires):
                continue  # the model is never offered tools the signed-in user may not use
            tools.append({"type": "function", "name": spec.name, "description": spec.description,
                          "parameters": _strip(to_strict_json_schema(spec.input_model)), "strict": True})
        return tools

    def _reject(self, ctx: ToolContext, name: str, reason: str, args_preview: Any = None) -> ToolResult:
        if ctx.recorder:
            ctx.recorder.event("TOOL_CALL_REJECTED", SOURCE_APP, f"Rejected {name}: {reason}", {"tool": name, "reason": reason,
                                                                                              "arguments": args_preview})
        record_audit(ctx.session, "tool_call_rejected", actor="model" if ctx.actor in AGENT_ACTORS else ctx.actor,
                     entity_type="workflow_run", entity_id=ctx.recorder.run.id if ctx.recorder else None,
                     metadata={"tool": name[:60], "reason": reason}, outcome="rejected")
        return ToolResult(status="rejected", tool=name, error=reason)

    def dispatch(self, ctx: ToolContext, name: str, raw_args: str | dict[str, Any], allowlist: set[str]) -> ToolResult:
        """Validate and run (READ) or propose (PROPOSE) a tool call requested by an agent."""
        spec = self._tools.get(name)
        if spec is None:
            return self._reject(ctx, name, "Unknown tool. Only registered tools may be called.")
        if name not in allowlist or (ctx.actor in AGENT_ACTORS and not spec.exposed_to_model):
            return self._reject(ctx, name, "Tool is not permitted in this workflow.")
        if not has_permission(spec.requires):
            return self._reject(ctx, name, "The signed-in user's role does not have access to this tool or data.")
        try:
            data = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            if not isinstance(data, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, TypeError) as exc:
            return self._reject(ctx, name, f"Arguments are not valid JSON: {str(exc)[:120]}")
        try:
            args = spec.input_model.model_validate(data)
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return self._reject(ctx, name, f"Invalid arguments — {errs}", data)
        scoped_id = getattr(args, "inquiry_id", None)
        if ctx.inquiry_scope and scoped_id is not None and scoped_id != ctx.inquiry_scope:
            return self._reject(ctx, name, f"Access outside this workflow's inquiry ({ctx.inquiry_scope}) is not permitted.",
                                data)
        if ctx.recorder:
            ctx.recorder.event("TOOL_CALL_REQUESTED", "model" if ctx.actor == "model" else SOURCE_APP,
                               f"{'Model' if ctx.actor == 'model' else 'Planner'} requested {name}",
                               {"tool": name, "arguments": args.model_dump(mode="json"), "permission": spec.permission.value})
        try:
            if name in ctx.injected_failures:
                raise FlightDeskError("The records service is temporarily unavailable. Please try again shortly.")
            result = spec.handler(ctx, args)
        except FlightDeskError as exc:
            return self._reject(ctx, name, str(exc), data)
        except Exception:
            logger.exception("Tool handler crashed tool=%s", name)
            return self._reject(ctx, name, "Internal tool error.", data)

        if spec.permission == Permission.READ:
            if ctx.recorder:
                ctx.recorder.event("TOOL_CALL_EXECUTED", SOURCE_TOOL, f"{name} returned data",
                                   {"tool": name, "result_preview": json.dumps(result, default=str)[:1500]})
            return ToolResult(status="executed", tool=name, data=result)

        # PROPOSE: record a pending proposal; nothing is mutated here.
        if ctx.recorder is None:
            raise PermissionDenied("Proposals require a workflow run.")
        proposal = ActionProposal(run_id=ctx.recorder.run.id, tool_name=name,
                                  arguments={**args.model_dump(mode="json"), **result.get("proposal_payload", {})},
                                  rationale=str(result.get("rationale", ""))[:500], status="pending")
        ctx.session.add(proposal)
        ctx.session.flush()
        ctx.recorder.event("PROPOSAL_CREATED", SOURCE_APP, f"Proposal {proposal.id}: {result.get('summary', name)}",
                           {"proposal_id": proposal.id, "tool": name, "arguments": proposal.arguments})
        return ToolResult(status="proposed", tool=name, proposal_id=proposal.id,
                          data={k: v for k, v in result.items() if k != "proposal_payload"})

    def execute_proposal(self, ctx: ToolContext, proposal: ActionProposal) -> dict[str, Any]:
        """Run an approved proposal's mutation. Arguments are re-validated; business rules re-checked."""
        if ctx.actor != "advisor":
            raise PermissionDenied("Only an advisor can approve and execute a proposal.")
        if proposal.status != "pending":
            raise PermissionDenied(f"Proposal {proposal.id} is already {proposal.status}.")
        spec = self._tools.get(proposal.tool_name)
        if spec is None or spec.executor is None:
            raise PermissionDenied(f"No executor for {proposal.tool_name}.")
        if not has_permission(spec.requires):
            raise PermissionDenied("Your role can't approve this action.")
        payload = dict(proposal.arguments or {})
        base_fields = set(spec.input_model.model_fields)
        args = spec.input_model.model_validate({k: v for k, v in payload.items() if k in base_fields})
        result = spec.executor(ctx, args, payload)
        proposal.status = "executed"
        proposal.decided_at = utcnow()
        proposal.decided_by = "advisor"
        proposal.result = result
        if ctx.recorder:
            ctx.recorder.event("HUMAN_DECISION", SOURCE_HUMAN, f"Advisor approved {proposal.id} ({proposal.tool_name})",
                               {"proposal_id": proposal.id, "decision": "approved"})
            ctx.recorder.event("TOOL_CALL_EXECUTED", SOURCE_TOOL, f"Executed approved {proposal.tool_name}",
                               {"proposal_id": proposal.id, "result": result})
        record_audit(ctx.session, "proposal_executed", actor="advisor", entity_type="action_proposal", entity_id=proposal.id,
                     metadata={"tool": proposal.tool_name, "run_id": proposal.run_id})
        return result
