"""Airport and city suggestions for the From / To fields.

The list is bundled (``src/reference/airports.csv``, built by ``scripts/build_places.py`` from the public-domain
OurAirports data): large and medium airports with an IATA code. Lookups are local — instant, free, no API key, and what
a customer types is never sent to a third party.

The value saved on an inquiry is short ("London Luton Airport (LTN)"); the suggestion shown while typing is fuller
("London Luton Airport (LTN) · Luton, United Kingdom") so people can search by airport, city, country or code.
Anything not in the list can still be typed and saved as written.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.ai.locations import is_ambiguous

DATA_FILE = Path(__file__).resolve().parent / "reference" / "airports.csv"


@dataclass(frozen=True)
class Place:
    iata: str
    icao: str
    name: str
    city: str
    country: str
    major: bool  # large airport
    lat: float
    lon: float

    @property
    def value(self) -> str:
        """What gets saved on the inquiry."""
        return f"{self.name} ({self.iata})"

    @property
    def label(self) -> str:
        """What the suggestion list shows (searchable by name, code, city and country)."""
        where = ", ".join(x for x in (self.city, self.country) if x)
        return f"{self.value} · {where}" if where else self.value


@lru_cache(maxsize=1)
def all_places() -> tuple[Place, ...]:
    if not DATA_FILE.exists():
        return ()
    with DATA_FILE.open(encoding="utf-8", newline="") as f:
        return tuple(Place(r["iata"], r["icao"], r["name"], r["city"], r["country"], r["rank"] == "0",
                           float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f))


@lru_cache(maxsize=1)
def place_values() -> tuple[str, ...]:
    return tuple(p.value for p in all_places())


@lru_cache(maxsize=1)
def labels_by_value() -> dict[str, str]:
    return {p.value: p.label for p in all_places()}


def label_for(value: str) -> str:
    """Suggestion label for a saved value; free text is shown as typed."""
    return labels_by_value().get(value, value)


@lru_cache(maxsize=1)
def _by_code() -> dict[str, Place]:
    out: dict[str, Place] = {}
    for p in all_places():
        out[p.iata] = p
        if p.icao:
            out.setdefault(p.icao, p)
    return out


def search_places(query: str, limit: int = 8) -> list[Place]:
    """Best matches for a typed query: exact code first, then name/city starts-with, then contains; big airports first."""
    q = " ".join((query or "").lower().split())
    if len(q) < 2:
        return []
    exact = _by_code().get(q.upper())
    scored: list[tuple[int, int, str, Place]] = []
    for p in all_places():
        if p is exact:
            continue
        name, city, country = p.name.lower(), p.city.lower(), p.country.lower()
        if city == q or name.startswith(q):
            score = 1
        elif city.startswith(q) or any(w.startswith(q) for w in name.split()):
            score = 2
        elif q in name or q in city or q in country:
            score = 3
        else:
            continue
        scored.append((score, 0 if p.major else 1, p.name, p))
    scored.sort(key=lambda t: t[:3])
    return ([exact] if exact else []) + [t[3] for t in scored][: limit - (1 if exact else 0)]


# Well-known charter destinations usually named by the area rather than the airport.
DESTINATION_ALIASES = {
    "st. barts": "SBH", "st barts": "SBH", "st. barths": "SBH", "st barths": "SBH", "saint barthélemy": "SBH",
    "vail": "EGE", "beaver creek": "EGE", "sun valley": "SUN", "turks and caicos": "PLS", "providenciales": "PLS",
    "cabo": "SJD", "los cabos": "SJD", "cabo san lucas": "SJD", "the hamptons": "HTO", "hamptons": "HTO",
    "courchevel": "GVA", "st. moritz": "SMV", "st moritz": "SMV", "lake tahoe": "TVL", "big sky": "BZN",
}


@lru_cache(maxsize=1)
def _by_value() -> dict[str, Place]:
    return {p.value: p for p in all_places()}


_CODE_RE = re.compile(r"\(([A-Z]{3})\)\s*$")


def resolve_place(text: str | None) -> Place | None:
    """Best-effort location for a saved From/To value, used only for maps and distances.
    Exact saved value or code first; otherwise a city name that matches exactly (its largest airport)."""
    if not text:
        return None
    t = text.strip()
    by_value = _by_value()
    if t in by_value:
        return by_value[t]
    m = _CODE_RE.search(t)
    if m and m.group(1) in _by_code():
        return _by_code()[m.group(1)]
    if t.upper() in _by_code():
        return _by_code()[t.upper()]
    city = t.split(",")[0].strip().lower()
    if is_ambiguous(t) or is_ambiguous(city):  # e.g. "Portland" — never guess on a map
        return None
    if city in DESTINATION_ALIASES and DESTINATION_ALIASES[city] in _by_code():
        return _by_code()[DESTINATION_ALIASES[city]]
    matches = [p for p in all_places() if p.city.lower() == city]
    if matches:
        return sorted(matches, key=lambda p: (not p.major, p.name))[0]
    named = [p for p in all_places() if p.name.lower().startswith(city + " ") or p.name.lower() == city]
    return sorted(named, key=lambda p: (not p.major, p.name))[0] if named else None


def distance_km(a: Place, b: Place) -> float:
    """Great-circle distance between two airports."""
    r = 6371.0088
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
