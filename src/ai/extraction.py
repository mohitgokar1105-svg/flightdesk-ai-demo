"""Inquiry extraction orchestration.

Two approaches share the same deterministic post-processing (:func:`src.ai.validation.post_process`):

* ``rule_based`` — :mod:`src.ai.rule_based` (no AI; labelled as such in the UI)
* ``openai`` — structured output from the configured OpenAI model, validated with Pydantic

Model output is converted field-by-field; malformed values become validation issues rather than
being silently coerced. Nothing here persists data — saving requires explicit advisor approval.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.ai.client import AIClient, TokenUsage
from src.ai.prompts import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION, extraction_input
from src.ai.rule_based import extract_rule_based
from src.ai.text_parsing import parse_iso_date
from src.ai.validation import post_process
from src.exceptions import AIServiceError
from src.schemas import DateSpec, InquiryExtraction, LLMDateSpec, LLMInquiryExtraction, ValidationIssue
from src.security import text_fingerprint

logger = logging.getLogger(__name__)

METHOD_RULE_BASED = "rule_based"
METHOD_OPENAI = "openai"
MAX_INQUIRY_CHARS = 6000


@dataclass
class ExtractionOutcome:
    method: str
    extraction: InquiryExtraction | None
    schema_valid: bool
    latency_ms: float
    model: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: str | None = None
    error_category: str | None = None
    attempts: int = 0
    raw_output: dict[str, Any] | None = None
    prompt_version: str | None = None

    @property
    def ok(self) -> bool:
        return self.extraction is not None


def _date_spec(raw: LLMDateSpec, field_name: str, issues: list[ValidationIssue]) -> DateSpec:
    values: dict[str, date | None] = {}
    for part in ("exact", "earliest", "latest"):
        value = getattr(raw, part)
        try:
            values[part] = parse_iso_date(value)
        except ValueError:
            values[part] = None
            issues.append(ValidationIssue(code="DATE_INVALID", severity="error", field=field_name,
                                          message=f"Model returned an invalid {field_name} date '{str(value)[:20]}'; it was discarded."))
    exact, lo, hi = values["exact"], values["earliest"], values["latest"]
    if exact is not None and lo in (None, exact) and hi in (None, exact):  # "Nov 14" sometimes comes back as Nov 14–Nov 14
        lo = hi = None
    return DateSpec(exact=exact, earliest=lo, latest=hi, source_text=raw.source_text)


def normalize_llm_output(raw: LLMInquiryExtraction) -> InquiryExtraction:
    """Convert validated model output into the domain schema (before deterministic post-processing)."""
    issues: list[ValidationIssue] = []
    departure = _date_spec(raw.departure, "departure", issues)
    ret = _date_spec(raw.return_date, "return_date", issues)
    evidence = {e.field: e.quote[:200] for e in raw.evidence}
    return InquiryExtraction(
        origin=raw.origin,
        destination=raw.destination,
        trip_type=raw.trip_type,
        departure=departure,
        return_date=ret,
        passenger_count=raw.passenger_count,
        passenger_counts_mentioned=[n for n in raw.passenger_counts_mentioned if isinstance(n, int)],
        trip_purpose=raw.trip_purpose,
        date_flexibility=raw.date_flexibility,
        budget_amount=raw.budget_amount,
        currency=raw.currency,
        requested_services=list(raw.requested_services),
        ambiguities=[ValidationIssue(code="MODEL_FLAGGED_AMBIGUITY", severity="info", field=a.field, message=a.description[:300])
                     for a in raw.ambiguities],
        inconsistencies=[ValidationIssue(code="MODEL_FLAGGED_INCONSISTENCY", severity="info", field=a.field, message=a.description[:300])
                         for a in raw.inconsistencies],
        validation_issues=issues,
        evidence=evidence,
        injection_signals=["model_flagged"] if raw.embedded_instructions_detected else [],
        method=METHOD_OPENAI,
        customer_name=(raw.customer_name or "").strip()[:120] or None,
        contact_email=(raw.contact_email or "").strip().lower()[:200] or None,
        contact_phone=(raw.contact_phone or "").strip()[:40] or None,
        departure_time_preference=(raw.departure_time_preference or "").strip()[:40] or None,
        special_requirements=[r.strip()[:120] for r in raw.special_requirements if r and r.strip()][:12],
        model_inferred_fields=[f for f in raw.inferred_fields if isinstance(f, str)][:20],
    )


def _check_input(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("The inquiry text is empty.")
    if len(text) > MAX_INQUIRY_CHARS:
        raise ValueError(f"The inquiry is longer than {MAX_INQUIRY_CHARS} characters; please shorten it.")
    return text


def run_rule_based(text: str, reference_date: date) -> ExtractionOutcome:
    start = time.perf_counter()
    text = _check_input(text)
    extraction = post_process(extract_rule_based(text, reference_date), text, reference_date)
    return ExtractionOutcome(method=METHOD_RULE_BASED, extraction=extraction, schema_valid=True,
                             latency_ms=(time.perf_counter() - start) * 1000, prompt_version=None)


def run_openai(text: str, reference_date: date, client: AIClient) -> ExtractionOutcome:
    """Call the model for structured extraction. Errors are returned (not raised) with a safe message."""
    text = _check_input(text)
    start = time.perf_counter()
    logger.info("Extraction requested method=openai input=%s", text_fingerprint(text))
    try:
        result = client.parse(
            instructions=EXTRACTION_INSTRUCTIONS,
            user_input=extraction_input(text, reference_date),
            schema=LLMInquiryExtraction,
            purpose="inquiry_extraction",
        )
    except AIServiceError as exc:
        return ExtractionOutcome(method=METHOD_OPENAI, extraction=None, schema_valid=False,
                                 latency_ms=(time.perf_counter() - start) * 1000, model=client.model,
                                 error=str(exc), error_category=exc.category, prompt_version=EXTRACTION_PROMPT_VERSION)
    extraction = post_process(normalize_llm_output(result.parsed), text, reference_date)
    return ExtractionOutcome(method=METHOD_OPENAI, extraction=extraction, schema_valid=True,
                             latency_ms=(time.perf_counter() - start) * 1000, model=result.model, usage=result.usage,
                             attempts=result.attempts, raw_output=result.raw_json, prompt_version=EXTRACTION_PROMPT_VERSION)
