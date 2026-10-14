"""Deterministic text-parsing primitives shared by the rule-based baseline and the grounding checks.

Conventions (documented in README → "Extraction conventions"):
* Dates without a year resolve to the next occurrence on/after the reference date.
* "in December" → window 1–31 Dec; "early" = days 1–10, "mid" = 11–20, "late"/"end of" = 21–end.
* Numeric dates like 03/04 where both parts ≤ 12 are *ambiguous* and left unresolved.
* "$" is interpreted as USD; "C$"/"CA$" → CAD, "A$"/"AU$" → AUD.
* Passenger counts expressed as components ("4 adults and 2 kids") are summed.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MONTHS: dict[str, int] = {}
for _i, _name in enumerate(calendar.month_name):
    if _name:
        MONTHS[_name.lower()] = _i
        MONTHS[_name[:3].lower()] = _i
MONTHS["sept"] = 9
_MONTH_RE = r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b\.?"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "a dozen": 12, "dozen": 12,
    "half a dozen": 6, "a couple of": 2, "couple of": 2,
}
_NUM_RE = r"(\d{1,3}|half a dozen|a dozen|dozen|twenty|nineteen|eighteen|seventeen|sixteen|fifteen|fourteen|thirteen|twelve|eleven|ten|nine|eight|seven|six|five|four|three|two|one)"
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}


def to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
@dataclass
class DateMention:
    start: int
    end: int
    exact: date | None = None
    earliest: date | None = None
    latest: date | None = None
    text: str = ""
    ambiguous: str | None = None  # reason if ambiguous
    second: date | None = None  # end of "Dec 12-15" style ranges


def _next_occurrence(month: int, day: int, ref: date) -> date | None:
    for year in (ref.year, ref.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= ref:
            return d
    return None


def _month_window(month: int, ref: date, part: str | None = None) -> tuple[date, date]:
    year = ref.year if month >= ref.month else ref.year + 1
    last = calendar.monthrange(year, month)[1]
    lo, hi = 1, last
    if part in ("early", "beginning of", "start of", "first week of"):
        lo, hi = 1, 7 if part == "first week of" else 10
    elif part in ("mid", "mid-", "middle of", "mid "):
        lo, hi = 11, 20
    elif part in ("late", "end of", "last week of"):
        lo, hi = (last - 6, last) if part == "last week of" else (21, last)
    return date(year, month, lo), date(year, month, hi)


def _thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(weeks=3)


def _holiday(name: str, ref: date) -> date | None:
    fixed = {"christmas eve": (12, 24), "christmas day": (12, 25), "christmas": (12, 25), "new year's eve": (12, 31),
             "new years eve": (12, 31), "new year's day": (1, 1), "new years day": (1, 1), "july 4th": (7, 4),
             "fourth of july": (7, 4), "4th of july": (7, 4), "valentine's day": (2, 14)}
    if name in fixed:
        return _next_occurrence(*fixed[name], ref)
    if name == "thanksgiving":
        d = _thanksgiving(ref.year)
        return d if d >= ref else _thanksgiving(ref.year + 1)
    return None


def find_dates(text: str, ref: date) -> list[DateMention]:
    """Find date mentions in order. Overlapping matches keep the earliest-starting, longest one."""
    t = text.lower()
    found: list[DateMention] = []

    def add(m: re.Match[str], **kw: object) -> None:
        found.append(DateMention(start=m.start(), end=m.end(), text=text[m.start():m.end()], **kw))  # type: ignore[arg-type]

    # ISO 2026-12-15
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            add(m, exact=date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            add(m, ambiguous="invalid calendar date")
    # Numeric 12/15[/2026] or 15.12.2026 (dots only with a year, so prices/decimals are not dates)
    numeric = list(re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t))
    numeric += list(re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t))
    for m in numeric:
        a, b = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if a <= 12 and b <= 12 and a != b:
            add(m, ambiguous="numeric date could be month/day or day/month")
            continue
        month, day = (a, b) if a <= 12 else (b, a)
        try:
            if year:
                y = int(year) + (2000 if len(year) == 2 else 0)
                add(m, exact=date(y, month, day))
            else:
                d = _next_occurrence(month, day, ref)
                add(m, exact=d) if d else add(m, ambiguous="invalid calendar date")
        except ValueError:
            add(m, ambiguous="invalid calendar date")
    # Month D1-D2 / Month D1 to D2 (range)
    for m in re.finditer(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to|through|thru|until)\s*(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4}))?", t):
        month = MONTHS[m.group(1)]
        d1, d2 = int(m.group(2)), int(m.group(3))
        start = _resolve(month, d1, m.group(4), ref)
        end = _resolve(month, d2, m.group(4), ref) if start else None
        if start and end and end < start:
            end = None
        if start:
            add(m, exact=start, second=end)
    # Month D[, YYYY]
    for m in re.finditer(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4}))?", t):
        d = _resolve(MONTHS[m.group(1)], int(m.group(2)), m.group(3), ref)
        add(m, exact=d) if d else add(m, ambiguous="invalid calendar date")
    # D Month / Dth of Month [YYYY]
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"(?:,?\s*(\d{4}))?", t):
        d = _resolve(MONTHS[m.group(2)], int(m.group(1)), m.group(3), ref)
        add(m, exact=d) if d else add(m, ambiguous="invalid calendar date")
    # Month windows: "in December", "early January", "mid-March", "end of May", "first week of June"
    for m in re.finditer(r"(\bearly|\blate|\bmid-|\bmid |\bmiddle of|\bend of|\bbeginning of|\bstart of|\bfirst week of|\blast week of)?\s*" + _MONTH_RE + r"(?!\s*\d{1,2}(?!\d))(?:,?\s*(\d{4})\b)?", t):
        month = MONTHS[m.group(2)]
        if m.group(2) == "may" and not m.group(1) and not re.search(r"\b(in|during|of|for|by|until|around)\s+$", t[max(0, m.start() - 10):m.start()]):
            continue  # "may" as a modal verb
        part = (m.group(1) or "").strip() or None
        anchor = ref if not m.group(3) else date(int(m.group(3)), 1, 1)  # explicit year wins
        lo, hi = _month_window(month, anchor, part if part != "mid-" else "mid")
        found.append(DateMention(start=m.start() + (0 if m.group(1) else len(m.group(0)) - len(m.group(0).lstrip())),
                                 end=m.end(), earliest=lo, latest=hi, text=text[m.start():m.end()].strip()))
    # Holidays
    for m in re.finditer(r"\b(christmas eve|christmas day|christmas|new year'?s eve|new year'?s day|thanksgiving|july 4th|fourth of july|4th of july)\b", t):
        d = _holiday(m.group(1).replace("new years", "new year's") if "new years" in m.group(1) else m.group(1), ref)
        if d:
            add(m, exact=d)
    # Relative
    for m in re.finditer(r"\b(tomorrow|today|tonight|day after tomorrow)\b", t):
        offset = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}[m.group(1)]
        add(m, exact=ref + timedelta(days=offset))
    for m in re.finditer(r"\b(this|next|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t):
        target = WEEKDAYS[m.group(2)]
        delta = (target - ref.weekday()) % 7
        if m.group(1) == "next" and delta == 0:
            delta = 7
        if delta == 0:
            delta = 7 if m.group(1) == "coming" else 0
        add(m, exact=ref + timedelta(days=delta))
    for m in re.finditer(r"\b(this weekend|next weekend|next week|next month)\b", t):
        phrase = m.group(1)
        if phrase == "next weekend":
            add(m, ambiguous="'next weekend' can mean the coming weekend or the one after")
        elif phrase == "this weekend":
            sat = ref + timedelta(days=(5 - ref.weekday()) % 7)
            add(m, earliest=sat, latest=sat + timedelta(days=1))
        elif phrase == "next week":
            mon = ref + timedelta(days=7 - ref.weekday())
            add(m, earliest=mon, latest=mon + timedelta(days=6))
        else:
            month = ref.month % 12 + 1
            year = ref.year + (1 if ref.month == 12 else 0)
            add(m, earliest=date(year, month, 1), latest=date(year, month, calendar.monthrange(year, month)[1]))
    # "the 20th" (day only) — resolved later relative to another date
    for m in re.finditer(r"\bthe\s+(\d{1,2})(st|nd|rd|th)\b", t):
        found.append(DateMention(start=m.start(), end=m.end(), text=text[m.start():m.end()], ambiguous="day-only:" + m.group(1)))

    found.sort(key=lambda dm: (dm.start, -(dm.end - dm.start)))
    result: list[DateMention] = []
    last_end = -1
    for mention in found:
        if mention.start < last_end:
            continue
        result.append(mention)
        last_end = mention.end
    return result


def _resolve(month: int, day: int, year: str | None, ref: date) -> date | None:
    try:
        if year:
            return date(int(year), month, day)
        return _next_occurrence(month, day, ref)
    except ValueError:
        return None


def resolve_day_only(day: int, anchor: date) -> date | None:
    """'the 20th' after an anchor date → that day in the anchor's month (or the following month)."""
    for months_ahead in (0, 1):
        y = anchor.year + (anchor.month + months_ahead - 1) // 12
        mth = (anchor.month + months_ahead - 1) % 12 + 1
        try:
            d = date(y, mth, day)
        except ValueError:
            continue
        if d >= anchor:
            return d
    return None


