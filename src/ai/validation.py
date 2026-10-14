"""Deterministic post-processing applied to *every* extraction, whatever produced it.

The model is never assumed to be correct. This module:
1. normalises values (canonical locations, currencies, services),
2. validates them (dates, ordering, passenger counts, budgets),
3. detects ambiguities and contradictions directly from the source text,
4. checks each extracted value is *grounded* in the source text (unsupported-claim detection),
5. derives the required missing information with explicit business rules.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from src.ai.locations import GAZETTEER, canonicalize, find_mentions, is_ambiguous
from src.ai.text_parsing import (
    CLOCK_24_RE,
    CLOCK_RE,
    FLEX_FIXED,
    FLEX_FLEXIBLE,
    MULTI_LEG_RE,
    NUMBER_WORDS,
    ONE_WAY_RE,
    ROUND_TRIP_RE,
    TIME_OF_DAY_RE,
    find_currency_words,
    find_dates,
    find_money,
    find_passengers,
    find_purposes,
    find_services,
    find_special_requirements,
)
from src.domain import REQUESTABLE_SERVICES, SUPPORTED_CURRENCIES
from src.schemas import DateSpec, InquiryExtraction, MissingItem, Severity, ValidationIssue
from src.security import detect_prompt_injection

MAX_TYPICAL_PRIVATE_JET_PAX = 19
MAX_PAX = 500
FAR_FUTURE_DAYS = 730

# Validation codes that the evaluation scores (model-specific free-text flags are excluded).
EVALUATED_ISSUE_CODES = {
    "AMBIGUOUS_LOCATION", "AMBIGUOUS_DATE", "PASSENGER_COUNT_CONFLICT", "PASSENGER_COUNT_INVALID",
    "PASSENGER_COUNT_LARGE", "RETURN_BEFORE_DEPARTURE", "DATE_IN_PAST", "SAME_ORIGIN_DESTINATION",
    "ONE_WAY_WITH_RETURN", "BUDGET_CONFLICT", "BUDGET_WITHOUT_CURRENCY", "PROMPT_INJECTION_SUSPECTED",
    "DATE_INVALID", "DATE_WINDOW_INVERTED", "BUDGET_INVALID", "DATE_FAR_FUTURE",
}

MISSING_QUESTIONS = {
    "origin": "Where will the flight depart from?",
    "destination": "Where would you like to fly to?",
    "departure_date": "What is your preferred departure date?",
    "exact_departure_date": "Which specific departure date within that period works best?",
    "passenger_count": "How many passengers will be travelling?",
    "trip_type": "Is this a one-way trip, or will you need a return flight?",
    "return_date": "When would you like to return?",
    "currency": "Which currency is your budget in?",
}


def _issue(code: str, severity: Severity, field: str | None, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity=severity, field=field, message=message)


# ---------------------------------------------------------------------------
# Grounding (unsupported-claim) checks
# ---------------------------------------------------------------------------
def _word_in(text_l: str, needle: str) -> bool:
    return re.search(r"(?<![\w'])" + re.escape(needle.lower()) + r"(?![\w'])", text_l) is not None


def location_grounded(value: str, text: str) -> bool:
    text_l = text.lower()
    canonical = canonicalize(value) or value
    if canonical in GAZETTEER:
        aliases = [canonical.lower(), *GAZETTEER[canonical]]
        if any(_word_in(text_l, a) for a in aliases if len(a) > 3 or not a.isalpha()):
            return True
        return any(re.search(r"\b" + re.escape(a.upper()) + r"\b", text) for a in aliases if len(a) <= 3 and a.isalpha())
    head = canonical.split(",")[0].strip()
    return bool(head) and _word_in(text_l, head)


def _date_set(text: str, ref: date) -> set[date]:
    out: set[date] = set()
    for m in find_dates(text, ref):
        for d in (m.exact, m.earliest, m.latest, m.second):
            if d:
                out.add(d)
    return out


def date_grounded(spec: DateSpec, text: str, ref: date) -> bool:
    known = _date_set(text, ref)
    values = [d for d in (spec.exact, spec.earliest, spec.latest) if d]
    if not values:
        return True
    if all(d in known for d in values):
        return True
    # "back on the 20th": day-only mention resolved against another date
    day_only = {int(m.group(1)) for m in re.finditer(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", text.lower())}
    return all(d in known or d.day in day_only for d in values)


def pax_grounded(value: int, text: str) -> bool:
    if value in find_passengers(text).totals:
        return True
    text_l = text.lower()
    if re.search(r"(?<![\d.,$€£])" + str(value) + r"(?![\d.,%])", text_l):
        return True
    return any(n == value and _word_in(text_l, w) for w, n in NUMBER_WORDS.items())


def budget_grounded(value: float, text: str) -> bool:
    return any(abs(m.amount - value) <= max(1.0, value * 0.005) for m in find_money(text))


def grounding_report(ex: InquiryExtraction, text: str, ref: date) -> dict[str, bool]:
    """Map each non-null extracted field (services individually) → whether it is supported by ``text``."""
    report: dict[str, bool] = {}
    if ex.origin:
        report["origin"] = location_grounded(ex.origin, text)
    if ex.destination:
        report["destination"] = location_grounded(ex.destination, text)
    if not ex.departure.is_empty:
        report["departure"] = date_grounded(ex.departure, text, ref)
    if not ex.return_date.is_empty:
        report["return_date"] = date_grounded(ex.return_date, text, ref)
    if ex.passenger_count is not None:
        report["passenger_count"] = pax_grounded(ex.passenger_count, text)
    if ex.budget_amount is not None:
        report["budget_amount"] = budget_grounded(ex.budget_amount, text)
    if ex.currency:
        currencies = set(find_currency_words(text)) | {m.currency for m in find_money(text) if m.currency}
        report["currency"] = ex.currency in currencies
    if ex.trip_purpose:
        report["trip_purpose"] = ex.trip_purpose in find_purposes(text)
    if ex.date_flexibility:
        pattern = FLEX_FIXED if ex.date_flexibility == "fixed" else FLEX_FLEXIBLE
        report["date_flexibility"] = bool(pattern.search(text))
    if ex.trip_type:
        if ex.trip_type == "one_way":
            report["trip_type"] = bool(ONE_WAY_RE.search(text))
        elif ex.trip_type == "round_trip":
            report["trip_type"] = bool(ROUND_TRIP_RE.search(text)) or not ex.return_date.is_empty
        else:
            report["trip_type"] = bool(MULTI_LEG_RE.search(text)) or len({m.canonical for m in find_mentions(text)}) >= 3
    services_in_text = find_services(text)
    for svc in ex.requested_services:
        report[f"service:{svc}"] = svc in services_in_text
    text_l = text.lower()
    if ex.customer_name:
        report["customer_name"] = all(_word_in(text_l, part) for part in ex.customer_name.split())
    if ex.contact_email:
        report["contact_email"] = ex.contact_email.lower() in text_l
    if ex.contact_phone:
        digits = re.sub(r"\D", "", ex.contact_phone)
        report["contact_phone"] = len(digits) >= 7 and digits in re.sub(r"\D", "", text)
    if ex.departure_time_preference:
        report["departure_time_preference"] = bool(TIME_OF_DAY_RE.search(text) or CLOCK_RE.search(text) or CLOCK_24_RE.search(text))
    for i, req in enumerate(ex.special_requirements):
        words = [w for w in re.findall(r"[a-z]{4,}", req.lower()) if w not in {"travelling", "with", "requirements", "board"}]
        report[f"requirement:{i}"] = req in find_special_requirements(text) or any(_word_in(text_l, w) or w[:-1] in text_l for w in words)
    return report


# Fields shown to the advisor with a stated / inferred / missing marker.
PROVENANCE_FIELDS = {
    "customer_name": "customer_name", "contact": None, "origin": "origin", "destination": "destination",
    "departure": "departure", "departure_time_preference": "departure_time_preference", "return_date": "return_date",
    "passenger_count": "passenger_count", "trip_type": "trip_type", "special_requirements": None,
}


def compute_provenance(ex: InquiryExtraction) -> dict[str, str]:
    """stated = value present and found in the message; inferred = present but interpreted (not found verbatim, or the
    model said it inferred it); missing = no value. Inferred values must be checked by the advisor."""
    g = ex.grounding
    inferred_by_model = set(ex.model_inferred_fields)
    out: dict[str, str] = {}

    def classify(present: bool, keys: list[str], name: str) -> str:
        if not present:
            return "missing"
        if name in inferred_by_model or any(not g.get(k, True) for k in keys):
            return "inferred"
        return "stated"

    out["customer_name"] = classify(bool(ex.customer_name), ["customer_name"], "customer_name")
    out["contact"] = classify(bool(ex.contact_email or ex.contact_phone), ["contact_email", "contact_phone"], "contact")
    for f in ("origin", "destination", "passenger_count", "trip_type", "departure_time_preference"):
        out[f] = classify(getattr(ex, f) is not None, [f], f)
    out["departure"] = classify(not ex.departure.is_empty, ["departure"], "departure")
    if ex.departure.exact is None and not ex.departure.is_empty and out["departure"] == "stated":
        out["departure"] = "inferred"  # a window ("in December") is an interpretation, not a date
    out["return_date"] = classify(not ex.return_date.is_empty, ["return_date"], "return_date")
    req_keys = [k for k in g if k.startswith("requirement:")]
    out["special_requirements"] = classify(bool(ex.special_requirements), req_keys, "special_requirements")
    return out


# ---------------------------------------------------------------------------
# Missing information
# ---------------------------------------------------------------------------
def compute_missing_information(ex: InquiryExtraction) -> list[MissingItem]:
    """Required information for an advisor to request quotes. Deterministic business rules."""
    missing: list[str] = []
    ambiguous_fields = {i.field for i in ex.ambiguities if i.code == "AMBIGUOUS_LOCATION"}
    if not ex.origin or "origin" in ambiguous_fields:
        missing.append("origin")
    if not ex.destination or "destination" in ambiguous_fields:
        missing.append("destination")
    if ex.departure.is_empty:
        missing.append("departure_date")
    elif ex.departure.exact is None:
        missing.append("exact_departure_date")
    if ex.passenger_count is None:
        missing.append("passenger_count")
    if ex.trip_type is None:
        missing.append("trip_type")
    if ex.trip_type == "round_trip" and ex.return_date.is_empty:
        missing.append("return_date")
    if ex.budget_amount is not None and not ex.currency:
        missing.append("currency")
    items = []
    for f in missing:
        q = MISSING_QUESTIONS[f]
        if f in ambiguous_fields:
            value = ex.origin if f == "origin" else ex.destination
            q = f"Which {value} do you mean? Please confirm the {f}."
        items.append(MissingItem(field=f, question=q))
    return items


# ---------------------------------------------------------------------------
# Post-processing pipeline
# ---------------------------------------------------------------------------
def post_process(ex: InquiryExtraction, text: str, reference_date: date) -> InquiryExtraction:
    """Normalise, validate and annotate an extraction. Returns a new object; input is not mutated."""
    ex = ex.model_copy(deep=True)
    issues: list[ValidationIssue] = list(ex.validation_issues)
    ambiguities: list[ValidationIssue] = list(ex.ambiguities)
    inconsistencies: list[ValidationIssue] = list(ex.inconsistencies)

    # --- normalisation -----------------------------------------------------
    ex.origin = canonicalize(ex.origin)
    ex.destination = canonicalize(ex.destination)
    allowed_services = {s.value for s in REQUESTABLE_SERVICES}
    ex.requested_services = [s for s in dict.fromkeys(ex.requested_services) if s in allowed_services]
    if ex.currency:
        ex.currency = ex.currency.strip().upper()
        if ex.currency not in SUPPORTED_CURRENCIES:
            issues.append(_issue("CURRENCY_UNSUPPORTED", "warning", "currency", f"Currency '{ex.currency}' is not supported; it was cleared."))
            ex.currency = None

    # --- locations -----------------------------------------------------------
    for mention in find_mentions(text):
        if mention.ambiguous:
            field = None
            for fname in ("origin", "destination"):
                val = getattr(ex, fname)
                if val and (is_ambiguous(val) or val.split(",")[0].strip().lower() == mention.surface.lower()):
                    field = fname
            if not any(a.code == "AMBIGUOUS_LOCATION" and a.field == field for a in ambiguities):
                ambiguities.append(_issue("AMBIGUOUS_LOCATION", "warning", field,
                                          f"'{mention.surface}' could refer to more than one place."))
    for fname in ("origin", "destination"):
        val = getattr(ex, fname)
        if val and is_ambiguous(val) and not any(a.code == "AMBIGUOUS_LOCATION" and a.field == fname for a in ambiguities):
            ambiguities.append(_issue("AMBIGUOUS_LOCATION", "warning", fname, f"'{val}' could refer to more than one place."))
    if ex.origin and ex.destination and ex.origin.lower() == ex.destination.lower():
        issues.append(_issue("SAME_ORIGIN_DESTINATION", "error", "destination", "Origin and destination are the same."))

    # --- dates ----------------------------------------------------------------
    for date_mention in find_dates(text, reference_date):
        if date_mention.ambiguous == "invalid calendar date":
            if not any(i.code == "DATE_INVALID" for i in issues):
                issues.append(_issue("DATE_INVALID", "error", "departure", f"'{date_mention.text}' is not a valid calendar date."))
            continue
        if date_mention.ambiguous and not date_mention.ambiguous.startswith("day-only") and not any(
            a.code == "AMBIGUOUS_DATE" for a in ambiguities + issues
        ):
            ambiguities.append(_issue("AMBIGUOUS_DATE", "warning", "departure_date",
                                      f"'{date_mention.text}': {date_mention.ambiguous}."))
    # Rule-based extractor may have put AMBIGUOUS_DATE into validation_issues; keep them under ambiguities.
    for i in [i for i in issues if i.code == "AMBIGUOUS_DATE"]:
        issues.remove(i)
        if not any(a.code == "AMBIGUOUS_DATE" for a in ambiguities):
            ambiguities.append(i)
    for name, spec in (("departure", ex.departure), ("return_date", ex.return_date)):
        if spec.earliest and spec.latest and spec.latest < spec.earliest:
            issues.append(_issue("DATE_WINDOW_INVERTED", "error", name, f"The {name} window ends before it starts."))
        if spec.earliest and spec.latest and spec.earliest == spec.latest and spec.exact is None:
            spec.exact, spec.earliest, spec.latest = spec.earliest, None, None
    dep, ret = ex.departure.anchor(), ex.return_date.anchor()
    if dep and dep < reference_date:
        issues.append(_issue("DATE_IN_PAST", "warning", "departure", f"Departure {dep.isoformat()} is before {reference_date.isoformat()}."))
    if dep and dep > reference_date + timedelta(days=FAR_FUTURE_DAYS):
        issues.append(_issue("DATE_FAR_FUTURE", "warning", "departure", "Departure is more than two years away; please confirm."))
    if dep and ret and ret < dep:
        inconsistencies.append(_issue("RETURN_BEFORE_DEPARTURE", "error", "return_date", "Return date is before the departure date."))
    if ex.trip_type == "one_way" and not ex.return_date.is_empty:
        inconsistencies.append(_issue("ONE_WAY_WITH_RETURN", "warning", "trip_type", "Trip described as one-way but a return date was found."))

    # --- passengers ---------------------------------------------------------
    text_totals = find_passengers(text).totals
    mentioned = list(dict.fromkeys([*ex.passenger_counts_mentioned, *text_totals]))
    if len(set(text_totals)) > 1:
        inconsistencies.append(_issue("PASSENGER_COUNT_CONFLICT", "warning", "passenger_count",
                                      f"Different passenger counts are mentioned ({', '.join(map(str, text_totals))}); please confirm."))
        ex.passenger_count = None
    ex.passenger_counts_mentioned = mentioned
    if ex.passenger_count is not None:
        if ex.passenger_count < 1 or ex.passenger_count > MAX_PAX:
            issues.append(_issue("PASSENGER_COUNT_INVALID", "error", "passenger_count", f"Passenger count {ex.passenger_count} is not valid."))
            ex.passenger_count = None
        elif ex.passenger_count > MAX_TYPICAL_PRIVATE_JET_PAX:
            issues.append(_issue("PASSENGER_COUNT_LARGE", "warning", "passenger_count",
                                 f"{ex.passenger_count} passengers exceeds typical private-jet capacity; confirm group size."))
    elif any(n < 1 for n in text_totals):
        issues.append(_issue("PASSENGER_COUNT_INVALID", "error", "passenger_count", "A passenger count below one was stated."))

    # --- budget ---------------------------------------------------------------
    if ex.budget_amount is not None and ex.budget_amount <= 0:
        issues.append(_issue("BUDGET_INVALID", "error", "budget_amount", "Budget must be greater than zero."))
        ex.budget_amount = None
    if ex.budget_amount is not None and not ex.currency:
        issues.append(_issue("BUDGET_WITHOUT_CURRENCY", "warning", "currency", "A budget was given without a currency."))
    money_amounts = {m.amount for m in find_money(text) if not m.is_range_upper}
    has_range = any(m.is_range_upper for m in find_money(text))
    if len(money_amounts) > 1 and not has_range and not any(i.code == "BUDGET_CONFLICT" for i in issues):
        issues.append(_issue("BUDGET_CONFLICT", "warning", "budget_amount", "Several different amounts are mentioned; the budget is unclear."))
    if any(i.code == "BUDGET_CONFLICT" for i in issues):
        inconsistencies.extend(i for i in issues if i.code == "BUDGET_CONFLICT")
        issues = [i for i in issues if i.code != "BUDGET_CONFLICT"]
        ex.budget_amount = None

    # --- prompt injection signals -------------------------------------------
    signals = detect_prompt_injection(text)
    model_flag = "model_flagged" in ex.injection_signals
    ex.injection_signals = [*signals, *(["model_flagged"] if model_flag else [])]
    if ex.injection_signals:
        issues.append(_issue("PROMPT_INJECTION_SUSPECTED", "warning", None,
                             "The inquiry contains text that looks like instructions to the system "
                             f"({', '.join(ex.injection_signals)}). It was treated as data only."))

    # --- grounding --------------------------------------------------------------
    ex.grounding = grounding_report(ex, text, reference_date)
    for field, ok in ex.grounding.items():
        if not ok:
            issues.append(_issue("UNGROUNDED_FIELD", "warning", field,
                                 f"'{field}' could not be matched to the inquiry text; verify before saving."))

    ex.validation_issues = issues
    ex.ambiguities = ambiguities
    ex.inconsistencies = inconsistencies
    ex.missing_information = compute_missing_information(ex)
    ex.provenance = compute_provenance(ex)
    return ex
