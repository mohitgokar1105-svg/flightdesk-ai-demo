"""SQLAlchemy ORM models. SQLite is the single source of truth.

Conventions
-----------
* Primary keys are stable, prefixed string IDs (``INQ-…``, ``QTE-…``) generated in Python.
* All timestamps are stored as UTC (naive in SQLite) and returned timezone-aware via :class:`UTCDateTime`.
* Status columns carry CHECK constraints mirroring :mod:`src.domain`.
* ``is_sample`` marks seeded demonstration records so they can be distinguished from demo-session records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.domain import (
    BookingStatus,
    FollowUpStatus,
    FollowUpType,
    InquiryStatus,
    QuoteStatus,
    RecommendationStatus,
)


def utcnow() -> datetime:
    from src.clock import now

    return now()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _in(column: str, values: Any) -> str:
    joined = ", ".join(f"'{v.value if hasattr(v, 'value') else v}'" for v in values)
    return f"{column} IN ({joined})"


class UTCDateTime(TypeDecorator[datetime]):
    """Stores datetimes as naive UTC; always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not accepted; supply a timezone-aware UTC datetime.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class User(Base):
    """An application login. Only a scrypt hash of the password is stored."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('business_owner', 'user')", name="ck_user_role"),
        UniqueConstraint("username", name="ux_user_username"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("USR"))
    username: Mapped[str] = mapped_column(String(40))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column()
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column()


class Customer(Base):
    """A (fictional) customer. Contact details are optional and only stored when the customer supplied them."""

    __tablename__ = "customers"
    __table_args__ = (
        Index("ux_customer_email", "email", unique=True, sqlite_where=text("email IS NOT NULL")),
        Index("ix_customer_name", "name"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("CUS"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    company: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    inquiries: Mapped[list[Inquiry]] = relationship(back_populates="customer")


class Operator(Base):
    """An aircraft operator the business requests quotes from (directory entry; contact details are optional)."""

    __tablename__ = "operators"
    __table_args__ = (Index("ix_operator_name", "name"),)

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("OPR"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    name: Mapped[str] = mapped_column(String(80))
    contact_name: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))  # used for WhatsApp links
    base_airport: Mapped[str | None] = mapped_column(String(120))
    fleet: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    requests: Mapped[list[OperatorRequest]] = relationship(back_populates="operator", cascade="all, delete-orphan")


class OperatorRequest(Base):
    """A request for quote (RFQ) the team sent to an operator for one inquiry. FlightDesk never sends it itself —
    the team sends it (WhatsApp / email / copy) and records that it went out."""

    __tablename__ = "operator_requests"
    __table_args__ = (
        CheckConstraint("status IN ('sent', 'replied', 'declined', 'no_response')", name="ck_rfq_status"),
        CheckConstraint("channel IN ('whatsapp', 'email', 'phone', 'other')", name="ck_rfq_channel"),
        Index("ix_rfq_inquiry", "inquiry_id"),
        Index("ux_rfq_inquiry_operator", "inquiry_id", "operator_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("RFQ"))
    inquiry_id: Mapped[str] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp")
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="sent")
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    replied_at: Mapped[datetime | None] = mapped_column()
    quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id", ondelete="SET NULL"))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    operator: Mapped[Operator] = relationship(back_populates="requests")
    inquiry: Mapped[Inquiry] = relationship(back_populates="operator_requests")


class Inquiry(Base):
    __tablename__ = "inquiries"
    __table_args__ = (
        CheckConstraint(_in("status", InquiryStatus), name="ck_inquiry_status"),
        CheckConstraint("passenger_count IS NULL OR (passenger_count >= 1 AND passenger_count <= 500)", name="ck_inquiry_pax"),
        CheckConstraint("budget_amount IS NULL OR budget_amount > 0", name="ck_inquiry_budget"),
        CheckConstraint("estimated_opportunity_value IS NULL OR estimated_opportunity_value >= 0", name="ck_inquiry_opp"),
        Index("ix_inquiry_status", "status"),
        Index("ix_inquiry_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("INQ"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    customer_label: Mapped[str] = mapped_column(String(80))
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    origin: Mapped[str | None] = mapped_column(String(120))
    destination: Mapped[str | None] = mapped_column(String(120))
    trip_type: Mapped[str | None] = mapped_column(String(20))
    departure_date: Mapped[date | None] = mapped_column(Date)
    departure_earliest: Mapped[date | None] = mapped_column(Date)
    departure_latest: Mapped[date | None] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    return_earliest: Mapped[date | None] = mapped_column(Date)
    return_latest: Mapped[date | None] = mapped_column(Date)
    departure_time_preference: Mapped[str | None] = mapped_column(String(40))
    special_requirements: Mapped[str | None] = mapped_column(Text)
    passenger_count: Mapped[int | None] = mapped_column(Integer)
    trip_purpose: Mapped[str | None] = mapped_column(String(30))
    date_flexibility: Mapped[str | None] = mapped_column(String(20))
    budget_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    requested_services: Mapped[list[Any]] = mapped_column(default=list)
    missing_information: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(30), default=InquiryStatus.NEW.value)
    estimated_opportunity_value: Mapped[float | None] = mapped_column(Float)
    advisor_notes: Mapped[str | None] = mapped_column(Text)
    original_text: Mapped[str] = mapped_column(Text, default="")
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(20), default="intake")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    customer: Mapped[Customer | None] = relationship(back_populates="inquiries")
    quotes: Mapped[list[Quote]] = relationship(back_populates="inquiry", cascade="all, delete-orphan")
    bookings: Mapped[list[Booking]] = relationship(back_populates="inquiry", cascade="all, delete-orphan")
    followups: Mapped[list[FollowUp]] = relationship(back_populates="inquiry", cascade="all, delete-orphan")
    recommendations: Mapped[list[Recommendation]] = relationship(back_populates="inquiry", cascade="all, delete-orphan")
    operator_requests: Mapped[list[OperatorRequest]] = relationship(back_populates="inquiry", cascade="all, delete-orphan")


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint(_in("status", QuoteStatus), name="ck_quote_status"),
        CheckConstraint("total_price > 0", name="ck_quote_price"),
        CheckConstraint("additional_fees IS NULL OR additional_fees >= 0", name="ck_quote_fees"),
        CheckConstraint("passenger_capacity IS NULL OR (passenger_capacity >= 1 AND passenger_capacity <= 500)", name="ck_quote_cap"),
        Index("ix_quote_inquiry", "inquiry_id"),
        Index("ix_quote_status", "status"),
        Index("ix_quote_created", "created_at"),
        Index("ix_quote_presented", "presented_at"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("QTE"))
    inquiry_id: Mapped[str] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    operator_label: Mapped[str] = mapped_column(String(80))
    total_price: Mapped[float] = mapped_column(Float)
    additional_fees: Mapped[float | None] = mapped_column(Float)  # quoted extras on top of total_price, same currency
    fees_description: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3))
    aircraft_type: Mapped[str | None] = mapped_column(String(80))
    passenger_capacity: Mapped[int | None] = mapped_column(Integer)
    included_services: Mapped[list[Any]] = mapped_column(default=list)
    excluded_services: Mapped[list[Any]] = mapped_column(default=list)
    restrictions: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(20), default=QuoteStatus.RECEIVED.value)
    presented_at: Mapped[datetime | None] = mapped_column()  # when the advisor recorded sending it to the customer
    notes: Mapped[str | None] = mapped_column(Text)
    source_description: Mapped[str] = mapped_column(String(200), default="Fictional demonstration quote (manual entry)")
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    inquiry: Mapped[Inquiry] = relationship(back_populates="quotes")

    @property
    def all_in_price(self) -> float:
        """Total price plus documented additional fees (same currency)."""
        return round(self.total_price + (self.additional_fees or 0.0), 2)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(_in("status", BookingStatus), name="ck_booking_status"),
        CheckConstraint("booked_value > 0", name="ck_booking_value"),
        Index("ix_booking_status", "status"),
        Index("ix_booking_created", "created_at"),
        Index("ix_booking_confirmed", "confirmed_at"),
        # At most one non-cancelled booking per quote, and per (inquiry, service category).
        Index("ux_booking_active_quote", "quote_id", unique=True,
              sqlite_where=text("status != 'cancelled' AND quote_id IS NOT NULL")),
        Index("ux_booking_active_inquiry_cat", "inquiry_id", "service_category", unique=True,
              sqlite_where=text("status != 'cancelled'")),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("BKG"))
    inquiry_id: Mapped[str] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default=BookingStatus.CONFIRMED.value)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    cancelled_at: Mapped[datetime | None] = mapped_column()
    booked_value: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    service_category: Mapped[str] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    inquiry: Mapped[Inquiry] = relationship(back_populates="bookings")
    quote: Mapped[Quote | None] = relationship()


class FollowUp(Base):
    __tablename__ = "followups"
    __table_args__ = (
        CheckConstraint(_in("status", FollowUpStatus), name="ck_followup_status"),
        CheckConstraint(_in("followup_type", FollowUpType), name="ck_followup_type"),
        Index("ix_followup_inquiry", "inquiry_id"),
        Index("ix_followup_status_due", "status", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("FUP"))
    inquiry_id: Mapped[str] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    due_at: Mapped[datetime | None] = mapped_column()
    followup_type: Mapped[str] = mapped_column(String(30))
    tone: Mapped[str] = mapped_column(String(20), default="professional")
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=FollowUpStatus.DRAFT.value)
    completed_at: Mapped[datetime | None] = mapped_column()
    advisor_notes: Mapped[str | None] = mapped_column(Text)
    generation_method: Mapped[str] = mapped_column(String(20), default="template")
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    inquiry: Mapped[Inquiry] = relationship(back_populates="followups")


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(_in("status", RecommendationStatus), name="ck_rec_status"),
        UniqueConstraint("inquiry_id", "rule_key", name="ux_rec_inquiry_rule"),
        Index("ix_rec_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("REC"))
    inquiry_id: Mapped[str] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    rule_key: Mapped[str] = mapped_column(String(60))
    category: Mapped[str] = mapped_column(String(40))
    suggested_action: Mapped[str] = mapped_column(String(300))
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    explanation: Mapped[str] = mapped_column(Text)
    missing_information: Mapped[list[Any]] = mapped_column(default=list)
    next_step: Mapped[str] = mapped_column(String(300))
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    relevance_score: Mapped[float | None] = mapped_column(Float)  # intentionally null: no calibrated score
    status: Mapped[str] = mapped_column(String(20), default=RecommendationStatus.OPEN.value)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column()
    advisor_feedback: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="rule_engine")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    inquiry: Mapped[Inquiry] = relationship(back_populates="recommendations")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_wf_type_state", "workflow_type", "state"),
        Index("ix_wf_started", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("RUN"))
    workflow_type: Mapped[str] = mapped_column(String(40))
    inquiry_id: Mapped[str | None] = mapped_column(ForeignKey("inquiries.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(80))
    ai_mode: Mapped[str] = mapped_column(String(30), default="none")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    error_category: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(500))
    final_outcome: Mapped[str | None] = mapped_column(String(300))
    input_summary: Mapped[str | None] = mapped_column(String(300))
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    events: Mapped[list[WorkflowEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="WorkflowEvent.seq"
    )
    proposals: Mapped[list[ActionProposal]] = relationship(back_populates="run", cascade="all, delete-orphan")

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class WorkflowEvent(Base):
    """One entry in a workflow's timeline. ``source`` distinguishes model / application / tool / human."""

    __tablename__ = "workflow_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="ux_wf_event_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)

    run: Mapped[WorkflowRun] = relationship(back_populates="events")


