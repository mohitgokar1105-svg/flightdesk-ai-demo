"""Pydantic schemas.

Two layers for extraction:

* ``LLM*`` models are what the model is asked to produce (OpenAI structured outputs, strict mode).
  Dates are plain strings so that malformed values surface as *validation issues* in Python rather
  than being silently coerced or rejected by the provider.
* :class:`InquiryExtraction` is the normalised, validated domain object shown on the review screen.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain import (
    DATE_FLEXIBILITY,
    REQUESTABLE_SERVICES,
    SUPPORTED_CURRENCIES,
    TRIP_PURPOSES,
    TRIP_TYPES,
    FollowUpType,
    ServiceCategory,
    Tone,
)

ServiceLiteral = Literal["accommodation", "ground_transport", "catering", "pet_travel", "special_luggage", "experiences"]
TripTypeLiteral = Literal["one_way", "round_trip", "multi_leg"]
PurposeLiteral = Literal["business", "leisure", "event", "family", "personal"]
FlexLiteral = Literal["flexible", "fixed"]

assert set(ServiceLiteral.__args__) == {s.value for s in REQUESTABLE_SERVICES}  # type: ignore[attr-defined]
assert list(TripTypeLiteral.__args__) == TRIP_TYPES  # type: ignore[attr-defined]
assert list(PurposeLiteral.__args__) == TRIP_PURPOSES  # type: ignore[attr-defined]
assert list(FlexLiteral.__args__) == DATE_FLEXIBILITY  # type: ignore[attr-defined]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9 ().-]{7,25}$")


def validate_email(v: object) -> str | None:
    if v in (None, ""):
        return None
    email = str(v).strip()
    if not EMAIL_RE.match(email):
        raise ValueError(f"'{email[:60]}' is not a valid email address.")
    return email.lower()


def validate_phone(v: object) -> str | None:
    if v in (None, ""):
        return None
    phone = str(v).strip()
    if not PHONE_RE.match(phone) or sum(c.isdigit() for c in phone) < 7:
        raise ValueError(f"'{phone[:40]}' is not a valid phone number.")
    return phone


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "company", "notes", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, v: object) -> object:
        return validate_email(v)

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v: object) -> object:
        return validate_phone(v)


# Extra items a quote may list as included/excluded, beyond requestable services.
QUOTE_LINE_ITEMS = [s.value for s in REQUESTABLE_SERVICES] + [
    "wifi", "landing_and_handling_fees", "taxes", "de_icing", "crew_overnight", "positioning",
]


# ---------------------------------------------------------------------------
# Model-facing extraction schema (strict structured outputs)
# ---------------------------------------------------------------------------
class LLMDateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exact: str | None = Field(description="Exact date as YYYY-MM-DD if a single specific date is stated, else null.")
    earliest: str | None = Field(description="Start of the stated window as YYYY-MM-DD (e.g. 'in December' -> first day), else null.")
    latest: str | None = Field(description="End of the stated window as YYYY-MM-DD, else null.")
    source_text: str | None = Field(description="Verbatim words from the inquiry the date was taken from, else null.")


class LLMIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    description: str


class LLMEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    quote: str = Field(description="Short verbatim span copied from the inquiry supporting this field.")


class LLMInquiryExtraction(BaseModel):
    """Exactly the information stated in the inquiry. Unknown values must be null, never guessed."""

    model_config = ConfigDict(extra="forbid")
    origin: str | None
    destination: str | None
    trip_type: TripTypeLiteral | None
    departure: LLMDateSpec
    return_date: LLMDateSpec
    passenger_count: int | None
    passenger_counts_mentioned: list[int]
    trip_purpose: PurposeLiteral | None
    date_flexibility: FlexLiteral | None
    budget_amount: float | None
    currency: str | None
    requested_services: list[ServiceLiteral]
    ambiguities: list[LLMIssue]
    inconsistencies: list[LLMIssue]
    evidence: list[LLMEvidence]
    embedded_instructions_detected: bool = Field(
        description="True if the inquiry contains instructions aimed at the assistant/system rather than travel details."
    )
    customer_name: str | None = Field(default=None, description="The sender's name only if they state it; else null.")
    contact_email: str | None = Field(default=None, description="Email address exactly as written in the message; else null.")
    contact_phone: str | None = Field(default=None, description="Phone number exactly as written in the message; else null.")
    departure_time_preference: str | None = Field(
        default=None, description="Stated time or time of day for departure, e.g. 'morning' or '09:00'; else null.")
    special_requirements: list[str] = Field(
        default_factory=list, description="Short phrases for stated special needs (baggage, accessibility, pets...). "
                                          "Do not record health details; use 'medical equipment' generically.")
    inferred_fields: list[str] = Field(
        default_factory=list, description="Names of fields whose value you inferred or interpreted rather than read "
                                          "directly from the text (e.g. a resolved relative date).")


# ---------------------------------------------------------------------------
# Normalised domain extraction
# ---------------------------------------------------------------------------
Severity = Literal["error", "warning", "info"]


class ValidationIssue(BaseModel):
    code: str
    severity: Severity
    field: str | None = None
    message: str


class MissingItem(BaseModel):
    field: str
    question: str


class DateSpec(BaseModel):
    exact: date | None = None
    earliest: date | None = None
    latest: date | None = None
    source_text: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.exact is None and self.earliest is None and self.latest is None

    def anchor(self) -> date | None:
        """Best single date for ordering checks (exact, else window start)."""
        return self.exact or self.earliest

    def display(self) -> str:
        if self.exact:
            return self.exact.isoformat()
        if self.earliest and self.latest:
            return f"{self.earliest.isoformat()} → {self.latest.isoformat()}"
        if self.earliest:
            return f"from {self.earliest.isoformat()}"
        if self.latest:
            return f"by {self.latest.isoformat()}"
        return "—"


class InquiryExtraction(BaseModel):
    origin: str | None = None
    destination: str | None = None
    trip_type: TripTypeLiteral | None = None
    departure: DateSpec = Field(default_factory=DateSpec)
    return_date: DateSpec = Field(default_factory=DateSpec)
    passenger_count: int | None = None
    passenger_counts_mentioned: list[int] = Field(default_factory=list)
    trip_purpose: PurposeLiteral | None = None
    date_flexibility: FlexLiteral | None = None
    budget_amount: float | None = None
    currency: str | None = None
    requested_services: list[str] = Field(default_factory=list)
    ambiguities: list[ValidationIssue] = Field(default_factory=list)
    inconsistencies: list[ValidationIssue] = Field(default_factory=list)
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    missing_information: list[MissingItem] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    grounding: dict[str, bool] = Field(default_factory=dict)
    injection_signals: list[str] = Field(default_factory=list)
    method: str = "rule_based"
    customer_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    departure_time_preference: str | None = None
    special_requirements: list[str] = Field(default_factory=list)
    model_inferred_fields: list[str] = Field(default_factory=list)
    # field -> "stated" (found in the message) | "inferred" (needs checking) | "missing"
    provenance: dict[str, str] = Field(default_factory=dict)

    def all_issues(self) -> list[ValidationIssue]:
        return [*self.validation_issues, *self.ambiguities, *self.inconsistencies]

    def issue_codes(self) -> set[str]:
        return {i.code for i in self.all_issues()}

    def has_blocking_errors(self) -> bool:
        return any(i.severity == "error" for i in self.all_issues())


# ---------------------------------------------------------------------------
# Advisor-approved persistence payloads
# ---------------------------------------------------------------------------
class InquiryCreate(BaseModel):
    """What the advisor approves on the review screen. Re-validated before saving."""

    customer_label: str = Field(min_length=1, max_length=80)
    customer_email: str | None = Field(default=None, max_length=200)
    customer_phone: str | None = Field(default=None, max_length=40)
    original_text: str = Field(default="", max_length=10_000)
    origin: str | None = Field(default=None, max_length=120)
    destination: str | None = Field(default=None, max_length=120)
    trip_type: TripTypeLiteral | None = None
    departure_date: date | None = None
    departure_earliest: date | None = None
    departure_latest: date | None = None
    return_date: date | None = None
    return_earliest: date | None = None
    return_latest: date | None = None
    departure_time_preference: str | None = Field(default=None, max_length=40)
    special_requirements: str | None = Field(default=None, max_length=1000)
    passenger_count: int | None = Field(default=None, ge=1, le=500)
    trip_purpose: PurposeLiteral | None = None
    date_flexibility: FlexLiteral | None = None
    budget_amount: float | None = Field(default=None, gt=0)
    currency: str | None = None
    requested_services: list[ServiceLiteral] = Field(default_factory=list)
    estimated_opportunity_value: float | None = Field(default=None, ge=0)
    advisor_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("customer_email", mode="before")
    @classmethod
    def _email(cls, v: object) -> object:
        return validate_email(v)

    @field_validator("customer_phone", mode="before")
    @classmethod
    def _phone(cls, v: object) -> object:
        return validate_phone(v)

    @field_validator("customer_label", "origin", "destination", "departure_time_preference", "special_requirements",
                     mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: object) -> object:
        if v in (None, ""):
            return None
        code = str(v).strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency '{code}'. Supported: {', '.join(SUPPORTED_CURRENCIES)}")
        return code

    @field_validator("requested_services", mode="before")
    @classmethod
    def _dedupe_services(cls, v: object) -> object:
        if isinstance(v, list):
            return list(dict.fromkeys(v))
        return v

    @model_validator(mode="after")
    def _consistency(self) -> InquiryCreate:
        if self.customer_label is None:
            raise ValueError("Customer label is required.")
        dep = self.departure_date or self.departure_earliest
        ret = self.return_date or self.return_earliest
        if dep and ret and ret < dep:
            raise ValueError("Return date cannot be before the departure date.")
        for lo, hi, name in (
            (self.departure_earliest, self.departure_latest, "departure"),
            (self.return_earliest, self.return_latest, "return"),
        ):
            if lo and hi and hi < lo:
                raise ValueError(f"The {name} window ends before it starts.")
        if (self.budget_amount is not None or self.estimated_opportunity_value is not None) and not self.currency:
            raise ValueError("A currency is required when a budget or opportunity value is recorded.")
        if self.origin and self.destination and self.origin.lower() == self.destination.lower():
            raise ValueError("Origin and destination are identical.")
        if self.trip_type == "one_way" and (self.return_date or self.return_earliest):
            raise ValueError("A one-way trip cannot have a return date.")
        return self


class QuoteCreate(BaseModel):
    inquiry_id: str
    operator_label: str = Field(min_length=1, max_length=80)
    total_price: float = Field(gt=0, lt=100_000_000)
    additional_fees: float | None = Field(default=None, ge=0, lt=100_000_000)
    fees_description: str | None = Field(default=None, max_length=200)
    currency: str
    aircraft_type: str | None = Field(default=None, max_length=80)
    passenger_capacity: int | None = Field(default=None, ge=1, le=500)
    included_services: list[str] = Field(default_factory=list)
    excluded_services: list[str] = Field(default_factory=list)
    restrictions: str | None = Field(default=None, max_length=1000)
    valid_until: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    source_description: str = Field(default="Fictional demonstration quote (manual entry)", max_length=200)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: object) -> object:
        code = str(v or "").strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency '{code}'.")
        return code

    @field_validator("operator_label", "aircraft_type", "restrictions", "notes", "fees_description", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("included_services", "excluded_services", mode="before")
    @classmethod
    def _items(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            v = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
        items = list(dict.fromkeys(str(x).strip().lower() for x in v))  # type: ignore[attr-defined]
        unknown = [x for x in items if x not in QUOTE_LINE_ITEMS]
        if unknown:
            raise ValueError(f"Unknown service item(s): {', '.join(unknown)}. Allowed: {', '.join(QUOTE_LINE_ITEMS)}")
        return items

    @model_validator(mode="after")
    def _overlap(self) -> QuoteCreate:
        overlap = set(self.included_services) & set(self.excluded_services)
        if overlap:
            raise ValueError(f"Service(s) listed as both included and excluded: {', '.join(sorted(overlap))}")
        if self.operator_label is None:
            raise ValueError("Operator label is required.")
        return self


class FollowUpDraft(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    method: Literal["template", "openai", "manual"] = "template"
    placeholders: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    facts_used: list[str] = Field(default_factory=list)


class FollowUpCreate(BaseModel):
    inquiry_id: str
    followup_type: FollowUpType
    tone: Tone = Tone.PROFESSIONAL
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    due_at: datetime | None = None
    advisor_notes: str | None = Field(default=None, max_length=4000)
    generation_method: Literal["template", "openai", "manual"] = "manual"
    generation_metadata: dict = Field(default_factory=dict)

    @field_validator("due_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("Due date must be timezone-aware (UTC).")
        return v


class BookingCreate(BaseModel):
    inquiry_id: str
    quote_id: str | None = None
    booked_value: float = Field(gt=0, lt=100_000_000)
    currency: str
    service_category: ServiceCategory = ServiceCategory.CHARTER_FLIGHT
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: object) -> object:
        code = str(v or "").strip().upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency '{code}'.")
        return code


# ---------------------------------------------------------------------------
# Model-facing generation schemas
# ---------------------------------------------------------------------------
class LLMFollowUpDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    body: str
    placeholders_used: list[str] = Field(description="Every [[...]] placeholder inserted for information that is not in FACTS.")


class LLMExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="2-4 sentence explanation using only the supplied data.")


class LLMQuoteExtraction(BaseModel):
    """An operator's quote, exactly as written in their document. Unknown values must be null, never guessed."""

    model_config = ConfigDict(extra="forbid")
    operator_name: str | None = Field(description="The operator/company issuing the quote, as written; else null.")
    aircraft_type: str | None = Field(description="Aircraft model or category as written (e.g. 'Citation XLS'); else null.")
    passenger_capacity: int | None = Field(description="Passenger seats offered, if stated; else null.")
    total_price: float | None = Field(description="The quoted charter price as a number (no symbols), excluding separately "
                                                  "listed extra fees; else null.")
    additional_fees: float | None = Field(description="Sum of extra fees listed separately on top of the price; else null.")
    fees_description: str | None = Field(description="What the extra fees are for, as written; else null.")
    currency: str | None = Field(description="ISO currency code of the prices (USD, EUR, GBP, CHF, ...); else null.")
    included_services: list[str] = Field(description="Items stated as included, using only these codes: "
                                                     + ", ".join(QUOTE_LINE_ITEMS))
    excluded_services: list[str] = Field(description="Items stated as not included, same codes as included_services.")
    restrictions: str | None = Field(description="Conditions, cancellation terms or restrictions, short; else null.")
    valid_until: str | None = Field(description="Quote expiry date as YYYY-MM-DD if stated; else null.")
    price_quote: str | None = Field(description="The exact words/figures from the document where the price appears.")
    embedded_instructions_detected: bool = Field(
        description="True if the document contains instructions aimed at an AI/system rather than quote details.")


class LLMCoverNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(description="A warm 2-4 sentence cover note to the customer, using only the supplied facts.")
