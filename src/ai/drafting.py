"""Follow-up draft generation (template or OpenAI) plus a deterministic claims guard.

Drafts use only a *facts* dictionary assembled from database records. Whatever produced the draft,
:func:`check_draft_claims` flags amounts, confirmations, discounts, availability or "already sent"
claims that the facts do not support. Nothing is ever sent by this application.
"""

from __future__ import annotations

import re
from typing import Any

from src.ai.client import AIClient, TokenUsage
from src.ai.prompts import FOLLOWUP_INSTRUCTIONS, followup_input
from src.ai.text_parsing import find_money
from src.domain import FollowUpType, Tone
from src.schemas import FollowUpDraft, LLMFollowUpDraft

SERVICE_TEXT = {
    "accommodation": "accommodation", "ground_transport": "ground transport", "catering": "catering",
    "pet_travel": "travelling with a pet", "special_luggage": "special or oversized luggage", "experiences": "experiences",
}
PLACEHOLDER_RE = re.compile(r"\[\[[^\]]{1,120}\]\]")


def _route(f: dict[str, Any]) -> str:
    o, d = f.get("origin"), f.get("destination")
    if o and d:
        return f"{o} to {d}"
    if d:
        return f"to {d}"
    if o:
        return f"from {o}"
    return "your upcoming trip"


def _dates(f: dict[str, Any]) -> str | None:
    if f.get("departure_date"):
        return f"departing {f['departure_date']}"
    if f.get("departure_earliest") and f.get("departure_latest"):
        return f"departing between {f['departure_earliest']} and {f['departure_latest']}"
    return None


def _greeting(tone: Tone, name: str | None = None) -> str:
    who = name if name and not name.lower().startswith(("new customer", "customer (")) else "[[Customer name]]"
    return {"professional": f"Dear {who},", "warm": f"Hello {who},", "concise": f"Hi {who},"}[tone.value]


def _closing(tone: Tone) -> str:
    return {
        "professional": "Kind regards,\n[[Advisor name]]",
        "warm": "Warm regards, and thank you again for thinking of us,\n[[Advisor name]]",
        "concise": "Thanks,\n[[Advisor name]]",
    }[tone.value]


