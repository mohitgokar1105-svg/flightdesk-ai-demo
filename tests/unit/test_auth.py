"""Authentication, password handling and backend role enforcement."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from src.auth import (
    GENERIC_FAILURE,
    MAX_FAILED_ATTEMPTS,
    ROLE_PERMISSIONS,
    SYSTEM,
    Permission,
    Role,
    as_principal,
    authenticate,
    bootstrap_demo_users,
    change_password,
    create_user,
    hash_password,
    principal_for,
    require,
    verify_password,
)
from src.exceptions import PermissionDenied, ValidationFailure
from src.models import AuditEvent, User
from src.schemas import BookingCreate, InquiryCreate, QuoteCreate


def _users(session):
    owner = create_user(session, "owner_t", "correct-horse", Role.BUSINESS_OWNER, "Owner")
    member = create_user(session, "member_t", "correct-horse", Role.USER, "Member")
    return principal_for(owner), principal_for(member)


def test_passwords_are_hashed_with_salt():
    h1, h2 = hash_password("test-pass-word-1"), hash_password("test-pass-word-1")
    assert h1 != h2 and "test-pass-word-1" not in h1 and h1.startswith("scrypt$")
    assert verify_password("test-pass-word-1", h1) and not verify_password("test-pass-word-2", h1)
    assert not verify_password("x", "not-a-hash")


def test_stored_user_has_no_plaintext(session):
    create_user(session, "alice_x", "s3cret-pass", Role.USER)
    row = session.scalar(select(User).where(User.username == "alice_x"))
    assert row is not None and "s3cret-pass" not in row.password_hash


def test_authenticate_success_and_generic_failures(session):
    _users(session)
    ok = authenticate(session, "OWNER_T", "correct-horse")
    assert ok.ok and ok.principal.role == Role.BUSINESS_OWNER
    bad_pw = authenticate(session, "owner_t", "wrong-password")
    unknown = authenticate(session, "nobody", "whatever-123")
    assert not bad_pw.ok and not unknown.ok and bad_pw.message == unknown.message == GENERIC_FAILURE
    audit = [e.event_type for e in session.scalars(select(AuditEvent))]
    assert "login_succeeded" in audit and audit.count("login_failed") == 2
    assert not any("correct-horse" in str(e.event_metadata) for e in session.scalars(select(AuditEvent)))


def test_lockout_after_repeated_failures(session):
    _users(session)
    for _ in range(MAX_FAILED_ATTEMPTS):
        authenticate(session, "member_t", "nope-nope")
    locked = authenticate(session, "member_t", "correct-horse")
    assert not locked.ok and "Too many attempts" in locked.message


def test_inactive_user_cannot_sign_in(session):
    _, member = _users(session)
    session.get(User, member.user_id).is_active = False
    assert not authenticate(session, "member_t", "correct-horse").ok


def test_validation_rules(session):
    with pytest.raises(ValidationFailure):
        create_user(session, "ab", "test-pass-word-1", Role.USER)
    with pytest.raises(ValidationFailure):
        create_user(session, "valid_name", "short", Role.USER)
    create_user(session, "dupe_name", "test-pass-word-1", Role.USER)
    with pytest.raises(ValidationFailure):
        create_user(session, "DUPE_NAME", "test-pass-word-1", Role.USER)


def test_bootstrap_from_environment_never_overwrites(session, monkeypatch):
    monkeypatch.setenv("FLIGHTDESK_OWNER_USERNAME", "boss_demo")
    monkeypatch.setenv("FLIGHTDESK_OWNER_PASSWORD", "first-password")
    monkeypatch.setenv("FLIGHTDESK_USER_USERNAME", "staff_demo")
    monkeypatch.setenv("FLIGHTDESK_USER_PASSWORD", "staff-password")
    assert sorted(bootstrap_demo_users(session)) == ["boss_demo", "staff_demo"]
    monkeypatch.setenv("FLIGHTDESK_OWNER_PASSWORD", "changed-password")
    assert bootstrap_demo_users(session) == []
    assert authenticate(session, "boss_demo", "first-password").ok
    roles = {u.username: u.role for u in session.scalars(select(User))}
    assert roles == {"boss_demo": "business_owner", "staff_demo": "user"}


def test_change_password_only_for_self(session):
    owner, member = _users(session)
    with as_principal(member):
        change_password(session, member.user_id, "correct-horse", "new-password-1")
        with pytest.raises(PermissionDenied):
            change_password(session, owner.user_id, "correct-horse", "hijack-password")
        with pytest.raises(ValidationFailure):
            change_password(session, member.user_id, "wrong", "another-pass")
    assert authenticate(session, "member_t", "new-password-1").ok


def test_permission_matrix():
    owner, user = ROLE_PERMISSIONS[Role.BUSINESS_OWNER], ROLE_PERMISSIONS[Role.USER]
    assert owner == frozenset(Permission)
    assert user == {Permission.VIEW_OPERATIONS, Permission.EDIT_OPERATIONS, Permission.USE_ASSISTANT}


def test_not_signed_in_is_denied(session):
    from src.auth import _current

    token = _current.set(None)
    try:
        with pytest.raises(PermissionDenied, match="sign in"):
            require(Permission.VIEW_OPERATIONS)
        from src.services.inquiry_service import create_inquiry

        with pytest.raises(PermissionDenied):
            create_inquiry(session, InquiryCreate(customer_label="X", original_text="x"))
    finally:
        _current.reset(token)


def test_team_member_backend_restrictions(settings, session):
    from src.database import delete_all_records
    from src.services.analytics_service import DashboardFilters, compute_metrics
    from src.services.booking_service import cancel_booking, create_booking_record
    from src.services.csv_service import export_entity
    from src.services.inquiry_service import create_inquiry
    from src.services.quote_service import create_quote
    from src.workflows import workflow_engine as wf

    owner, member = _users(session)
    with as_principal(member):
        inq = create_inquiry(session, InquiryCreate(customer_label="Ops", original_text="t", origin="Miami", destination="Aspen",
                                                    trip_type="one_way", departure_date=date(2026, 12, 1), passenger_count=2))
        q = create_quote(session, QuoteCreate(inquiry_id=inq.id, operator_label="Op", total_price=1000, currency="USD"))
        for action in (lambda: compute_metrics(session, DashboardFilters()),
                       lambda: export_entity(session, "inquiries"),
                       lambda: delete_all_records(session, "RESET"),
                       lambda: create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=10, currency="USD")),
                       lambda: wf.start_booking_confirmation(session, settings, q.id)):
            with pytest.raises(PermissionDenied):
                action()
    with as_principal(owner):
        b = create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=10, currency="USD"))
    with as_principal(member), pytest.raises(PermissionDenied):
        cancel_booking(session, b.id, reason="not allowed")


def test_financial_values_hidden_from_team_members(session):
    from src.services.booking_service import create_booking_record
    from src.services.customer_service import customer_summaries
    from src.services.dashboard_service import business_kpis, resolve_period
    from src.services.inquiry_service import create_inquiry

    owner, member = _users(session)
    with as_principal(SYSTEM):
        inq = create_inquiry(session, InquiryCreate(customer_label="Rich", original_text="r", origin="Miami", destination="Aspen"))
        create_booking_record(session, BookingCreate(inquiry_id=inq.id, booked_value=50000, currency="USD"))
    p = resolve_period("all_time")
    with as_principal(owner):
        assert business_kpis(session, p).booked_value == {"USD": 50000.0}
        assert customer_summaries(session, "Rich")[0]["booked_value"] == {"USD": 50000.0}
    with as_principal(member):
        k = business_kpis(session, p)
        assert k.booked_value == {} and not k.financials_visible and k.confirmed_bookings == 1
        assert k.as_dict()["booked_value_by_currency"] == "restricted for this role"
        assert customer_summaries(session, "Rich")[0]["booked_value"] == {}


def test_assistant_tools_respect_roles(session):
    from src.workflows.recorder import WorkflowRecorder
    from src.workflows.state_machine import WorkflowType
    from src.workflows.tool_registry import ToolContext
    from src.workflows.tools import ASSISTANT_ALLOWLIST, REGISTRY

    owner, member = _users(session)
    with as_principal(owner):
        assert "get_booking_summary" in {t["name"] for t in REGISTRY.openai_tools(ASSISTANT_ALLOWLIST)}
    with as_principal(member):
        names = {t["name"] for t in REGISTRY.openai_tools(ASSISTANT_ALLOWLIST)}
        assert "get_booking_summary" not in names and "get_inquiries" in names
        rec = WorkflowRecorder.start(session, WorkflowType.ASSISTANT_QUERY)
        res = REGISTRY.dispatch(ToolContext(session=session, actor="model", recorder=rec), "get_booking_summary",
                                {"period": "this_month"}, ASSISTANT_ALLOWLIST)
        assert res.status == "rejected" and "role" in res.error
