"""The three experiences through the real Streamlit UI (AppTest, no network): role-based routing, the customer request
form, the team inbox picking the request up, the inquiry workspace actions, and failure handling."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[2] / "app.py")
USERS: dict[str, str] = {}


@pytest.fixture(scope="module")
def portal_db(tmp_path_factory) -> str:
    from src.auth import SYSTEM, Role, as_principal, create_user
    from src.database import init_db, session_scope

    path = tmp_path_factory.mktemp("portal") / "portal.db"
    url = f"sqlite:///{Path(path).as_posix()}"
    os.environ["DATABASE_URL"] = url
    from src.ui.common import _bootstrap

    _bootstrap.clear()
    init_db(url)
    with session_scope(url) as s, as_principal(SYSTEM):
        USERS["owner"] = create_user(s, "owner_portal", "owner-pass-1", Role.BUSINESS_OWNER, "Owner").id
        USERS["user"] = create_user(s, "member_portal", "member-pass-1", Role.USER, "Member").id
    yield url
    _bootstrap.clear()
    os.environ.pop("DATABASE_URL", None)


def _signed_in(script: AppTest, role: str) -> AppTest:
    now = datetime.now(UTC)
    script.session_state["auth"] = {"user_id": USERS[role], "login_at": now, "last_seen": now}
    return script


def _page(name: str, role: str | None = None) -> AppTest:
    script = ("from src.auth import set_principal\nfrom src.ui.common import resolve_session\n"
              f"set_principal(resolve_session())\nfrom src.ui import {name}\n{name}.render()\n")
    at = AppTest.from_string(script, default_timeout=90)
    if role:
        _signed_in(at, role)
    return at.run()


def _click(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click().run()
    assert not at.exception, at.exception
    return at


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info] + \
            [s.value for s in at.success] + [e.value for e in at.error] + [w.value for w in at.warning]
    return " ".join(str(p) for p in parts)


def _fill_request(at: AppTest, name: str, email: str, special: str = "") -> AppTest:
    _click(at, "Request a flight")
    at.selectbox(key="pf_origin").set_value("Farnborough Airport (FAB)")
    at.selectbox(key="pf_destination").set_value("Geneva International Airport (GVA)")
    at.date_input(key="pf_departure_date").set_value(date.today() + timedelta(days=30))
    at.number_input(key="pf_passengers").set_value(3)
    _click(at, "Continue")
    at.text_input(key="pf_full_name").set_value(name)
    at.text_input(key="pf_email").set_value(email)
    _click(at, "Continue")
    if special:
        at.text_area(key="pf_special_requests").set_value(special)
    return at


def test_routing_by_role(portal_db):
    anon = AppTest.from_file(APP, default_timeout=90).run()
    assert not anon.exception
    assert "Team sign in" in _text(anon)
    assert set(anon.session_state["_pages"]) == {"signin", "request", "status"}, "no internal page exists for visitors"

    member = _signed_in(AppTest.from_file(APP, default_timeout=90), "user").run()
    assert not member.exception and [t.value for t in member.title] == ["Inbox"]
    assert not {"home", "reports", "evaluation", "activity"} & set(member.session_state["_pages"])

    owner = _signed_in(AppTest.from_file(APP, default_timeout=90), "owner").run()
    assert not owner.exception and any("👋" in t.value for t in owner.title), "the owner lands on the Overview"
    assert {"home", "inbox", "reports", "evaluation", "settings"} <= set(owner.session_state["_pages"])


def test_customer_request_reaches_team_inbox_and_workspace(portal_db):
    at = _fill_request(_page("portal"), "Nora Quinn", "nora.quinn@example.com")
    _click(at, "Send request")
    ref = at.session_state["portal"]["reference"]
    assert "Request received" in _text(at) and ref in _text(at)
    assert "Nothing is booked or charged" in _text(at)

    inbox = _page("inbox", role="user")
    assert "Nora Quinn" in _text(inbox) and "From website" in _text(inbox)
    _click(inbox, "Open the next new inquiry →")
    text = _text(inbox)
    assert "Next step" in text and ref in text
    for label in ("✨ Extract flight details", "🔎 Find missing information", "⚖️ Compare quotes", "✉️ Draft customer email",
                  "📝 Summarize inquiry"):
        assert any(b.label == label for b in inbox.button), label
    _click(inbox, "🔎 Find missing information")
    assert "Nothing required is missing" in _text(inbox)
    _click(inbox, "📝 Summarize inquiry")
    assert "Summary · from the record" in _text(inbox) and "Farnborough Airport (FAB) → Geneva International Airport (GVA)" in _text(inbox)
    _click(inbox, "⚖️ Compare quotes")
    assert "No quotes recorded yet" in _text(inbox)


def test_status_check_is_scoped_to_reference_and_email(portal_db):
    at = _fill_request(_page("portal"), "Omar Reed", "omar.reed@example.com")
    _click(at, "Send request")
    _click(at, "Check status")
    _click(at, "Check status")
    assert "Received" in _text(at)
    at.text_input(key="portal_lookup_email").set_value("nora.quinn@example.com")
    _click(at, "Check status")
    assert "couldn't find a request" in _text(at)


def test_submission_failure_keeps_details_and_explains(portal_db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from src.ui import portal

    def broken(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(portal, "submit_flight_request", broken)
    at = _fill_request(_page("portal"), "Pia Lund", "pia.lund@example.com")
    _click(at, "Send request")
    assert "couldn't send your request" in _text(at)
    assert at.session_state["portal"]["step"] == 3 and at.session_state["portal"]["values"]["email"] == "pia.lund@example.com"


def test_portal_validation_messages(portal_db):
    at = _page("portal")
    _click(at, "Request a flight")
    _click(at, "Continue")
    text = _text(at)
    assert "Where are you flying from?" in text and "How many people are travelling?" in text
    assert at.session_state["portal"]["step"] == 1


def test_status_check_is_rate_limited_server_side(portal_db):
    from src.ratelimit import STATUS_LOOKUPS

    for _ in range(STATUS_LOOKUPS.limit):
        assert STATUS_LOOKUPS.allow("direct")
    at = AppTest.from_string("from src.ui import portal\nportal.render_status()\n", default_timeout=90).run()
    at.text_input[0].set_value("INQ-ABC123")
    at.text_input[1].set_value("x@example.com")
    next(b for b in at.button if b.label == "Check status").click().run()
    assert any("Too many checks" in e.value for e in at.error)
