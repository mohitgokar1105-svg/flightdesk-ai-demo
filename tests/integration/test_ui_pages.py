"""Streamlit AppTest smoke tests: every page renders without exceptions on an empty and a seeded database,
and the main owner workflows can be driven through the real UI (no network: demo mode, no API key)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["home", "inbox", "portal", "inquiries", "quotes", "customers", "followups", "assistant_chat", "reports", "settings",
         "workflow_inspector", "evaluation_lab"]
USERS: dict[str, str] = {}


@pytest.fixture(scope="module")
def ui_db(tmp_path_factory) -> str:
    from src.auth import SYSTEM, Role, as_principal, create_user
    from src.database import init_db, session_scope

    path = tmp_path_factory.mktemp("ui") / "ui.db"
    url = f"sqlite:///{Path(path).as_posix()}"
    os.environ["DATABASE_URL"] = url
    from src.ui.common import _bootstrap

    _bootstrap.clear()
    init_db(url)
    with session_scope(url) as s, as_principal(SYSTEM):
        USERS["owner"] = create_user(s, "owner_ui", "owner-pass-1", Role.BUSINESS_OWNER, "Owner").id
        USERS["user"] = create_user(s, "member_ui", "member-pass-1", Role.USER, "Member").id
    yield url
    _bootstrap.clear()
    os.environ.pop("DATABASE_URL", None)


def _page(name: str, state: dict | None = None, role: str = "owner") -> AppTest:
    script = (f"from src.auth import set_principal\nfrom src.ui.common import resolve_session, sidebar_brand\n"
              f"set_principal(resolve_session())\nfrom src.ui import {name}\nsidebar_brand()\n{name}.render()\n")
    at = AppTest.from_string(script, default_timeout=90)
    now = datetime.now(UTC)
    at.session_state["auth"] = {"user_id": USERS[role], "login_at": now, "last_seen": now}
    for k, v in (state or {}).items():
        at.session_state[k] = v
    at.run()
    return at


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [i.value for i in at.info]
    return " ".join(str(p) for p in parts)


@pytest.mark.parametrize("name", PAGES)
def test_pages_render_on_empty_database(ui_db, name):
    at = _page(name)
    assert not at.exception, f"{name}: {at.exception}"


def test_empty_overview_shows_first_run_welcome(ui_db):
    at = _page("home")
    assert "Welcome to FlightDesk" in _text(at)
    assert any(b.label == "Add your first inquiry" for b in at.button)


def test_assistant_without_ai_answers_from_records(ui_db):
    at = _page("assistant_chat")
    assert "isn't set up" in _text(at) or "switched off" in _text(at)
    next(b for b in at.button if b.label == "What should I prioritize today?").click().run()
    assert not at.exception
    assert "Answered from your records (AI not used)." in _text(at)


def test_seed_then_all_pages_render(ui_db):
    from src.config import get_settings
    from src.database import session_scope
    from src.seed import seed_demo_data

    settings = get_settings()
    assert settings.database_url == ui_db
    with session_scope(ui_db) as s:
        seed_demo_data(s, settings)
    for name in PAGES:
        at = _page(name)
        assert not at.exception, f"{name}: {at.exception}"


def test_overview_kpis_and_summary(ui_db):
    at = _page("home")
    text = _text(at)
    for label in ("New inquiries", "Quotes in progress", "Confirmed bookings", "Follow-ups needing attention",
                  "Confirmed booking value", "Ask AI about your business"):
        assert label in text
    assert "Summary · from your records" in text, "without AI the summary is deterministic and labelled"
    assert "Needs attention" in text


def test_add_inquiry_flow_basic_reader(ui_db):
    at = _page("inquiries", {"inquiries_tab": "add"})
    at.text_area(key="intake_message").set_value(
        "Hi, I need a private flight from Los Angeles to Las Vegas on November 15 for six passengers. "
        "We would prefer a morning departure.\n\nThanks,\nJane Doe\njane.doe@example.com").run()
    next(b for b in at.button if b.label == "Read message").click().run()
    assert not at.exception
    text = _text(at)
    assert "Read by the basic reader (no AI)" in text
    assert "Customer name: ✓ from the message" in text
    assert "Departure time: ✓ from the message" in text
    form_inputs = {t.label: t.value for t in at.text_input}
    assert form_inputs["Name"] == "Jane Doe" and form_inputs["Email"] == "jane.doe@example.com"
    assert form_inputs["Preferred time"] == "morning"


def test_followups_template_email(ui_db):
    at = _page("followups")
    next(b for b in at.button if b.label == "Write email").click().run()
    assert not at.exception
    assert "Written from a template" in _text(at)
    assert at.text_area(key="email_body").value.strip()


def test_login_screen_rejects_bad_password_and_accepts_good(ui_db):
    at = AppTest.from_string("from src.ui import login\nlogin.render()\n", default_timeout=60)
    at.run()
    assert not at.exception and any(b.label == "Sign in" for b in at.button)
    at.text_input[0].set_value("owner_ui")
    at.text_input[1].set_value("wrong-password")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert any("don't match" in e.value for e in at.error)
    assert "auth" not in at.session_state
    at.text_input[0].set_value("owner_ui")
    at.text_input[1].set_value("owner-pass-1")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert at.session_state["auth"]["user_id"] == USERS["owner"]


def test_pages_require_sign_in(ui_db):
    at = AppTest.from_string("from src.ui import inquiries\ninquiries.render()\n", default_timeout=60)
    at.run()
    assert any("sign in" in i.value.lower() for i in at.info)
    assert not any(t.value == "Inquiries" for t in at.title)


@pytest.mark.parametrize("name", ["reports", "evaluation_lab", "workflow_inspector"])
def test_team_member_cannot_open_owner_pages(ui_db, name):
    at = _page(name, role="user")
    assert not at.exception
    assert any(t.value == "Not available" for t in at.title)


def test_team_member_overview_hides_money(ui_db):
    at = _page("home", role="user")
    text = _text(at)
    assert "Confirmed booking value" not in text and "Overdue follow-ups" in text


def test_idle_session_expires(ui_db):
    script = ("from src.auth import set_principal\nfrom src.ui.common import resolve_session\n"
              "import streamlit as st\np = resolve_session()\nst.write('signed-in' if p else 'signed-out')\n")
    at = AppTest.from_string(script, default_timeout=60)
    from datetime import timedelta

    old = datetime.now(UTC) - timedelta(hours=2)
    at.session_state["auth"] = {"user_id": USERS["owner"], "login_at": old, "last_seen": old}
    at.run()
    assert any(m.value == "signed-out" for m in at.markdown)
