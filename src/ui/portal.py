"""Customer portal: "Where would you like to fly?" — a short, three-step flight request form and a status check.

Runs as the PORTAL principal: it can submit a request and look up one request by reference + email, nothing else.
No internal pages, records, prices or analytics are reachable from here.
"""

from __future__ import annotations

import html
from datetime import timedelta
from typing import Any

import streamlit as st
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.auth import PORTAL, as_principal, current_principal
from src.clock import now
from src.config import APP_NAME
from src.exceptions import FlightDeskError
from src.ratelimit import FLIGHT_REQUESTS, STATUS_LOOKUPS, client_key
from src.services.portal_service import (
    AIRCRAFT_PREFERENCES,
    MAX_DAYS_AHEAD,
    MAX_PASSENGERS,
    TIME_PREFERENCES,
    TRIP_TYPE_LABELS,
    FlightRequest,
    check_request_status,
    friendly_errors,
    submit_flight_request,
)
from src.ui.common import CSS, app_settings, db
from src.ui.fx import app_fx
from src.ui.visuals import FLIGHT_SVG, HERO_CSS, place_select, route_map

STEP_FIELDS = {
    1: ["trip_type", "origin", "destination", "departure_date", "return_date", "passengers"],
    2: ["full_name", "email", "phone"],
    3: ["time_preference", "aircraft_preference", "special_requests"],
}
STEP_NAMES = {1: "Your trip", 2: "Your details", 3: "Extras & review"}


def _caller() -> str:
    try:
        return client_key(dict(st.context.headers))
    except Exception:  # headers unavailable (tests, unusual runtimes)
        return client_key(None)


PORTAL_CSS = """
<style>
.block-container { max-width: 860px !important; padding-top: 2.2rem !important; }
.fd-portal-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }
.fd-portal-top .brand { font-family:"Inter Tight","Inter",sans-serif; font-weight:700; color:var(--navy); font-size:1.05rem; letter-spacing:0.02em; }
.fd-portal-top .brand span { color:#1E7F5C; }
.fd-portal-hero { background: linear-gradient(135deg, #0B1B33 0%, #13294B 70%, #1B3B5E 100%); color:#fff; border-radius:22px;
                  padding:46px 40px 40px; position:relative; overflow:hidden; }
.fd-portal-hero:after { content:""; position:absolute; right:-80px; top:-80px; width:300px; height:300px; border-radius:50%;
                        border:1px solid rgba(255,255,255,0.10); box-shadow: 0 0 0 46px rgba(255,255,255,0.03), 0 0 0 100px rgba(255,255,255,0.02); }
.fd-portal-hero .fd-hero-art { width:40%; }
.fd-portal-hero h1, .fd-portal-hero p, .fd-portal-hero .eyebrow { position:relative; z-index:1; }
.fd-portal-hero h1 { color:#fff !important; font-size:2.25rem !important; margin:0 0 10px 0 !important; max-width:62%; }
.fd-portal-hero p { color:#D3DCEB; font-size:1.04rem; margin:0; max-width:58%; line-height:1.55; }
.fd-portal-hero .eyebrow { color:#8FD3B6; font-size:0.78rem; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; margin-bottom:12px; }
.fd-steps { display:flex; gap:8px; margin:6px 0 18px; flex-wrap:wrap; }
.fd-steps div { font-size:0.8rem; padding:5px 12px; border-radius:999px; background:#F1EEE8; color:#5B6478; font-weight:600; }
.fd-steps div.on { background:#0B1B33; color:#fff; }
.fd-steps div.done { background:#E3F1EA; color:#1E6B4A; }
.fd-ref { font-family:"Inter Tight","Inter",sans-serif; font-size:2rem; font-weight:700; letter-spacing:0.04em; color:var(--navy); }
.fd-portal-foot { color:#5B6478; font-size:0.78rem; margin-top:36px; border-top:1px solid var(--line); padding-top:14px; }
</style>
"""


def _chrome() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(PORTAL_CSS, unsafe_allow_html=True)
    app_fx()
    if current_principal() is None:  # public visitor: no sidebar at all
        st.markdown("<style>section[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl']{display:none;}</style>",
                    unsafe_allow_html=True)
    st.markdown(f'<div class="fd-portal-top"><div class="brand">✈ {html.escape(APP_NAME.upper())} '
                '<span>· Private charter</span></div></div>', unsafe_allow_html=True)


def _footer() -> None:
    st.markdown('<div class="fd-portal-foot">Independent demo project — not affiliated with or endorsed by Flygreen or any '
                "aircraft operator. Sending a request doesn't book a flight or take a payment; an advisor replies by email. "
                "Your details are used only to respond to your request.</div>", unsafe_allow_html=True)