class ActionProposal(Base):
    """A mutating operation requested by a workflow/model that awaits explicit advisor approval."""

    __tablename__ = "action_proposals"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'rejected', 'executed', 'failed')", name="ck_proposal_status"),
        Index("ix_proposal_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("ACT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rationale: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(String(500))

    run: Mapped[WorkflowRun] = relationship(back_populates="proposals")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('advisor', 'model', 'system')", name="ck_audit_actor"),
        CheckConstraint("outcome IN ('success', 'failure', 'rejected')", name="ck_audit_outcome"),
        Index("ix_audit_ts", "timestamp"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("AUD"))
    timestamp: Mapped[datetime] = mapped_column(default=utcnow)
    event_type: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(20))
    actor_type: Mapped[str] = mapped_column(String(20))
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    outcome: Mapped[str] = mapped_column(String(20), default="success")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (Index("ix_eval_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=lambda: new_id("EVL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    dataset_name: Mapped[str] = mapped_column(String(120))
    dataset_version: Mapped[str] = mapped_column(String(40))
    approach: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(80))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running")
    n_cases: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(500))

    cases: Mapped[list[EvaluationCase]] = relationship(back_populates="run", cascade="all, delete-orphan")


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="ux_eval_case"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"))
    case_id: Mapped[str] = mapped_column(String(40))
    tags: Mapped[list[Any]] = mapped_column(default=list)
    input_text: Mapped[str] = mapped_column(Text)
    expected: Mapped[dict[str, Any]] = mapped_column(default=dict)
    actual: Mapped[dict[str, Any]] = mapped_column(default=dict)
    field_results: Mapped[dict[str, Any]] = mapped_column(default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    exact_match: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_categories: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[EvaluationRun] = relationship(back_populates="cases")
