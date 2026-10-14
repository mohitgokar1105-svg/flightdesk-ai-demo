"""Rule-based extraction baseline (no AI).

Purpose: (1) an evaluation baseline for the LLM approach and (2) a clearly labelled offline fallback
in demo mode. It is intentionally transparent and limited — e.g. it only knows places in the
gazetteer plus capitalised "from X" / "to Y" phrases, and it does not understand paraphrase.
"""

from __future__ import annotations

import re
from datetime import date
from typing import cast

from src.ai.locations import find_mentions
from src.ai.text_parsing import (
    DEPART_CUE_RE,
    FLEX_FIXED,
    FLEX_FLEXIBLE,
    MULTI_LEG_RE,
    ONE_WAY_RE,
    PURPOSE_PRIORITY,
    RETURN_CUE_RE,
    ROUND_TRIP_RE,
    DateMention,
    find_contact,
    find_currency_words,
    find_dates,
    find_money,
    find_passengers,
    find_purposes,
    find_services,
    find_special_requirements,
    find_time_preference,
    resolve_day_only,
)
from src.schemas import (
    DateSpec,
    FlexLiteral,
    InquiryExtraction,
    PurposeLiteral,
    TripTypeLiteral,
    ValidationIssue,
)

_ORIGIN_CUE = re.compile(r"(\bfrom|\bout of|\bdeparting|\bdepart|\bleaving|\bleave|\bbased in|\bpick(ing)? us up (in|at))\s+(the\s+)?$", re.I)
_DEST_CUE = re.compile(r"(\bto|\binto|\bfor|\btowards|\bheading|\bdestination( is)?:?|\barriv(e|ing) (in|at)|\bland(ing)? (in|at)|\bvisit(ing)?|\bin)\s+(the\s+)?$", re.I)
_ROUTE_SEP = re.compile(r"^\s*(to|->|→|–|-|>)\s*$", re.I)
_NON_PLACES = {
    "i", "we", "my", "our", "the", "please", "thanks", "thank", "hi", "hello", "dear", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "christmas", "thanksgiving", "new", "easter", "demo", "mr", "mrs", "ms",
    "dr", "you", "us", "it", "them", "a", "an", "flight", "jet", "book", "need", "be", "do", "go", "get", "fly",
}
_CAP_PLACE = r"((?:St\.|Saint|San|Santa|Los|Las|Port|Fort|Ft\.|Mount|Mt\.|New|Palm|West|East|North|South|Lake)?\s?[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,2})"


def _capitalised_fallback(text: str, cue: str) -> str | None:
    for m in re.finditer(r"\b" + cue + r"\s+" + _CAP_PLACE, text):
        candidate = m.group(1).strip()
        words = candidate.split()
        while words and words[-1].lower() in _NON_PLACES:
            words.pop()
        if not words or words[0].lower() in _NON_PLACES:
            continue
        return " ".join(words)
    return None