def _state() -> dict[str, Any]:
    return st.session_state.setdefault("portal", {"step": 0, "values": {}})


def _initial(key: str, value: Any) -> dict[str, Any]:
    """Initial value for a widget, unless the visitor has already changed it during this step."""
    return {} if key in st.session_state else {"value": value}


def _errors_for(values: dict[str, Any], fields: list[str]) -> dict[str, str]:
    """Validate everything entered so far, reporting only errors for ``fields``."""
    today = now().date()
    probe = {"full_name": "Customer", "email": "customer@example.com", "origin": "Origin", "destination": "Destination",
             "departure_date": today + timedelta(days=30), "passengers": 1, "trip_type": "one_way"}
    probe.update({k: v for k, v in values.items() if v not in (None, "")})
    for f in fields:  # fields on this step that are empty must be reported as missing
        if values.get(f) in (None, ""):
            probe.pop(f, None)
    try:
        FlightRequest.model_validate(probe)
    except ValidationError as exc:
        return {k: v for k, v in friendly_errors(exc).items() if k in fields}
    return {}


def _show_errors(errors: dict[str, str]) -> None:
    if errors:
        st.error("Please check: " + " ".join(f"**{_label(k)}** — {v}" for k, v in errors.items()))


def _label(field: str) -> str:
    from src.services.portal_service import FIELD_LABELS

    return FIELD_LABELS.get(field, field.replace("_", " ").capitalize())


def _progress(step: int) -> None:
    chips = "".join(f'<div class="{"on" if n == step else "done" if n < step else ""}">{n}. {name}</div>'
                    for n, name in STEP_NAMES.items())
    st.markdown(f'<div class="fd-steps">{chips}</div>', unsafe_allow_html=True)


def _landing(state: dict[str, Any]) -> None:
    st.markdown(HERO_CSS + f'<div class="fd-portal-hero">{FLIGHT_SVG}<div class="eyebrow">Private jet charter</div>'
                "<h1>Where would you like to fly?</h1>"
                "<p>Tell us about your trip in three short steps. An advisor will review it and reply by email — "
                "no account needed.</p></div>", unsafe_allow_html=True)
    st.markdown(" ")
    a, b, _ = st.columns([1.3, 1.5, 2])
    if a.button("Request a flight", type="primary", width="stretch", key="portal_start"):
        state["step"] = 1
        st.rerun()
    if b.button("Check an existing request", width="stretch", key="portal_to_status"):
        state["step"] = "status"
        st.rerun()
    st.markdown(" ")
    cols = st.columns(3)
    for col, (title, body) in zip(cols, [
        ("1 · Tell us your trip", "Where from, where to, when and how many people."),
        ("2 · We review it", "An advisor checks the details and looks for suitable aircraft."),
        ("3 · We reply by email", "You get options or a quick question. Nothing is booked until you confirm."),
    ], strict=True):
        col.markdown(f'<div class="fd-step"><div style="font-weight:650">{title}</div>'
                     f'<div style="color:#6B7280;font-size:0.88rem;margin-top:4px">{body}</div></div>', unsafe_allow_html=True)


def _step_trip(state: dict[str, Any]) -> None:
    v = state["values"]
    today = now().date()
    st.markdown("### Your trip")
    trip = st.radio("Trip", list(TRIP_TYPE_LABELS), format_func=TRIP_TYPE_LABELS.get, horizontal=True, key="pf_trip_type",
                    index=list(TRIP_TYPE_LABELS).index(v.get("trip_type", "one_way")))
    a, b = st.columns(2)
    origin = place_select("From", "pf_origin", v.get("origin"), container=a, placeholder="City or airport, e.g. London Luton")
    destination = place_select("To", "pf_destination", v.get("destination"), container=b,
                               placeholder="City or airport, e.g. Nice")
    a, b, c = st.columns(3)
    dep = a.date_input("Departure date", key="pf_departure_date", **_initial("pf_departure_date", v.get("departure_date")), min_value=today,
                       max_value=today + timedelta(days=MAX_DAYS_AHEAD), format="DD/MM/YYYY")
    ret = None
    if trip == "round_trip":
        ret = b.date_input("Return date", key="pf_return_date", **_initial("pf_return_date", v.get("return_date")), min_value=today,
                           max_value=today + timedelta(days=MAX_DAYS_AHEAD), format="DD/MM/YYYY")
    pax = c.number_input("Passengers", min_value=1, max_value=MAX_PASSENGERS, step=1, key="pf_passengers",
                         **_initial("pf_passengers", v.get("passengers")), placeholder="e.g. 4")
    if origin and destination:
        route_map(origin, destination, height=230, key="portal_route_1")
    if st.button("Continue", type="primary", key="portal_next_1"):
        values = {"trip_type": trip, "origin": origin, "destination": destination, "departure_date": dep,
                  "return_date": ret, "passengers": int(pax) if pax is not None else None}
        v.update(values)
        errors = _errors_for(v, STEP_FIELDS[1])
        if errors:
            state["errors"] = errors
        else:
            state.pop("errors", None)
            state["step"] = 2
        st.rerun()