def parse_iso_date(value: str | None) -> date | None:
    """Strict YYYY-MM-DD parse. Raises ValueError on malformed input (caller records an issue)."""
    if value is None or str(value).strip() == "":
        return None
    return date.fromisoformat(str(value).strip()[:10])


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
CURRENCY_WORDS = {
    "usd": "USD", "dollars": "USD", "us dollars": "USD", "bucks": "USD", "eur": "EUR", "euros": "EUR", "euro": "EUR",
    "gbp": "GBP", "pounds": "GBP", "sterling": "GBP", "chf": "CHF", "swiss francs": "CHF", "francs": "CHF",
    "cad": "CAD", "canadian dollars": "CAD", "aud": "AUD", "australian dollars": "AUD", "aed": "AED", "dirhams": "AED",
    "sgd": "SGD", "jpy": "JPY", "yen": "JPY",
}
SYMBOLS = {"us$": "USD", "c$": "CAD", "ca$": "CAD", "a$": "AUD", "au$": "AUD", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_AMOUNT = r"(\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?)\s*(k|m|mm|thousand|million|grand)?\b"
_CODES = "usd|eur|gbp|chf|cad|aud|aed|sgd|jpy"
_WORDS = "us dollars|canadian dollars|australian dollars|swiss francs|dollars|euros|euro|pounds|sterling|francs|dirhams|yen|bucks"


@dataclass
class MoneyMention:
    amount: float
    currency: str | None
    start: int
    end: int
    is_range_upper: bool = False


def _to_amount(num: str, mult: str | None) -> float:
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", num):  # European thousands "30.000"
        value = float(num.replace(".", ""))
    else:
        value = float(num.replace(",", ""))
    factor = {"k": 1e3, "thousand": 1e3, "grand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6}.get((mult or "").lower(), 1)
    return round(value * factor, 2)


def find_money(text: str) -> list[MoneyMention]:
    t = text.lower()
    out: list[MoneyMention] = []
    sym = r"(us\$|ca\$|au\$|c\$|a\$|\$|€|£|¥)"
    for m in re.finditer(sym + r"\s?" + _AMOUNT, t):
        out.append(MoneyMention(_to_amount(m.group(2), m.group(3)), SYMBOLS[m.group(1)], m.start(), m.end()))
    for m in re.finditer(r"\b(" + _CODES + r")\s?" + _AMOUNT, t):
        out.append(MoneyMention(_to_amount(m.group(2), m.group(3)), m.group(1).upper(), m.start(), m.end()))
    for m in re.finditer(r"(?<![\$€£¥\w.,])" + _AMOUNT + r"\s*(" + _CODES + "|" + _WORDS + r")\b", t):
        out.append(MoneyMention(_to_amount(m.group(1), m.group(2)), CURRENCY_WORDS[m.group(3)], m.start(), m.end()))
    # Bare "budget of 40k" / "budget is around 50,000"
    for m in re.finditer(r"\b(budget|spend|up to|max(?:imum)?|no more than|under|ceiling)\b[^\d$€£¥]{0,20}" + _AMOUNT, t):
        if m.group(3) or "," in m.group(2) or len(m.group(2).replace(".", "")) >= 4:
            start = m.start(2)
            if not any(o.start <= start < o.end or (start <= o.start < m.end()) for o in out):
                out.append(MoneyMention(_to_amount(m.group(2), m.group(3)), None, start, m.end()))
    out.sort(key=lambda x: x.start)
    dedup: list[MoneyMention] = []
    for mm in out:
        if dedup and mm.start < dedup[-1].end:
            continue
        dedup.append(mm)
    # Ranges "$30k-$40k", "between 30k and 40k"
    for a, b in zip(dedup, dedup[1:], strict=False):
        between = t[a.end:b.start]
        if re.fullmatch(r"\s*(-|–|to|and)\s*", between):
            b.is_range_upper = True
            if b.currency is None:
                b.currency = a.currency
            if a.currency is None:
                a.currency = b.currency
    return dedup


def find_currency_words(text: str) -> list[str]:
    t = text.lower()
    codes = [CURRENCY_WORDS[m.group(1)] for m in re.finditer(r"\b(" + _CODES + "|" + _WORDS + r")\b", t)]
    codes += [SYMBOLS[m.group(1)] for m in re.finditer(r"(us\$|ca\$|au\$|c\$|a\$|€|£|¥)", t)]
    return list(dict.fromkeys(codes))


# ---------------------------------------------------------------------------
# Passengers
# ---------------------------------------------------------------------------
_PAX_NOUNS = r"(passengers?|pax|people|persons|guests|travell?ers|adults|ppl|executives|of us|seats|family members)"
_COMPONENT_NOUNS = r"(adults?|child(?:ren)?|kids?|infants?|babies|teenagers|teens|toddlers?)"
SOLO_RE = re.compile(r"\b(just me|only me|myself only|travell?ing alone|travell?ing solo|solo trip|by myself)\b", re.I)
COUPLE_RE = re.compile(r"\b(me and my (wife|husband|partner|girlfriend|boyfriend|fianc[ée]e?|son|daughter|mother|father|mom|dad|colleague|friend|assistant)|my (wife|husband|partner|fianc[ée]e?) and (i|me))\b", re.I)


@dataclass
class PaxFindings:
    totals: list[int] = field(default_factory=list)  # distinct explicit totals, in order
    component_sum: int | None = None


def find_passengers(text: str) -> PaxFindings:
    t = text.lower()
    totals: list[int] = []
    comp_spans: list[tuple[int, int]] = []
    comp_total = 0
    comps = list(re.finditer(r"\b" + _NUM_RE + r"\s+" + _COMPONENT_NOUNS + r"\b", t))
    if len(comps) >= 2:
        for m in comps:
            n = to_int(m.group(1))
            if n:
                comp_total += n
                comp_spans.append((m.start(), m.end()))
    for m in re.finditer(r"(?<![/\d.,$€£-])\b" + _NUM_RE + r"\s+(?:[a-z]+\s+)?" + _PAX_NOUNS + r"\b", t):
        if any(s <= m.start() < e for s, e in comp_spans):
            continue
        n = to_int(m.group(1))
        if n is not None and not (m.group(2) == "seats" and n > 100):
            totals.append(n)
    for m in re.finditer(r"\b(?:we(?:'re| are)|there(?:'s| is| are| will be| 'll be)|we'll be|we will be)\s+" + _NUM_RE + r"\b(?!\s*(?:days?|nights?|weeks?|hours?|minutes?|years?|%))", t):
        n = to_int(m.group(1))
        if n is not None:
            totals.append(n)
    for m in re.finditer(r"\b(party|group|family|team) of\s+" + _NUM_RE + r"\b", t):
        n = to_int(m.group(2))
        if n is not None:
            totals.append(n)
    for m in re.finditer(r"\b(?:for|seats for|seating for)\s+" + _NUM_RE + r"\b(?!\s*(?:days?|nights?|weeks?|hours?|am|pm|k\b|thousand|%|st|nd|rd|th))", t):
        n = to_int(m.group(1))
        nxt = t[m.end():m.end() + 12]
        if n is not None and not re.match(r"\s*(" + _PAX_NOUNS + "|" + _COMPONENT_NOUNS + r"|of)", nxt) and n <= 60:
            totals.append(n)
    # "me and 3 friends" / "with 3 colleagues" -> sender + N
    for m in re.finditer(r"\b(?:(?:me|i) and|with)\s+(?:my\s+)?" + _NUM_RE + r"\s+(friends|colleagues|others|clients)\b", t):
        n = to_int(m.group(1))
        if n is not None:
            totals.append(n + 1)
    if comp_total:
        totals.append(comp_total)
    if SOLO_RE.search(text):
        totals.append(1)
    if COUPLE_RE.search(text):
        totals.append(2)
    distinct = list(dict.fromkeys(totals))
    return PaxFindings(totals=distinct, component_sum=comp_total or None)


# ---------------------------------------------------------------------------
# Lexicons: services, purpose, flexibility, trip type
# ---------------------------------------------------------------------------
SERVICE_LEXICON: dict[str, list[str]] = {
    "accommodation": [r"hotels?", r"villas?", r"chalets?", r"lodging", r"accommodations?", r"place to stay",
                      r"somewhere to stay", r"airbnb", r"hotel rooms?", r"suites?", r"where to stay"],
    "ground_transport": [r"car service", r"chauffeur(ed)?", r"limo(usine)?s?", r"transfers?", r"pick[- ]?ups?", r"pick us up",
                         r"drivers?", r"ground transport(ation)?", r"suvs?", r"sprinter( van)?", r"rental cars?", r"a car"],
    "catering": [r"catering", r"catered", r"meals?", r"food", r"champagne", r"snacks", r"dietary", r"vegan",
                 r"vegetarian", r"kosher", r"halal", r"gluten[- ]free", r"allerg(y|ies)", r"dinner on board", r"lunch on board",
                 r"breakfast on board", r"birthday cake"],
    "pet_travel": [r"dogs?", r"cats?", r"pets?", r"pupp(y|ies)", r"kitten"],
    "special_luggage": [r"skis", r"ski (equipment|gear|bags)", r"snowboards?", r"golf clubs", r"golf bags?", r"surfboards?",
                        r"oversized luggage", r"extra luggage", r"lots of luggage", r"bikes", r"bicycles", r"fishing (gear|rods)",
                        r"cello", r"musical instruments?", r"large bags", r"heavy luggage"],
    "experiences": [r"restaurant reservations?", r"dinner reservations?", r"tickets", r"tours?", r"yacht( charter)?",
                    r"excursions?", r"spa", r"tee times?", r"concierge", r"vip access"],
}
_NEG_BEFORE = re.compile(r"(\bno\b|\bdon'?t need\b|\bdo not need\b|\bnot need\b|\bwon'?t need\b|\bwithout\b|\bnot looking for\b|\bno need for\b|\bnot interested in\b|\bskip\b)[^.,;!?]{0,25}$")
# "we have a car" means the service is NOT needed; "we have skis" means luggage IS coming — so possession
# negates only arrangeable services.
_POSSESSION_BEFORE = re.compile(r"(\bwe have\b|\bwe've got\b|\balready have\b|\bwe own\b)[^.,;!?]{0,25}$")
_POSSESSION_NEGATES = {"accommodation", "ground_transport", "catering", "experiences"}
_NEG_AFTER = re.compile(r"^[^.;!?]{0,30}(already (booked|sorted|arranged|covered|taken care of)|is sorted|are sorted|not needed|isn'?t needed|aren'?t needed|taken care of|is covered|are covered)")


def find_services(text: str) -> dict[str, list[str]]:
    """Requested services → list of supporting snippets. Negated mentions are ignored."""
    t = text.lower()
    hits: dict[str, list[str]] = {}
    for service, patterns in SERVICE_LEXICON.items():
        for pat in patterns:
            for m in re.finditer(r"\b" + pat + r"\b", t):
                before = t[max(0, m.start() - 45):m.start()]
                after = t[m.end():m.end() + 45]
                if _NEG_BEFORE.search(before) or _NEG_AFTER.search(after):
                    continue
                if service in _POSSESSION_NEGATES and _POSSESSION_BEFORE.search(before):
                    continue
                if service == "ground_transport" and pat == r"transfers?" and re.search(r"(wire|bank|money)\s+$", before):
                    continue
                if service == "pet_travel" and re.search(r"(hot)\s*$", before):
                    continue
                if service == "accommodation" and re.search(r"\b(our|my)( own)?\s*$", before):
                    continue  # "staying at our chalet" is not a request
                hits.setdefault(service, []).append(text[m.start():m.end()])
    return hits


PURPOSE_LEXICON: dict[str, list[str]] = {
    "event": [r"wedding", r"concert", r"festival", r"gala", r"super bowl", r"grand prix", r"f1", r"tournament",
              r"art basel", r"the masters", r"fashion week", r"premiere", r"film festival", r"bachelor(ette)? party", r"graduation"],
    "business": [r"business trip", r"conference", r"board meeting", r"client meeting", r"for work", r"corporate", r"investor meetings?",
                 r"site visit", r"roadshow", r"business meetings?", r"work trip", r"meetings? with (clients|investors|partners)",
                 r"offsite", r"business"],
    "family": [r"family reunion", r"visit(ing)? (my |our )?(family|parents|grandparents|grandkids|relatives)", r"family visit",
               r"see (my |our )?(family|parents|grandparents)"],
    "leisure": [r"vacation", r"holiday trip", r"getaway", r"honeymoon", r"ski trip", r"spring break", r"leisure", r"golf trip",
                r"anniversary trip", r"birthday trip", r"girls'? trip", r"guys'? trip", r"family vacation", r"skiing", r"beach trip"],
    "personal": [r"medical", r"hospital", r"treatment", r"clinic", r"funeral", r"surgery", r"doctor'?s? appointment"],
}
PURPOSE_PRIORITY = ["event", "business", "personal", "family", "leisure"]


def find_purposes(text: str) -> dict[str, list[str]]:
    t = text.lower()
    hits: dict[str, list[str]] = {}
    for purpose, patterns in PURPOSE_LEXICON.items():
        for pat in patterns:
            for m in re.finditer(r"\b" + pat + r"\b", t):
                if purpose == "business" and pat == "business" and re.search(r"(no|any|funny|monkey|family)\s+$", t[max(0, m.start() - 8):m.start()]):
                    continue
                if purpose == "business" and pat == "business" and re.match(r"\s*(class|card|hours|days?|jet)", t[m.end():m.end() + 8]):
                    continue
                hits.setdefault(purpose, []).append(text[m.start():m.end()])
    return hits


FLEX_FIXED = re.compile(r"\b(not flexible|no flexibility|inflexible|fixed dates?|dates are fixed|dates are firm|firm dates?|can'?t move|cannot move|must (depart|leave|fly|be)|has to be|have to be (on|there)|exact dates?|strict(ly)?|non-negotiable|locked in)\b", re.I)
FLEX_FLEXIBLE = re.compile(r"\b(flexible|flexibility|give or take|plus or minus|any day (that|this|next) week|dates are open|open on dates|open to (other )?dates|can move|either day|whenever|sometime|some time|we'?re easy|dates can shift|a day or two either way)\b|\+/-|±", re.I)

ROUND_TRIP_RE = re.compile(r"\b(round[- ]?trip|return (flight|leg|trip)|and back|returning|return on|return the|fly(ing)? back|coming back|come back|back on|back home|heading back|head back|both ways|return date|back the|return\b)", re.I)
ONE_WAY_RE = re.compile(r"\b(one[- ]?way|single leg|no return|not returning|won'?t need a return|one direction)\b", re.I)
MULTI_LEG_RE = re.compile(r"\b(multi[- ]?leg|multiple stops|then on to|then onward to|onward to|followed by|second leg|third leg|several stops)\b", re.I)
RETURN_CUE_RE = re.compile(r"(return(ing)?|back|coming back|fly(ing)? back|home|until|through)\W*(\w+\W+){0,3}$", re.I)
DEPART_CUE_RE = re.compile(r"(depart(ing|ure)?|leav(e|ing)|fly(ing)? out|outbound|go(ing)? out|head(ing)? out)\W*(\w+\W+){0,3}$", re.I)


# ---------------------------------------------------------------------------
# Customer / contact details, time preference, special requirements
# ---------------------------------------------------------------------------
EMAIL_FIND_RE = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_FIND_RE = re.compile(r"(?<![\w/])(\+?\d[\d ().-]{6,22}\d)(?![\w/])")
_NAME = r"([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2})"
NAME_PATTERNS = [
    re.compile(r"\b(?:[Mm]y name is|[Tt]his is|[Ii]t's|[Nn]ame:)\s+" + _NAME),
    re.compile(r"\b(?:[Tt]hanks|[Tt]hank you|[Rr]egards|[Bb]est|[Cc]heers|[Ss]incerely|[Kk]ind regards|[Bb]est regards)[,!.]?\s*[\r\n]+\s*"
               + _NAME + r"[ \t]*(?:\r?\n|$)"),
    re.compile(r"^\s*[-–—]\s*" + _NAME + r"\s*$", re.M),
]
_NOT_NAMES = {"Hi", "Hello", "Thanks", "Please", "Dear", "Team", "The", "We", "Our", "Flight", "Demo", "Regards", "Best"}


@dataclass
class ContactFindings:
    name: str | None = None
    email: str | None = None
    phone: str | None = None


def find_contact(text: str) -> ContactFindings:
    """Contact details exactly as written. Nothing is inferred (no name guessed from an email address)."""
    out = ContactFindings()
    m = EMAIL_FIND_RE.search(text)
    if m:
        out.email = m.group(0).lower()
    for pm in PHONE_FIND_RE.finditer(text):
        candidate = pm.group(1).strip()
        digits = sum(c.isdigit() for c in candidate)
        if 7 <= digits <= 15 and not re.fullmatch(r"\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?", candidate) \
                and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate) and not re.fullmatch(r"[\d,]+", candidate):
            out.phone = candidate
            break
    for pattern in NAME_PATTERNS:
        nm = pattern.search(text.strip())
        if nm and nm.group(1).split()[0] not in _NOT_NAMES:
            out.name = nm.group(1).strip()
            break
    return out


TIME_OF_DAY_RE = re.compile(r"\b(early morning|late morning|morning|midday|noon|afternoon|early evening|evening|late night|night)\b"
                            r"(?:\s+(?:departure|flight|take-?off))?", re.I)
CLOCK_RE = re.compile(r"\b(?:at|around|about|by|before|after|~)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?!\s*(?:passengers?|people|pax|%|k\b))",
                      re.I)
CLOCK_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def find_time_preference(text: str) -> str | None:
    """'morning', '09:00', 'around 14:30' … only when a departure time preference is stated."""
    for m in CLOCK_RE.finditer(text):
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        if not ampm and not m.group(2):
            continue  # "at 9" alone is too ambiguous (could be a count)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            prefix = "around " if m.group(0).lower().startswith(("around", "about", "~")) else ""
            return f"{prefix}{hour:02d}:{minute:02d}"
    m24 = CLOCK_24_RE.search(text)
    if m24:
        return f"{int(m24.group(1)):02d}:{m24.group(2)}"
    tod = TIME_OF_DAY_RE.search(text)
    if tod:
        m = tod
        before = text[max(0, m.start() - 30):m.start()].lower()
        if re.search(r"\b(good|this|yesterday|tomorrow's?)\s*$", before) and "depart" not in before:
            return None  # "good morning", "this morning I…" are not preferences
        return m.group(1).lower()
    return None


SPECIAL_REQUIREMENT_LEXICON: list[tuple[str, str]] = [
    (r"wheelchair|mobility (?:aid|assistance|scooter)|reduced mobility", "wheelchair / mobility assistance"),
    (r"oxygen|medical equipment|stretcher|medical needs?", "medical equipment (details not recorded)"),
    (r"car seats?|child seats?|infant seats?|booster seats?", "child seat"),
    (r"extra (?:baggage|luggage|bags)|lots of (?:luggage|bags)|oversized (?:luggage|baggage|bags)|heavy (?:luggage|bags)|large bags",
     "extra or oversized baggage"),
    (r"skis|ski (?:equipment|gear|bags)|snowboards?|golf clubs|golf bags?|surfboards?|bikes|bicycles|fishing (?:gear|rods)",
     "sports equipment"),
    (r"cello|musical instruments?|guitars?", "musical instrument"),
    (r"\bdogs?\b|\bcats?\b|\bpets?\b|pupp(?:y|ies)|kitten", "travelling with a pet"),
    (r"wi-?fi|internet on board", "Wi-Fi on board"),
    (r"lavatory|bathroom on board|enclosed (?:toilet|lav)", "lavatory on board"),
    (r"flight attendant|cabin attendant|cabin crew", "cabin attendant"),
    (r"vegan|vegetarian|kosher|halal|gluten[- ]free|dietary|allerg(?:y|ies)", "dietary requirements for catering"),
]


def find_special_requirements(text: str) -> list[str]:
    t = text.lower()
    found: list[str] = []
    for pattern, label in SPECIAL_REQUIREMENT_LEXICON:
        for m in re.finditer(r"\b(?:" + pattern + r")", t):
            before = t[max(0, m.start() - 40):m.start()]
            if _NEG_BEFORE.search(before) or re.search(r"hot\s*$", before):
                continue
            if label not in found:
                found.append(label)
            break
    return found
