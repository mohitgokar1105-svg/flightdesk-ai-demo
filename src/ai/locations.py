"""A small gazetteer of locations common in private-aviation inquiries.

Used for (a) the rule-based extraction baseline, (b) canonicalising model output before comparison,
and (c) deterministic ambiguity detection (e.g. an unqualified "Portland").

It is deliberately limited; places outside it are handled by capitalised-phrase fallbacks in the
rule-based extractor and by the model in live mode. This limitation is visible in evaluation results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# canonical name -> aliases (lower case). The canonical name itself is always an alias.
GAZETTEER: dict[str, list[str]] = {
    "Los Angeles": ["la", "l.a.", "lax", "los angeles", "los angles", "los angelos"],
    "Van Nuys": ["van nuys", "vny"],
    "New York": ["nyc", "new york", "new york city", "jfk", "manhattan", "ny"],
    "Teterboro": ["teterboro", "teb"],
    "White Plains": ["white plains", "westchester", "hpn"],
    "Aspen": ["aspen", "ase"],
    "Miami": ["miami", "mia"],
    "Las Vegas": ["las vegas", "vegas"],
    "San Francisco": ["san francisco", "sfo", "sf", "san fran"],
    "Chicago": ["chicago", "ord", "chi-town"],
    "Boston": ["boston", "bos", "logan"],
    "Washington, DC": ["washington dc", "washington, dc", "washington d.c.", "d.c.", "dc", "iad", "dca"],
    "Dallas": ["dallas", "dfw"],
    "Houston": ["houston", "iah"],
    "Denver": ["denver", "den"],
    "Seattle": ["seattle", "sea"],
    "Atlanta": ["atlanta", "atl"],
    "Nashville": ["nashville", "bna"],
    "Austin": ["austin", "aus"],
    "Phoenix": ["phoenix", "phx"],
    "Scottsdale": ["scottsdale"],
    "Palm Beach": ["palm beach", "west palm beach", "pbi", "west palm"],
    "Fort Lauderdale": ["fort lauderdale", "ft lauderdale", "ft. lauderdale", "fll"],
    "East Hampton": ["east hampton", "hto"],
    "The Hamptons": ["the hamptons", "hamptons"],
    "Nantucket": ["nantucket", "ack"],
    "Martha's Vineyard": ["martha's vineyard", "marthas vineyard", "mvy"],
    "Jackson Hole": ["jackson hole", "jac"],
    "Vail": ["vail", "eagle county", "ege"],
    "Telluride": ["telluride"],
    "Sun Valley": ["sun valley"],
    "Salt Lake City": ["salt lake city", "slc", "salt lake"],
    "Park City": ["park city"],
    "Cabo San Lucas": ["cabo san lucas", "cabo", "los cabos", "sjd"],
    "Cancun": ["cancun", "cancún"],
    "Mexico City": ["mexico city", "cdmx"],
    "Nassau": ["nassau"],
    "Turks and Caicos": ["turks and caicos", "turks & caicos", "providenciales", "provo"],
    "St. Barts": ["st. barts", "st barts", "st. barths", "st barths", "saint barthelemy", "sbh"],
    "Anguilla": ["anguilla"],
    "Barbados": ["barbados"],
    "Aruba": ["aruba"],
    "Toronto": ["toronto", "yyz"],
    "Montreal": ["montreal", "montréal"],
    "London": ["london", "luton", "farnborough", "heathrow", "lhr"],
    "Paris": ["paris", "le bourget", "cdg"],
    "Nice": ["nice", "nce"],
    "French Riviera": ["french riviera", "cote d'azur", "côte d'azur"],
    "Cannes": ["cannes"],
    "Monaco": ["monaco", "monte carlo"],
    "Geneva": ["geneva", "genève", "gva"],
    "Zurich": ["zurich", "zürich", "zrh"],
    "Milan": ["milan", "milano"],
    "Rome": ["rome", "roma"],
    "Ibiza": ["ibiza"],
    "Mykonos": ["mykonos"],
    "Athens": ["athens"],
    "Madrid": ["madrid"],
    "Barcelona": ["barcelona"],
    "Lisbon": ["lisbon", "lisboa"],
    "Dubai": ["dubai", "dxb"],
    "Abu Dhabi": ["abu dhabi"],
    "Doha": ["doha"],
    "Istanbul": ["istanbul"],
    "Munich": ["munich", "münchen"],
    "Vienna": ["vienna", "wien"],
    "Edinburgh": ["edinburgh"],
    "Dublin": ["dublin"],
    "Amsterdam": ["amsterdam"],
    "Olbia": ["olbia"],
    "Sardinia": ["sardinia"],
    "Malaga": ["malaga", "málaga"],
    "Marbella": ["marbella"],
    "Courchevel": ["courchevel"],
    "St. Moritz": ["st. moritz", "st moritz", "saint moritz"],
    "Gstaad": ["gstaad"],
    "Chamonix": ["chamonix"],
    "Singapore": ["singapore"],
    "Hong Kong": ["hong kong"],
    "Tokyo": ["tokyo"],
    "Sydney": ["sydney"],
    "Maldives": ["maldives", "male", "malé"],
    # Qualified forms of otherwise-ambiguous names
    "Portland, OR": ["portland, oregon", "portland oregon", "portland, or", "pdx"],
    "Portland, ME": ["portland, maine", "portland maine", "portland, me", "pwm"],
    "Naples, FL": ["naples, florida", "naples florida", "naples, fl", "naples fl", "apf"],
    "Naples, Italy": ["naples, italy", "naples italy", "napoli"],
    "San Jose, CA": ["san jose, california", "san jose california", "san jose, ca", "sjc"],
    "San Jose, Costa Rica": ["san jose, costa rica", "san jose costa rica", "san josé, costa rica"],
    "Springfield, IL": ["springfield, illinois", "springfield, il"],
    "Springfield, MO": ["springfield, missouri", "springfield, mo"],
    "Springfield, MA": ["springfield, massachusetts", "springfield, ma"],
    "Columbus, OH": ["columbus, ohio", "columbus ohio", "columbus, oh", "cmh"],
    "Birmingham, UK": ["birmingham, uk", "birmingham, england", "birmingham england"],
    "Birmingham, AL": ["birmingham, alabama", "birmingham, al"],
}

# Unqualified names that refer to multiple well-known places.
AMBIGUOUS_NAMES: dict[str, list[str]] = {
    "portland": ["Portland, OR", "Portland, ME"],
    "naples": ["Naples, FL", "Naples, Italy"],
    "san jose": ["San Jose, CA", "San Jose, Costa Rica"],
    "springfield": ["Springfield, IL", "Springfield, MO", "Springfield, MA"],
    "columbus": ["Columbus, OH", "Columbus, GA"],
    "birmingham": ["Birmingham, UK", "Birmingham, AL"],
}

# Aliases too short/common to match inside free text without strong context (only exact-field matches).
_FREE_TEXT_BLOCKLIST = {"la", "ny", "sf", "dc", "nice", "male", "chi-town", "logan", "provo"} | {
    a for aliases in GAZETTEER.values() for a in aliases if len(a) == 3 and a.isalpha()
}  # 3-letter codes: upper case only
# IATA-style codes we *do* match in free text when written in upper case.
_UPPERCASE_CODES = {a.upper() for aliases in GAZETTEER.values() for a in aliases if len(a) == 3 and a.isalpha()}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in GAZETTEER.items():
    _ALIAS_TO_CANONICAL[_canon.lower()] = _canon
    for _a in _aliases:
        _ALIAS_TO_CANONICAL[_a] = _canon
for _amb in AMBIGUOUS_NAMES:
    _ALIAS_TO_CANONICAL.setdefault(_amb, _amb.title())


@dataclass(frozen=True)
class LocationMention:
    canonical: str
    surface: str
    start: int
    end: int
    ambiguous: bool


def _norm(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s*\((?:[a-z]{3,4})\)\s*$", "", s)  # "Los Angeles (LAX)" -> "los angeles"
    s = re.sub(r"(?:,\s*|\s+)(usa|us|united states|u\.s\.a?\.?)$", "", s)
    s = re.sub(r"\s+(international\s+)?airport$", "", s)
    return re.sub(r"\s+", " ", s).strip(" .,")


def canonicalize(name: str | None) -> str | None:
    """Map an alias/variant to its canonical gazetteer name; unknown names are returned tidied."""
    if name is None:
        return None
    raw = name.strip()
    if not raw:
        return None
    key = _norm(raw)
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    # "Aspen, Colorado" -> try the part before the comma when it is unambiguous
    head = key.split(",")[0].strip()
    if head in _ALIAS_TO_CANONICAL and head not in AMBIGUOUS_NAMES:
        return _ALIAS_TO_CANONICAL[head]
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in raw.split())


def is_ambiguous(name: str | None) -> bool:
    return bool(name) and _norm(name or "") in AMBIGUOUS_NAMES


def locations_match(expected: str | None, actual: str | None) -> bool:
    """Equality after canonicalisation; for places outside the gazetteer a prefix match is accepted
    ("Bozeman" vs "Bozeman, Montana")."""
    if expected is None or actual is None:
        return expected is None and actual is None
    e, a = canonicalize(expected), canonicalize(actual)
    assert e is not None and a is not None
    if e.lower() == a.lower():
        return True
    if e in GAZETTEER or a in GAZETTEER:
        return False
    el, al = _norm(e), _norm(a)
    return al.startswith(el + ",") or el.startswith(al + ",") or al.startswith(el + " ") or el.startswith(al + " ")


def find_mentions(text: str) -> list[LocationMention]:
    """All non-overlapping gazetteer mentions in ``text`` (longest alias wins), in order of appearance."""
    lowered = text.lower()
    candidates: list[tuple[int, int, str, str]] = []
    aliases = sorted(_ALIAS_TO_CANONICAL.items(), key=lambda kv: -len(kv[0]))
    for alias, canonical in aliases:
        if alias in _FREE_TEXT_BLOCKLIST:
            continue
        for m in re.finditer(r"(?<![\w'])" + re.escape(alias) + r"(?![\w'])", lowered):
            candidates.append((m.start(), m.end(), canonical, text[m.start():m.end()]))
    for m in re.finditer(r"\b[A-Z]{3}\b", text):
        code = m.group(0)
        if code in _UPPERCASE_CODES:
            candidates.append((m.start(), m.end(), _ALIAS_TO_CANONICAL[code.lower()], code))
    # Unambiguous short forms written in capitals ("LA", "NYC", "SF", "DC").
    for m in re.finditer(r"\b(LA|L\.A\.|NY|SF|DC)\b", text):
        candidates.append((m.start(), m.end(), _ALIAS_TO_CANONICAL[m.group(0).lower()], m.group(0)))
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    chosen: list[LocationMention] = []
    last_end = -1
    for start, end, canonical, surface in candidates:
        if start < last_end:
            continue
        # "Nice" as an adjective is excluded via the blocklist; "nice" only matches as "Nice" capitalised.
        chosen.append(LocationMention(canonical, surface, start, end, _norm(surface) in AMBIGUOUS_NAMES))
        last_end = end
    for m in re.finditer(r"\bNice\b", text):  # capitalised city name, not mid-sentence adjective
        mid_sentence = m.start() > 0 and text[max(0, m.start() - 2):m.start()].strip() not in {"", ".", "!", "?"}
        if mid_sentence and not any(c.start <= m.start() < c.end for c in chosen):
            chosen.append(LocationMention("Nice", "Nice", m.start(), m.end(), False))
    return sorted(chosen, key=lambda c: c.start)