def _step_contact(state: dict[str, Any]) -> None:
    v = state["values"]
    st.markdown("### Your details")
    st.caption("We only use these to reply to your request.")
    name = st.text_input("Full name", key="pf_full_name", autocomplete="name", **_initial("pf_full_name", v.get("full_name") or ""))
    a, b = st.columns(2)
    email = a.text_input("Email", key="pf_email", autocomplete="email", placeholder="you@example.com",
                         **_initial("pf_email", v.get("email") or ""))
    phone = b.text_input("Phone (optional)", key="pf_phone", autocomplete="tel", placeholder="+44 20 7946 0000",
                         **_initial("pf_phone", v.get("phone") or ""))
    x, y, _ = st.columns([1, 1.2, 3])
    if x.button("Back", key="portal_back_2"):
        v.update({"full_name": name, "email": email, "phone": phone})
        state["step"] = 1
        state.pop("errors", None)
        st.rerun()
    if y.button("Continue", type="primary", key="portal_next_2"):
        v.update({"full_name": name, "email": email, "phone": phone or None})
        errors = _errors_for(v, STEP_FIELDS[1] + STEP_FIELDS[2])
        if errors:
            state["errors"] = errors
        else:
            state.pop("errors", None)
            state["step"] = 3
        st.rerun()


def _review_rows(v: dict[str, Any]) -> list[tuple[str, str]]:
    rows = [("Trip", TRIP_TYPE_LABELS.get(v.get("trip_type", "one_way"), "")),
            ("Route", f"{v.get('origin')} → {v.get('destination')}"),
            ("Departure", f"{v['departure_date']:%A %d %B %Y}" if v.get("departure_date") else "—")]
    if v.get("trip_type") == "round_trip" and v.get("return_date"):
        rows.append(("Return", f"{v['return_date']:%A %d %B %Y}"))
    rows += [("Passengers", str(v.get("passengers"))), ("Name", v.get("full_name") or ""), ("Email", v.get("email") or ""),
             ("Phone", v.get("phone") or "not given")]
    return rows


def _step_extras(state: dict[str, Any]) -> None:
    v = state["values"]
    st.markdown("### Extras (optional)")
    a, b = st.columns(2)
    time_pref = a.selectbox("Preferred departure time", TIME_PREFERENCES, key="pf_time_preference",
                            index=TIME_PREFERENCES.index(v.get("time_preference") or TIME_PREFERENCES[0]))
    aircraft = b.selectbox("Aircraft preference", AIRCRAFT_PREFERENCES, key="pf_aircraft_preference",
                           index=AIRCRAFT_PREFERENCES.index(v.get("aircraft_preference") or AIRCRAFT_PREFERENCES[0]))
    special = st.text_area("Anything else we should know?", key="pf_special_requests", max_chars=900, height=90,
                           placeholder="e.g. travelling with a dog, extra luggage, catering, wheelchair assistance",
                           **_initial("pf_special_requests", v.get("special_requests") or ""))
    st.markdown("#### Check your request")
    rows = "".join(f'<div class="k">{html.escape(k)}</div><div class="v">{html.escape(val)}</div>' for k, val in _review_rows(v))
    st.markdown(f'<div class="fd-panel"><div class="fd-kv">{rows}</div></div>', unsafe_allow_html=True)
    route_map(v.get("origin"), v.get("destination"), height=210, key="portal_route_3")
    st.caption("This is a request, not a booking. Nothing is booked or charged.")
    x, y, _ = st.columns([1, 1.4, 2.6])
    if x.button("Back", key="portal_back_3"):
        v.update({"time_preference": time_pref, "aircraft_preference": aircraft, "special_requests": special})
        state["step"] = 2
        state.pop("errors", None)
        st.rerun()
    if y.button("Send request", type="primary", key="portal_submit"):
        v.update({"time_preference": time_pref, "aircraft_preference": aircraft, "special_requests": special or None})
        _submit(state)


