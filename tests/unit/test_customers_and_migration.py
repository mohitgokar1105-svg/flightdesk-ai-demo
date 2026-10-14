"""Customers, contact extraction, schema v2 migration from a v1 database, config precedence, new email types."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.ai.drafting import mailto_link, template_draft
from src.ai.extraction import run_rule_based
from src.ai.text_parsing import find_contact, find_special_requirements, find_time_preference
from src.domain import FollowUpType, Tone
from src.exceptions import DuplicateRecord, PermissionDenied
from src.models import Customer, Inquiry
from src.schemas import CustomerCreate, InquiryCreate
from src.services.customer_service import customer_summaries, find_or_create_customer, update_customer
from src.services.followup_service import build_followup_facts
from src.services.inquiry_service import create_inquiry

MSG = ("Hi, I need a private flight from Los Angeles to Las Vegas on November 15 for six passengers. We would prefer a "
       "morning departure. My father uses a wheelchair and we have extra luggage.\n\nThanks,\nJane Doe\n"
       "jane.doe@example.com · +1 555 0142")


def test_contact_time_and_requirements_extraction():
    c = find_contact(MSG)
    assert (c.name, c.email, c.phone) == ("Jane Doe", "jane.doe@example.com", "+1 555 0142")
    assert find_time_preference(MSG) == "morning"
    assert find_time_preference("leaving around 2pm please") == "around 14:00"
    assert find_time_preference("Good morning, flight on Dec 3") is None
    assert find_special_requirements(MSG) == ["wheelchair / mobility assistance", "extra or oversized baggage"]
    assert find_contact("Fly 12/18 for 6 ppl, budget 40,000").phone is None


def test_provenance_distinguishes_stated_inferred_missing():
    ex = run_rule_based(MSG, date(2026, 10, 11)).extraction
    assert ex.provenance["customer_name"] == "stated" and ex.provenance["contact"] == "stated"
    assert ex.provenance["departure_time_preference"] == "stated"
    assert ex.provenance["return_date"] == "missing" and ex.provenance["trip_type"] == "missing"
    window = run_rule_based("Need a jet to Aspen in December for 4", date(2026, 10, 11)).extraction
    assert window.provenance["departure"] == "inferred", "a month window is an interpretation, not a date"


def test_customer_find_or_create_and_dedupe(session):
    a = find_or_create_customer(session, CustomerCreate(name="Jane Doe", email="Jane.Doe@Example.com"))
    b = find_or_create_customer(session, CustomerCreate(name="J. Doe", email="jane.doe@example.com", phone="+1 555 0142"))
    assert a.id == b.id and b.phone == "+1 555 0142"
    other = find_or_create_customer(session, CustomerCreate(name="Sam Lee", email="sam@example.com"))
    with pytest.raises(DuplicateRecord):
        update_customer(session, other.id, CustomerCreate(name="Sam Lee", email="jane.doe@example.com"))
    with pytest.raises(PermissionDenied):
        find_or_create_customer(session, CustomerCreate(name="X"), actor="model")
    with pytest.raises(ValidationError):
        CustomerCreate(name="Bad", email="not-an-email")
    with pytest.raises(ValidationError):
        CustomerCreate(name="Bad", phone="12")


def test_inquiries_link_to_customers_and_summaries(session):
    inq = create_inquiry(session, InquiryCreate(customer_label="Jane Doe", customer_email="jane@example.com",
                                                original_text="x", destination="Aspen"))
    assert inq.customer is not None and inq.customer.email == "jane@example.com"
    create_inquiry(session, InquiryCreate(customer_label="Jane Doe", customer_email="jane@example.com", original_text="y"))
    rows = customer_summaries(session, "jane")
    assert len(rows) == 1 and rows[0]["inquiries"] == 2 and rows[0]["booked_value"] == {}


def test_new_email_types_use_records_and_never_claim_confirmation(session):
    inq = create_inquiry(session, InquiryCreate(customer_label="Jane Doe", original_text="x", origin="Los Angeles",
                                                destination="Las Vegas", passenger_count=6))
    facts = build_followup_facts(session, inq.id)
    first = template_draft(facts, FollowUpType.INITIAL_RESPONSE, Tone.PROFESSIONAL)
    assert first.body.startswith("Dear Jane Doe,") and "6 passenger" in first.body
    conf = template_draft(facts, FollowUpType.BOOKING_CONFIRMATION, Tone.PROFESSIONAL)
    assert "no confirmed booking is recorded" in conf.body and conf.warnings == []
    assert "is confirmed" not in conf.body.lower()
    update = template_draft(facts, FollowUpType.GENERAL_UPDATE, Tone.CONCISE)
    assert "[[Advisor to add the update]]" in update.body
    link = mailto_link("jane@example.com", "Hi there", "Line 1\nLine 2")
    assert link.startswith("mailto:jane%40example.com?subject=Hi%20there&body=Line%201%0ALine%202")


def test_migration_from_v1_database(tmp_path):
    """Build a v1-shaped database by hand, then let init_db migrate it to v2 without losing data."""
    from src.database import (
        SCHEMA_VERSION,
        dispose_engine,
        get_engine,
        get_schema_version,
        init_db,
        session_scope,
    )

    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE schema_meta (key VARCHAR(64) PRIMARY KEY, value VARCHAR(255));
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        CREATE TABLE inquiries (id VARCHAR(20) PRIMARY KEY, created_at DATETIME, updated_at DATETIME,
            customer_label VARCHAR(80), origin VARCHAR(120), destination VARCHAR(120), trip_type VARCHAR(20),
            departure_date DATE, departure_earliest DATE, departure_latest DATE, return_date DATE, return_earliest DATE,
            return_latest DATE, passenger_count INTEGER, trip_purpose VARCHAR(30), date_flexibility VARCHAR(20),
            budget_amount FLOAT, currency VARCHAR(3), requested_services JSON, missing_information JSON, status VARCHAR(30),
            estimated_opportunity_value FLOAT, advisor_notes TEXT, original_text TEXT, extraction_metadata JSON,
            content_hash VARCHAR(64) UNIQUE, source VARCHAR(20), is_sample BOOLEAN);
        INSERT INTO inquiries (id, created_at, updated_at, customer_label, status, requested_services, missing_information,
            extraction_metadata, content_hash, source, is_sample, original_text)
            VALUES ('INQ-OLD0000001', '2026-09-01 10:00:00', '2026-09-01 10:00:00', 'Old Customer', 'quoted', '[]', '[]', '{}',
                    'h1', 'intake', 0, 'old');
        CREATE TABLE quotes (id VARCHAR(20) PRIMARY KEY, inquiry_id VARCHAR(20) REFERENCES inquiries(id), operator_label VARCHAR(80),
            total_price FLOAT, currency VARCHAR(3), aircraft_type VARCHAR(80), passenger_capacity INTEGER,
            included_services JSON, excluded_services JSON, restrictions TEXT, valid_until DATE, created_at DATETIME,
            updated_at DATETIME, status VARCHAR(20), notes TEXT, source_description VARCHAR(200), dedupe_key VARCHAR(64) UNIQUE,
            is_sample BOOLEAN);
        INSERT INTO quotes VALUES ('QTE-OLD0000001', 'INQ-OLD0000001', 'Op', 1000, 'USD', NULL, NULL, '[]', '[]', NULL, NULL,
            '2026-09-02 10:00:00', '2026-09-03 10:00:00', 'presented', NULL, 'manual', 'k1', 0);
        CREATE TABLE followups (id VARCHAR(20) PRIMARY KEY, inquiry_id VARCHAR(20) REFERENCES inquiries(id), created_at DATETIME,
            updated_at DATETIME, due_at DATETIME, followup_type VARCHAR(30) CHECK (followup_type IN ('missing_information')),
            tone VARCHAR(20), subject VARCHAR(200), body TEXT, status VARCHAR(20), completed_at DATETIME, advisor_notes TEXT,
            generation_method VARCHAR(20), generation_metadata JSON, is_sample BOOLEAN);
        INSERT INTO followups VALUES ('FUP-OLD0000001', 'INQ-OLD0000001', '2026-09-04 10:00:00', '2026-09-04 10:00:00', NULL,
            'missing_information', 'professional', 'S', 'B', 'draft', NULL, NULL, 'template', '{}', 0);
    """)
    con.commit()
    con.close()
    url = f"sqlite:///{path.as_posix()}"
    try:
        assert init_db(url) == SCHEMA_VERSION and get_schema_version(get_engine(url)) == SCHEMA_VERSION
        with session_scope(url) as s:
            inq = s.get(Inquiry, "INQ-OLD0000001")
            assert inq.customer is not None and inq.customer.name == "Old Customer", "customers backfilled"
            assert inq.quotes[0].presented_at is not None, "sent date backfilled for presented quotes"
            assert inq.followups[0].subject == "S", "follow-ups preserved through the table rebuild"
            from src.models import FollowUp

            s.add(FollowUp(inquiry_id=inq.id, followup_type="initial_response", subject="new", body="b"))
            s.flush()
            assert s.scalar(select(Customer).where(Customer.name == "Old Customer")) is not None
        assert init_db(url) == SCHEMA_VERSION, "idempotent"
    finally:
        dispose_engine(url)


