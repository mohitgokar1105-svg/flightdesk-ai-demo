"""AI service abstraction over the OpenAI Responses API.

* :class:`AIClient` is the small interface the rest of the application depends on, so model
  configuration can change without touching callers and tests can substitute fakes.
* :class:`OpenAIClient` uses the official SDK (``responses.parse`` for structured outputs,
  ``responses.create`` for function calling) with *our own* bounded retry loop so that every retry
  decision is explicit and observable. SDK-internal retries are disabled.
* Validation failures are never retried as though they were network failures.
* There is deliberately no "fake AI" fallback: when live AI is unavailable, callers receive
  :class:`AINotConfigured` and must offer a clearly labelled alternative.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

import openai
from pydantic import BaseModel, ValidationError

from src.config import Settings
from src.exceptions import (
    AINotConfigured,
    AIRequestRejected,
    AIResponseInvalid,
    AIServiceError,
    AITransientError,
)
from src.security import redact_secrets

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

RETRY_BACKOFF_SECONDS = (0.5, 1.5, 3.0, 5.0, 8.0)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


def estimate_cost_usd(usage: TokenUsage, settings: Settings) -> float:
    """Cost estimate from configured per-million-token prices (assumed list prices; verify with OpenAI)."""
    return round(
        usage.input_tokens / 1_000_000 * settings.price_input_per_1m
        + usage.output_tokens / 1_000_000 * settings.price_output_per_1m,
        6,
    )


@dataclass
class StructuredResult(Generic[T]):
    parsed: T
    usage: TokenUsage
    model: str
    latency_ms: float
    attempts: int
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRequest:
    call_id: str
    name: str
    arguments_json: str


@dataclass
class ToolTurn:
    """One model turn in a tool-calling loop."""

    tool_calls: list[ToolCallRequest]
    text: str
    usage: TokenUsage
    model: str
    latency_ms: float
    attempts: int
    output_items: list[Any]  # opaque provider items to append to the next request's input


class AIClient(Protocol):
    model: str

    def parse(self, *, instructions: str, user_input: str, schema: type[T], purpose: str) -> StructuredResult[T]: ...

    def tool_turn(self, *, instructions: str, input_items: list[Any], tools: list[dict[str, Any]], purpose: str) -> ToolTurn: ...


def _supports_temperature(model: str) -> bool:
    # Reasoning-model families reject `temperature`; GPT-4.x accepts it.
    return model.startswith(("gpt-4", "gpt-3.5"))


def classify_openai_error(exc: Exception) -> AIServiceError:
    """Map SDK exceptions to application error classes (transient vs. permanent)."""
    msg = redact_secrets(str(exc))[:300]
    if isinstance(exc, openai.RateLimitError):
        return AITransientError(f"Rate limited by the AI provider. ({msg})")
    if isinstance(exc, openai.APITimeoutError):
        return AITransientError("The AI provider timed out.")
    if isinstance(exc, openai.APIConnectionError):
        return AITransientError("Could not connect to the AI provider.")
    if isinstance(exc, openai.InternalServerError):
        return AITransientError(f"The AI provider returned a server error. ({msg})")
    if isinstance(exc, openai.AuthenticationError):
        return AIRequestRejected("The AI provider rejected the API key (authentication failed).")
    if isinstance(exc, openai.PermissionDeniedError):
        return AIRequestRejected("The API key does not have permission for this model or operation.")
    if isinstance(exc, openai.NotFoundError):
        return AIRequestRejected(f"Model or endpoint not found. Check OPENAI_MODEL. ({msg})")
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        return AIRequestRejected(f"The AI provider rejected the request. ({msg})")
    if isinstance(exc, openai.LengthFinishReasonError | openai.ContentFilterFinishReasonError):
        return AIResponseInvalid("The model response was truncated or filtered.")
    if isinstance(exc, ValidationError | json.JSONDecodeError):
        return AIResponseInvalid("The model output did not match the required schema.")
    if isinstance(exc, openai.APIStatusError):
        return AIRequestRejected(f"AI provider error {exc.status_code}. ({msg})")
    return AIServiceError(f"Unexpected AI client error: {type(exc).__name__}")


class OpenAIClient:
    """Thin, observable wrapper over the OpenAI Responses API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        http_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise AINotConfigured("OPENAI_API_KEY is not configured.")
        self.model = model
        self.max_retries = max(0, min(max_retries, len(RETRY_BACKOFF_SECONDS)))
        self._sleep = sleep
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_seconds, "max_retries": 0}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = openai.OpenAI(**kwargs)

    def _with_retries(self, purpose: str, fn: Callable[[], Any]) -> tuple[Any, int, float]:
        attempts = 0
        start = time.perf_counter()
        while True:
            attempts += 1
            try:
                result = fn()
                return result, attempts, (time.perf_counter() - start) * 1000
            except Exception as exc:  # classified below; nothing is swallowed silently
                err = exc if isinstance(exc, AIServiceError) else classify_openai_error(exc)
                retryable = err.transient and attempts <= self.max_retries
                logger.warning("AI call failed purpose=%s attempt=%s category=%s retry=%s",
                               purpose, attempts, err.category, retryable)
                if not retryable:
                    raise err from exc
                self._sleep(RETRY_BACKOFF_SECONDS[attempts - 1])

    @staticmethod
    def _usage(resp: Any) -> TokenUsage:
        u = getattr(resp, "usage", None)
        if u is None:
            return TokenUsage()
        return TokenUsage(int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0))

    def parse(self, *, instructions: str, user_input: str, schema: type[T], purpose: str) -> StructuredResult[T]:
        kwargs: dict[str, Any] = {"model": self.model, "instructions": instructions, "input": user_input,
                                  "text_format": schema, "store": False}
        if _supports_temperature(self.model):
            kwargs["temperature"] = 0

        def call() -> Any:
            resp = self._client.responses.parse(**kwargs)
            if getattr(resp, "status", None) == "incomplete":
                raise AIResponseInvalid("The model response was incomplete (likely truncated).")
            parsed = resp.output_parsed
            if parsed is None:
                raise AIResponseInvalid("The model returned no structured output (possibly a refusal).")
            if not isinstance(parsed, schema):
                parsed = schema.model_validate(parsed)
            return resp, parsed

        (resp, parsed), attempts, latency = self._with_retries(purpose, call)
        usage = self._usage(resp)
        logger.info("AI call ok purpose=%s model=%s attempts=%s in_tokens=%s out_tokens=%s latency_ms=%.0f",
                    purpose, self.model, attempts, usage.input_tokens, usage.output_tokens, latency)
        return StructuredResult(parsed=parsed, usage=usage, model=getattr(resp, "model", self.model) or self.model,
                                latency_ms=latency, attempts=attempts, raw_json=parsed.model_dump(mode="json"))

    def tool_turn(self, *, instructions: str, input_items: list[Any], tools: list[dict[str, Any]], purpose: str) -> ToolTurn:
        kwargs: dict[str, Any] = {"model": self.model, "instructions": instructions, "input": input_items,
                                  "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False, "store": False}
        if _supports_temperature(self.model):
            kwargs["temperature"] = 0

        resp, attempts, latency = self._with_retries(purpose, lambda: self._client.responses.create(**kwargs))
        calls: list[ToolCallRequest] = []
        output_items: list[Any] = []
        for item in resp.output or []:
            dumped = item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item
            output_items.append(dumped)
            if getattr(item, "type", None) == "function_call":
                calls.append(ToolCallRequest(call_id=item.call_id, name=item.name, arguments_json=item.arguments or "{}"))
        return ToolTurn(tool_calls=calls, text=getattr(resp, "output_text", "") or "", usage=self._usage(resp),
                        model=getattr(resp, "model", self.model) or self.model, latency_ms=latency,
                        attempts=attempts, output_items=output_items)


def build_ai_client(settings: Settings, http_client: Any | None = None) -> OpenAIClient:
    """Create the live client, or raise :class:`AINotConfigured` with a user-presentable reason."""
    if settings.demo_mode:
        raise AINotConfigured("Demo mode is enabled (DEMO_MODE=true); live AI calls are disabled.")
    if not settings.ai_key_configured:
        raise AINotConfigured("OPENAI_API_KEY is not configured; live AI calls are unavailable.")
    assert settings.openai_api_key is not None
    return OpenAIClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
        http_client=http_client,
    )
