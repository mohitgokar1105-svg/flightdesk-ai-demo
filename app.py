"""FlightDesk AI — Streamlit entry point.  Run:  streamlit run app.py

Three experiences share one database:
* Customers (no account):  /request  — "Request a flight", and /my-request — check a request's status.
* Team members (sign in):  Inbox-first workspace for inquiries, quotes, customers and follow-ups.
* Business owner (sign in): Overview plus everything the team has, with Reports, AI Evaluation Lab and settings under "More".
"""

from __future__ import annotations

import streamlit as st

from src.auth import Permission, Role, set_principal
from src.config import APP_NAME
from src.ui import (
    assistant_chat,
    customers,
    evaluation_lab,
    followups,
    home,
    inbox,
    inquiries,
    login,
    portal,
    quotes,
    reports,
    settings,
    workflow_inspector,
)
from src.ui.common import resolve_session, sidebar_brand

st.set_page_config(page_title=APP_NAME, page_icon="✈", layout="wide", initial_sidebar_state="expanded")

principal = resolve_session()
set_principal(principal)  # every service call in this run is authorised against this user

if principal is None:
    # Public: staff sign-in (default) and the customer portal. No internal page is registered for anonymous visitors.
    public = {
        "signin": st.Page(login.render, title="Sign in", icon=":material/login:", default=True),
        "request": st.Page(portal.render, title="Request a flight", icon=":material/flight_takeoff:", url_path="request"),
        "status": st.Page(portal.render_status, title="Check my request", icon=":material/search:", url_path="my-request"),
    }
    st.session_state["_pages"] = public
    st.navigation(list(public.values()), position="hidden").run()
    st.stop()

# key -> (render function, title, icon, url path, permission needed to see it)
PAGES = {
    "home": (home.render, "Overview", ":material/space_dashboard:", "overview", Permission.VIEW_FINANCIALS),
    "inbox": (inbox.render, "Inbox", ":material/inbox:", "inbox", Permission.VIEW_OPERATIONS),
    "inquiries": (inquiries.render, "Inquiries", ":material/flight:", "inquiries", Permission.VIEW_OPERATIONS),
    "quotes": (quotes.render, "Quotes", ":material/request_quote:", "quotes", Permission.VIEW_OPERATIONS),
    "customers": (customers.render, "Customers", ":material/groups:", "customers", Permission.VIEW_OPERATIONS),
    "followups": (followups.render, "Follow-ups", ":material/mark_email_unread:", "followups", Permission.VIEW_OPERATIONS),
    "assistant": (assistant_chat.render, "Ask AI", ":material/auto_awesome:", "assistant", Permission.USE_ASSISTANT),
    "reports": (reports.render, "Reports", ":material/insights:", "reports", Permission.VIEW_FINANCIALS),
    "evaluation": (evaluation_lab.render, "AI Evaluation Lab", ":material/science:", "evaluation", Permission.RUN_EVALUATIONS),
    "activity": (workflow_inspector.render, "AI activity log", ":material/account_tree:", "ai-activity", Permission.VIEW_DIAGNOSTICS),
    "portal": (portal.render, "Customer request form", ":material/flight_takeoff:", "request", Permission.VIEW_OPERATIONS),
    "settings": (settings.render, "Settings", ":material/settings:", "settings", Permission.VIEW_OPERATIONS),
}
allowed = [k for k, spec in PAGES.items() if principal.can(spec[4])]
# Landing page: the owner starts on the Overview, a team member on their Inbox.
landing = "home" if principal.role == Role.BUSINESS_OWNER and "home" in allowed else "inbox"
visible = {k: st.Page(PAGES[k][0], title=PAGES[k][1], icon=PAGES[k][2], url_path=PAGES[k][3], default=(k == landing))
           for k in allowed}
st.session_state["_pages"] = visible

if st.session_state.pop("launch_fx", False):
    from sqlalchemy import func, select

    from src.auth import ROLE_LABELS, ROLE_PERMISSIONS
    from src.models import FollowUp, Inquiry, Quote
    from src.ui.common import db
    from src.ui.fx import fx_enabled, launch_sequence

    if fx_enabled():
        with db() as s:
            n_inq = s.scalar(select(func.count(Inquiry.id))) or 0
            n_quotes = s.scalar(select(func.count(Quote.id))) or 0
            n_due = s.scalar(select(func.count(FollowUp.id)).where(FollowUp.status.in_(["draft", "scheduled",
                                                                                         "pending_approval", "approved"]))) or 0
        role = ROLE_LABELS.get(principal.role, "") if principal.role else ""
        perms = len(ROLE_PERMISSIONS[principal.role]) if principal.role else 0
        name = principal.display_name if principal.display_name not in ROLE_LABELS.values() else principal.username
        launch_sequence([f"Identity verified · {principal.username}", f"Role: {role} · {perms} permissions granted",
                         f"Syncing {n_inq:,} inquiries · {n_quotes:,} quotes · {n_due:,} open follow-ups",
                         "AI co-pilot standing by · every action needs your approval"],
                        f"Welcome back, {name}")

main = [visible[k] for k in ("home", "inbox", "inquiries", "quotes", "customers", "followups", "assistant") if k in visible]
more = [visible[k] for k in ("reports", "evaluation", "activity", "portal", "settings") if k in visible]
navigation = st.navigation({"": main, "More": more})
sidebar_brand()
navigation.run()