def template_draft(facts: dict[str, Any], followup_type: FollowUpType, tone: Tone) -> FollowUpDraft:
    """Deterministic, fact-only draft. Unknown information becomes a [[placeholder]]."""
    inq = facts["inquiry"]
    route = _route(inq)
    when = _dates(inq)
    trip = route + (f", {when}" if when else "")
    quotes = facts.get("quotes", [])
    services = [SERVICE_TEXT.get(s, s) for s in inq.get("requested_services", [])]
    concise = tone == Tone.CONCISE
    lines: list[str] = []

    if followup_type == FollowUpType.MISSING_INFORMATION:
        subject = f"A few details for your trip {route}"
        questions = [m["question"] for m in inq.get("missing_information_questions", [])]
        lines.append(f"Thank you for your inquiry about {trip}." if not concise else f"Thanks for your inquiry about {trip}.")
        if questions:
            lines.append("To request accurate options from operators, could you please confirm:")
            lines += [f"  • {q}" for q in questions]
        else:
            lines.append("We have the key details we need. [[Advisor to confirm: anything further to ask?]]")
    elif followup_type == FollowUpType.QUOTE_FOLLOWUP:
        subject = f"Following up on the options for {route}"
        presented = [q for q in quotes if q["status"] in ("presented", "received")]
        if presented:
            lines.append(f"I wanted to follow up on the option(s) we shared for {trip}:")
            for q in presented:
                cap = f", up to {q['passenger_capacity']} passengers" if q.get("passenger_capacity") else ""
                ac = q.get("aircraft_type") or "[[Advisor to confirm: aircraft type]]"
                lines.append(f"  • {q['operator_label']} — {ac}{cap}: {q.get('all_in_price', q['total_price']):,.2f} {q['currency']}")
            lines.append("Pricing and aircraft availability are subject to operator confirmation at the time of booking.")
        else:
            lines.append(f"I wanted to follow up regarding {trip}. [[Advisor to confirm: which option was shared?]]")
        lines.append("Please let me know if you have any questions or would like to proceed.")
    elif followup_type == FollowUpType.ALTERNATIVES:
        subject = f"Alternative options for {route}"
        if len(quotes) >= 2:
            lines.append(f"Alongside the option discussed for {trip}, the following documented alternatives are on file:")
            for q in quotes:
                ac = q.get("aircraft_type") or "aircraft type to be confirmed"
                lines.append(f"  • {q['operator_label']} — {ac}: {q.get('all_in_price', q['total_price']):,.2f} {q['currency']}")
            lines.append("All options remain subject to operator confirmation.")
        else:
            lines.append(f"Regarding {trip}, [[Advisor to confirm: alternatives to offer — none are documented yet]].")
    elif followup_type == FollowUpType.ACCOMMODATION:
        subject = f"Accommodation for your trip {route}"
        if "accommodation" in inq.get("requested_services", []):
            lines.append(f"You mentioned you would like help with accommodation for {trip}.")
            lines.append("To coordinate this, could you share:")
            lines += ["  • Preferred check-in and check-out dates", "  • Number of rooms and any preferences (location, style)",
                      "  • Any budget guidance for the stay"]
        else:
            lines.append(f"Regarding {trip}: [[Advisor to confirm: accommodation was not requested in the inquiry — check before sending]].")
    elif followup_type == FollowUpType.ADDITIONAL_SERVICES:
        subject = f"Arrangements for your trip {route}"
        if services:
            lines.append(f"For {trip}, you mentioned: {', '.join(services)}.")
            lines.append("Could you share any specific requirements so we can coordinate these with the operator?")
        else:
            lines.append(f"For {trip}, please let us know if there is anything else you would like us to help arrange.")
    elif followup_type == FollowUpType.INITIAL_RESPONSE:
        subject = f"Your private flight inquiry: {route}"
        lines.append(f"Thank you for getting in touch about {trip}." if not concise else f"Thanks for your inquiry about {trip}.")
        if inq.get("passenger_count"):
            lines.append(f"We have noted {inq['passenger_count']} passenger(s).")
        questions = [m["question"] for m in inq.get("missing_information_questions", [])]
        if questions:
            lines.append("So we can request the right options from operators, could you also confirm:")
            lines += [f"  • {q}" for q in questions]
        lines.append("We will come back to you with suitable options [[Advisor to confirm: expected timing]]. "
                     "Pricing and aircraft availability are always subject to operator confirmation.")
    elif followup_type == FollowUpType.BOOKING_CONFIRMATION:
        subject = f"Your trip {route}"
        if facts.get("bookings_confirmed"):
            accepted = [q for q in quotes if q["status"] == "accepted"]
            lines.append(f"Thank you for confirming your trip ({trip}).")
            if accepted:
                q = accepted[0]
                ac = q.get("aircraft_type") or "[[Advisor to confirm: aircraft type]]"
                lines.append(f"Operator: {q['operator_label']} · Aircraft: {ac} · Total: "
                             f"{q.get('all_in_price', q['total_price']):,.2f} {q['currency']}")
            lines.append("[[Advisor to confirm: departure time, FBO/terminal details and passenger names]]")
        else:
            lines.append("[[Advisor: no confirmed booking is recorded for this trip yet — wait for the operator's "
                         "confirmation and record the booking before using this email]]")
            lines.append(f"Regarding your trip ({trip}): [[Advisor to add the confirmed arrangements]].")
    elif followup_type == FollowUpType.GENERAL_UPDATE:
        subject = f"An update on your trip {route}"
        lines.append(f"A quick update on {trip}: [[Advisor to add the update]].")
        lines.append("Please let me know if you have any questions in the meantime.")
    elif followup_type == FollowUpType.POST_INTERACTION:
        subject = f"Thank you — next steps for {route}"
        lines.append("Thank you for your time [[Advisor to confirm: date and channel of the conversation]].")
        lines.append("As next steps, [[Advisor to confirm: agreed next steps]].")
    else:  # POST_TRIP
        subject = f"Your recent trip {route}"
        if facts.get("bookings_completed"):
            lines.append(f"I hope your recent trip ({route}) went smoothly.")
        else:
            lines.append(f"[[Advisor to confirm: the trip ({route}) is recorded as completed before sending]]")
        lines.append("If you have a moment, we would value any feedback on your experience.")

    body = "\n".join([_greeting(tone, inq.get("customer_label")), "", *lines, "", _closing(tone)])
    return FollowUpDraft(subject=subject, body=body, method="template",
                         placeholders=PLACEHOLDER_RE.findall(subject + "\n" + body),
                         warnings=check_draft_claims(subject + "\n" + body, facts),
                         facts_used=sorted(k for k, v in inq.items() if v not in (None, [], "")))