def test_dotenv_takes_priority_but_is_disabled_in_tests(tmp_path, monkeypatch):
    import src.config as cfg

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "ENV_FILE", env_file)
    monkeypatch.setenv("OPENAI_MODEL", "from-shell")
    assert cfg.get_settings().openai_model == "from-shell", "FLIGHTDESK_DISABLE_DOTENV=1 in tests"
    monkeypatch.setenv("FLIGHTDESK_DISABLE_DOTENV", "0")
    assert cfg.get_settings().openai_model == "from-dotenv", ".env wins over a stale shell variable"


def test_inquiry_search_matches_every_word_and_filters_dates_before_the_limit(session):
    from datetime import UTC, date, datetime

    from src.schemas import InquiryCreate
    from src.services.inquiry_service import create_inquiry, search_inquiries

    for i in range(60):  # more than the old hard cap of 50
        inq = create_inquiry(session, InquiryCreate(customer_label=f"Bulk {i}", original_text=f"bulk {i}",
                                                    origin="Miami", destination="Nassau"))
        inq.created_at = datetime(2026, 3, 1 + i % 28, tzinfo=UTC) if i < 5 else datetime(2026, 9, 1, tzinfo=UTC)
    target = create_inquiry(session, InquiryCreate(customer_label="Avery Stone", original_text="aspen",
                                                   origin="Los Angeles", destination="Aspen"))
    session.commit()
    assert [r.id for r in search_inquiries(session, "Avery Aspen")] == [target.id], "each word may match a different field"
    assert search_inquiries(session, "Avery Nassau") == []
    march = search_inquiries(session, "", None, 1000, received_from=date(2026, 3, 1), received_to=date(2026, 3, 31))
    assert len(march) == 5, "old inquiries are found even when newer ones exceed the old 50-row cap"