def _submit(state: dict[str, Any]) -> None:
    v = state["values"]
    try:
        req = FlightRequest.model_validate({k: val for k, val in v.items() if val not in (None, "")})
    except ValidationError as exc:
        errors = friendly_errors(exc)
        state["errors"] = errors
        state["step"] = next((n for n, fields in STEP_FIELDS.items() if set(fields) & set(errors)), 3)
        st.rerun()
        return
    if not FLIGHT_REQUESTS.allow(_caller()):
        st.error("We've received several requests from your connection recently. Please wait a while before sending "
                 "another, or email us directly.")
        return
    try:
        with as_principal(PORTAL), db() as s:
            result = submit_flight_request(s, req)
    except (SQLAlchemyError, FlightDeskError, OSError):
        st.error("Sorry — we couldn't send your request just now. Your details are still here; please try again in a "
                 "minute. If it keeps happening, email us instead.")
        return
    state.update(step="done", reference=result.reference, duplicate=result.duplicate, sent_email=req.email,
                 summary=_review_rows(v), values={})
    for key in [k for k in st.session_state if str(k).startswith("pf_")]:
        del st.session_state[key]
    st.rerun()


def _confirmation(state: dict[str, Any]) -> None:
    st.markdown("## ✓ Request received" if not state.get("duplicate") else "## ✓ We already have this request")
    if state.get("duplicate"):
        st.info("This exact request was already sent, so we didn't create a second one. Here's its reference.")
    st.markdown("Your reference")
    st.markdown(f'<div class="fd-ref">{html.escape(state["reference"])}</div>', unsafe_allow_html=True)
    st.caption(f"Keep this reference. You can check the status with it and the email you used ({state.get('sent_email')}).")
    st.markdown("#### What happens next")
    st.markdown("1. An advisor reviews your request.\n"
                "2. We email you — either with a question about the details or with aircraft options.\n"
                "3. **Nothing is booked or charged** until you confirm with your advisor.")
    with st.expander("Your request"):
        rows = "".join(f'<div class="k">{html.escape(k)}</div><div class="v">{html.escape(val)}</div>'
                       for k, val in state.get("summary", []))
        st.markdown(f'<div class="fd-kv">{rows}</div>', unsafe_allow_html=True)
    a, b, _ = st.columns([1.3, 1.5, 2])
    if a.button("Check status", key="portal_done_status", width="stretch"):
        st.session_state["portal_lookup_ref"] = state["reference"]
        st.session_state["portal_lookup_email"] = state.get("sent_email", "")
        state["step"] = "status"
        st.rerun()
    if b.button("Send another request", key="portal_again", width="stretch"):
        st.session_state["portal"] = {"step": 1, "values": {}}
        st.rerun()


def _status(state: dict[str, Any]) -> None:
    st.markdown("## Check your request")
    st.caption("Enter the reference from your confirmation and the email address you used.")
    with st.form("portal_lookup"):
        ref = st.text_input("Reference", key="portal_lookup_ref", placeholder="INQ-…")
        email = st.text_input("Email", key="portal_lookup_email")
        go = st.form_submit_button("Check status", type="primary")
    if go:
        if not STATUS_LOOKUPS.allow(_caller()):
            st.error("Too many checks from your connection. Please try again in a few minutes, or reply to our email.")
        else:
            try:
                with as_principal(PORTAL), db() as s:
                    found = check_request_status(s, ref, email)
            except (SQLAlchemyError, FlightDeskError):
                found, failed = None, True
            else:
                failed = False
            if failed:
                st.error("We couldn't check right now. Please try again in a minute.")
            elif found is None:
                st.warning("We couldn't find a request with that reference and email. Please check both and try again.")
            else:
                st.success(f"**{found['status']}** — {found['detail']}")
                dates = f"{found['departure_date']:%d %b %Y}" if found["departure_date"] else "date to be confirmed"
                if found.get("return_date"):
                    dates += f" – {found['return_date']:%d %b %Y}"
                st.markdown(f"**{html.escape(found['route'])}** · {dates} · {found['passengers'] or '?'} passenger(s)  \n"
                            f"Reference {found['reference']} · sent {found['submitted']:%d %b %Y}")
    if st.button("← Back", key="portal_status_back"):
        state["step"] = 0
        st.rerun()


def render() -> None:
    app_settings()  # make sure the database is ready
    _chrome()
    state = _state()
    step = state.get("step", 0)
    if step == 0:
        _landing(state)
    elif step == "done":
        _confirmation(state)
    elif step == "status":
        _status(state)
    else:
        st.markdown("## Request a flight")
        _progress(int(step))
        _show_errors(state.pop("errors", {}))  # shown once; cleared as soon as the visitor edits anything
        {1: _step_trip, 2: _step_contact, 3: _step_extras}[int(step)](state)
    _footer()


def render_status() -> None:
    """Direct link to the status check."""
    st.session_state.setdefault("portal", {"step": "status", "values": {}})
    if st.session_state["portal"].get("step") == 0:
        st.session_state["portal"]["step"] = "status"
    render()