_CLAIM_CHECKS: list[tuple[str, re.Pattern[str], str]] = [
    ("booking_confirmed", re.compile(r"\b(booking|reservation|flight)\s+(is|has been|was)\s+(confirmed|secured|booked)\b|\byou(?:'re| are) (all )?(booked|confirmed)\b", re.I),
     "Claims a booking is confirmed, but no confirmed booking is on record."),
    ("discount", re.compile(r"\b(discount|% off|percent off|special price|reduced rate|complimentary upgrade)\b", re.I),
     "Mentions a discount or special price; none is recorded."),
    ("availability", re.compile(r"\b(is|are) (available|guaranteed)\b|\bavailability (is )?(confirmed|guaranteed)\b|\bguarantee(d)?\b", re.I),
     "Claims availability or a guarantee; availability is never confirmed by this application."),
    ("already_sent", re.compile(r"\b(i|we) (have )?(sent|emailed|forwarded)\b|\bemail (was|has been) sent\b|\bas (discussed|agreed) (on|in|during) (our|the) (call|meeting|conversation)\b", re.I),
     "Refers to a prior message or conversation that is not recorded."),
]


def check_draft_claims(text: str, facts: dict[str, Any]) -> list[str]:
    """Deterministic checks for unsupported factual claims in a draft."""
    warnings: list[str] = []
    known_amounts = {round(float(q[k]), 2) for q in facts.get("quotes", [])
                     for k in ("total_price", "all_in_price", "additional_fees") if q.get(k)}
    if facts["inquiry"].get("budget_amount"):
        known_amounts.add(round(float(facts["inquiry"]["budget_amount"]), 2))
    for m in find_money(text):
        if not any(abs(m.amount - k) <= 0.01 for k in known_amounts):
            warnings.append(f"Mentions an amount ({m.amount:,.2f}) that does not match any recorded quote or budget.")
    has_confirmed_booking = bool(facts.get("bookings_confirmed"))
    has_interaction = bool(facts.get("recorded_interactions"))
    for key, pattern, message in _CLAIM_CHECKS:
        if not pattern.search(text):
            continue
        if key == "booking_confirmed" and has_confirmed_booking:
            continue
        if key == "already_sent" and has_interaction:
            continue
        warnings.append(message)
    return warnings


def llm_draft(facts: dict[str, Any], followup_type: FollowUpType, tone: Tone, client: AIClient) -> tuple[FollowUpDraft, TokenUsage, str]:
    """OpenAI-generated draft. Raises AIServiceError on failure (caller shows the error; no silent fallback)."""
    result = client.parse(instructions=FOLLOWUP_INSTRUCTIONS, user_input=followup_input(facts, followup_type.value, tone.value),
                          schema=LLMFollowUpDraft, purpose="followup_draft")
    draft = result.parsed
    text = draft.subject + "\n" + draft.body
    return (
        FollowUpDraft(subject=draft.subject[:200], body=draft.body[:8000], method="openai",
                      placeholders=PLACEHOLDER_RE.findall(text), warnings=check_draft_claims(text, facts),
                      facts_used=sorted(k for k, v in facts["inquiry"].items() if v not in (None, [], ""))),
        result.usage,
        result.model,
    )


def mailto_link(to: str | None, subject: str, body: str) -> str:
    """A mailto: link that opens the owner's own email app with the draft filled in. Nothing is sent by FlightDesk."""
    from urllib.parse import quote

    return f"mailto:{quote(to or '')}?subject={quote(subject)}&body={quote(body[:1800])}"
