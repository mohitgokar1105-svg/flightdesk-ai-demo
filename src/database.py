"""Database engine, transactional sessions, schema versioning and guarded reset."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from src.auth import Permission, require
from src.exceptions import ConfirmationRequired
from src.models import (
    ActionProposal,
    AuditEvent,
    Base,
    Booking,
    Customer,
    EvaluationCase,
    EvaluationRun,
    FollowUp,
    Inquiry,
    Operator,
    OperatorRequest,
    Quote,
    Recommendation,
    SchemaMeta,
    User,
    WorkflowEvent,
    WorkflowRun,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4
RESET_CONFIRMATION_TOKEN = "RESET"


def _columns(conn: Any, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _migrate_v2(engine: Engine) -> None:
    """v1 → v2: customers (+ inquiries.customer_id), inquiry time preference / special requirements,
    quote sent date + additional fees, new follow-up email types (table rebuild for the CHECK constraint).
    Idempotent and transactional; existing data is preserved."""
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        inq_cols = _columns(conn, "inquiries")
        for name, ddl in (("customer_id", "VARCHAR(20) REFERENCES customers(id) ON DELETE SET NULL"),
                          ("departure_time_preference", "VARCHAR(40)"), ("special_requirements", "TEXT")):
            if name not in inq_cols:
                conn.exec_driver_sql(f"ALTER TABLE inquiries ADD COLUMN {name} {ddl}")
        q_cols = _columns(conn, "quotes")
        for name, ddl in (("presented_at", "DATETIME"), ("additional_fees", "FLOAT"), ("fees_description", "VARCHAR(200)")):
            if name not in q_cols:
                conn.exec_driver_sql(f"ALTER TABLE quotes ADD COLUMN {name} {ddl}")
        # Best available sent date for quotes already marked as sent (documented approximation).
        conn.exec_driver_sql("UPDATE quotes SET presented_at = updated_at "
                             "WHERE presented_at IS NULL AND status IN ('presented', 'accepted')")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_quote_presented ON quotes (presented_at)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_booking_confirmed ON bookings (confirmed_at)")
        # Rebuild followups so its CHECK constraint accepts the new email types.
        old_cols = sorted(_columns(conn, "followups"))
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_followup_inquiry")
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_followup_status_due")
        conn.exec_driver_sql("ALTER TABLE followups RENAME TO followups_v1")
        FollowUp.__table__.create(conn)  # type: ignore[attr-defined]
        cols = ", ".join(old_cols)
        conn.exec_driver_sql(f"INSERT INTO followups ({cols}) SELECT {cols} FROM followups_v1")
        conn.exec_driver_sql("DROP TABLE followups_v1")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


# version -> migration callable(engine). Version 1 is the initial schema created by create_all.
def _migrate_v3(engine: Engine) -> None:
    """v2 → v3: adds the ``users`` table (created by ``create_all`` before migrations run). Nothing to alter."""


def _migrate_v4(engine: Engine) -> None:
    """v3 → v4: adds ``operators`` and ``operator_requests`` (created by ``create_all``). Nothing to alter."""


MIGRATIONS: dict[int, Callable[[Engine], None]] = {2: _migrate_v2, 3: _migrate_v3, 4: _migrate_v4}

_ENGINES: dict[str, Engine] = {}
_SESSIONMAKERS: dict[str, sessionmaker[Session]] = {}

# Child tables first so deletes respect foreign keys.
TABLES_IN_DELETE_ORDER = [
    EvaluationCase, EvaluationRun, ActionProposal, WorkflowEvent, WorkflowRun, AuditEvent,
    Recommendation, FollowUp, OperatorRequest, Booking, Quote, Inquiry, Customer, Operator,
]
COUNTED_TABLES = {
    "users": User, "customers": Customer, "inquiries": Inquiry, "quotes": Quote, "bookings": Booking, "followups": FollowUp,
    "recommendations": Recommendation, "workflow_runs": WorkflowRun, "workflow_events": WorkflowEvent,
    "action_proposals": ActionProposal, "audit_events": AuditEvent, "evaluation_runs": EvaluationRun,
    "evaluation_cases": EvaluationCase, "operators": Operator, "operator_requests": OperatorRequest,
}


def _enable_sqlite_fk(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(database_url: str) -> Engine:
    """Return a cached engine for ``database_url`` with SQLite foreign keys enforced."""
    if database_url not in _ENGINES:
        if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
            Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        engine = create_engine(database_url, connect_args=connect_args, future=True)
        if database_url.startswith("sqlite"):
            event.listen(engine, "connect", _enable_sqlite_fk)
        _ENGINES[database_url] = engine
        _SESSIONMAKERS[database_url] = sessionmaker(bind=engine, expire_on_commit=False)
    return _ENGINES[database_url]


def dispose_engine(database_url: str) -> None:
    engine = _ENGINES.pop(database_url, None)
    _SESSIONMAKERS.pop(database_url, None)
    if engine is not None:
        engine.dispose()


@contextmanager
def session_scope(database_url: str) -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on *any* exception, always closes."""
    get_engine(database_url)
    session = _SESSIONMAKERS[database_url]()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_schema_version(engine: Engine) -> int | None:
    if not inspect(engine).has_table("schema_meta"):
        return None
    with Session(engine) as s:
        row = s.get(SchemaMeta, "schema_version")
        return int(row.value) if row else None