def _route(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    mentions = find_mentions(text)
    origin = destination = None
    o_ev = d_ev = None
    # "X to Y" adjacency is the strongest signal.
    for a, b in zip(mentions, mentions[1:], strict=False):
        if _ROUTE_SEP.match(text[a.end:b.start]):  # same place on both sides is kept so validation can flag it
            origin, destination = a.canonical, b.canonical
            o_ev, d_ev = a.surface, b.surface
            break
    if origin is None:
        for mtn in mentions:
            before = text[max(0, mtn.start - 30):mtn.start]
            if origin is None and _ORIGIN_CUE.search(before):
                origin, o_ev = mtn.canonical, mtn.surface
            elif destination is None and _DEST_CUE.search(before) and mtn.canonical != origin:
                destination, d_ev = mtn.canonical, mtn.surface
    unassigned = [m for m in mentions if m.canonical not in (origin, destination)]
    if origin is None and destination is None and len(mentions) >= 2:
        origin, destination = mentions[0].canonical, mentions[1].canonical
        o_ev, d_ev = mentions[0].surface, mentions[1].surface
    elif destination is None and unassigned and origin is not None:
        destination, d_ev = unassigned[0].canonical, unassigned[0].surface
    elif origin is None and destination is None and len(mentions) == 1:
        destination, d_ev = mentions[0].canonical, mentions[0].surface
    if origin is None:
        fb = _capitalised_fallback(text, "from")
        if fb and fb != destination:
            origin, o_ev = fb, fb
    if destination is None:
        fb = _capitalised_fallback(text, "to")
        if fb and fb != origin:
            destination, d_ev = fb, fb
    return origin, destination, o_ev, d_ev


def _assign_dates(text: str, ref: date, round_trip_hint: bool) -> tuple[DateSpec, DateSpec, list[ValidationIssue]]:
    mentions = find_dates(text, ref)
    departure, ret = DateSpec(), DateSpec()
    issues: list[ValidationIssue] = []
    dep_filled = ret_filled = False
    last_exact: date | None = None
    for m in mentions:
        if m.ambiguous and m.ambiguous.startswith("day-only:"):
            if last_exact is None:
                continue
            resolved = resolve_day_only(int(m.ambiguous.split(":")[1]), last_exact)
            if resolved is None:
                continue
            m = DateMention(m.start, m.end, exact=resolved, text=m.text)
        before = text[max(0, m.start - 30):m.start]
        is_return = bool(RETURN_CUE_RE.search(before)) and dep_filled
        is_depart = bool(DEPART_CUE_RE.search(before))
        if m.ambiguous == "invalid calendar date":
            issues.append(ValidationIssue(code="DATE_INVALID", severity="error", field="departure_date",
                                          message=f"'{m.text}' is not a valid calendar date."))
            if not dep_filled:
                dep_filled = True
            continue
        if m.ambiguous:
            issues.append(ValidationIssue(code="AMBIGUOUS_DATE", severity="warning",
                                          field="return_date" if is_return else "departure_date",
                                          message=f"'{m.text}': {m.ambiguous}."))
            if is_return:
                ret_filled = True
            elif not dep_filled:
                dep_filled = True
            continue
        spec = DateSpec(exact=m.exact, earliest=m.earliest, latest=m.latest, source_text=m.text)
        if m.second is not None:
            if round_trip_hint and not dep_filled:
                departure, ret = DateSpec(exact=m.exact, source_text=m.text), DateSpec(exact=m.second, source_text=m.text)
                dep_filled = ret_filled = True
                last_exact = m.second
                continue
            spec = DateSpec(earliest=m.exact, latest=m.second, source_text=m.text)
        if (is_return or (dep_filled and not is_depart)) and not ret_filled:
            ret, ret_filled = spec, True
        elif not dep_filled:
            departure, dep_filled = spec, True
        last_exact = m.exact or m.latest or last_exact
    return departure, ret, issues


def extract_rule_based(text: str, reference_date: date) -> InquiryExtraction:
    """Extract inquiry fields with deterministic rules. Output still goes through shared post-processing."""
    evidence: dict[str, str] = {}
    origin, destination, o_ev, d_ev = _route(text)
    if o_ev:
        evidence["origin"] = o_ev
    if d_ev:
        evidence["destination"] = d_ev

    trip_type: TripTypeLiteral | None
    if ONE_WAY_RE.search(text):
        trip_type = "one_way"
    elif MULTI_LEG_RE.search(text):
        trip_type = "multi_leg"
    elif ROUND_TRIP_RE.search(text):
        trip_type = "round_trip"
    else:
        trip_type = None

    departure, ret, date_issues = _assign_dates(text, reference_date, trip_type == "round_trip")
    if trip_type is None and not ret.is_empty:
        trip_type = "round_trip"

    pax = find_passengers(text)
    passenger_count = pax.totals[0] if len(pax.totals) == 1 else None

    money = find_money(text)
    budget_amount = None
    currency = None
    issues = list(date_issues)
    if money:
        uppers = [m for m in money if m.is_range_upper]
        if uppers:
            chosen = uppers[0]
            issues.append(ValidationIssue(code="BUDGET_RANGE", severity="info", field="budget_amount",
                                          message="Budget given as a range; the upper bound was recorded."))
            budget_amount, currency = chosen.amount, chosen.currency
        elif len({m.amount for m in money}) == 1:
            budget_amount, currency = money[0].amount, money[0].currency
        else:
            issues.append(ValidationIssue(code="BUDGET_CONFLICT", severity="warning", field="budget_amount",
                                          message="Several different amounts are mentioned; the budget is unclear."))
    if currency is None:
        words = find_currency_words(text)
        if len(words) == 1:
            currency = words[0]

    purposes = find_purposes(text)
    trip_purpose = cast("PurposeLiteral | None", next((p for p in PURPOSE_PRIORITY if p in purposes), None))
    if trip_purpose:
        evidence["trip_purpose"] = purposes[trip_purpose][0]

    flexibility: FlexLiteral | None
    if FLEX_FIXED.search(text):
        flexibility = "fixed"
    elif FLEX_FLEXIBLE.search(text):
        flexibility = "flexible"
    else:
        flexibility = None

    services = find_services(text)
    for svc, snippets in services.items():
        evidence[f"service:{svc}"] = snippets[0]
    contact = find_contact(text)

    return InquiryExtraction(
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        departure=departure,
        return_date=ret,
        passenger_count=passenger_count,
        passenger_counts_mentioned=pax.totals,
        trip_purpose=trip_purpose,
        date_flexibility=flexibility,
        budget_amount=budget_amount,
        currency=currency,
        requested_services=list(services.keys()),
        validation_issues=issues,
        evidence=evidence,
        method="rule_based",
        customer_name=contact.name,
        contact_email=contact.email,
        contact_phone=contact.phone,
        departure_time_preference=find_time_preference(text),
        special_requirements=find_special_requirements(text),
    )