def init_db(database_url: str) -> int:
    """Create tables if missing and apply pending migrations. Never deletes data. Returns the schema version."""
    engine = get_engine(database_url)
    current = get_schema_version(engine)
    Base.metadata.create_all(engine)
    if current is None:
        with Session(engine) as s, s.begin():
            s.merge(SchemaMeta(key="schema_version", value=str(SCHEMA_VERSION)))
        logger.info("Initialised database schema at version %s", SCHEMA_VERSION)
        return SCHEMA_VERSION
    for version in range(current + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RuntimeError(f"No migration registered for schema version {version}")
        logger.info("Applying migration to schema version %s", version)
        migration(engine)
        with Session(engine) as s, s.begin():
            s.merge(SchemaMeta(key="schema_version", value=str(version)))
    backfill_customers(database_url)
    return max(current, SCHEMA_VERSION)


def backfill_customers(database_url: str) -> int:
    """Link inquiries created before customers existed (schema v1) to a customer record. Idempotent."""
    from src.auth import SYSTEM, as_principal
    from src.schemas import CustomerCreate
    from src.services.customer_service import find_or_create_customer, link_inquiry

    linked = 0
    with session_scope(database_url) as s, as_principal(SYSTEM):
        for inq in s.scalars(select(Inquiry).where(Inquiry.customer_id.is_(None))):
            customer = find_or_create_customer(s, CustomerCreate(name=inq.customer_label), actor="system",
                                               is_sample=inq.is_sample)
            link_inquiry(s, inq, customer)
            linked += 1
    return linked


def record_counts(session: Session) -> dict[str, int]:
    return {name: int(session.scalar(select(func.count()).select_from(model)) or 0) for name, model in COUNTED_TABLES.items()}


def delete_all_records(session: Session, confirmation: str) -> dict[str, int]:
    """Delete every application record. Requires ``confirmation == RESET_CONFIRMATION_TOKEN``.

    Runs inside the caller's transaction so a failure leaves the database untouched.
    """
    require(Permission.ADMIN_DATA)
    if confirmation != RESET_CONFIRMATION_TOKEN:
        raise ConfirmationRequired(f"Type {RESET_CONFIRMATION_TOKEN!r} to confirm deleting all records.")
    deleted: dict[str, int] = {}
    for model in TABLES_IN_DELETE_ORDER:
        result = session.query(model).delete(synchronize_session=False)
        deleted[model.__tablename__] = int(result or 0)
    return deleted
