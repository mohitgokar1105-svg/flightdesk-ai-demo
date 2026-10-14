"""Build the airport/city suggestion list used by the From/To fields.

Source: OurAirports (https://ourairports.com/data/) — airports.csv and countries.csv, released into the public domain.
Keeps large and medium airports with an IATA code, plus small ones with scheduled service or on a short list of
business-aviation fields (the airports charter customers actually name), and writes
``src/reference/airports.csv``.

Usage:
    python scripts/build_places.py                         # downloads the two files
    python scripts/build_places.py --airports a.csv --countries c.csv   # use local copies
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "reference" / "airports.csv"
BASE_URL = "https://davidmegginson.github.io/ourairports-data/"
KEEP_TYPES = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
# Small airports are kept only if they have scheduled service, or are well-known business-aviation fields.
SMALL_EXTRAS = {"HTO"}


def _read(path: str | None, name: str) -> list[dict[str, str]]:
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(BASE_URL + name, timeout=60) as resp:  # noqa: S310 - fixed https URL
            text = resp.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _city(municipality: str) -> str:
    """'Luton, Luton' -> 'Luton'; 'Nice, Alpes-Maritimes' -> 'Nice'."""
    return municipality.split(",")[0].strip() if municipality else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--airports")
    parser.add_argument("--countries")
    args = parser.parse_args()
    countries = {c["code"]: c["name"] for c in _read(args.countries, "countries.csv")}
    rows = []
    seen: set[str] = set()
    for a in _read(args.airports, "airports.csv"):
        iata = a["iata_code"].strip().upper()
        if a["type"] not in KEEP_TYPES or len(iata) != 3 or iata in seen:
            continue
        if a["type"] == "small_airport" and a["scheduled_service"] != "yes" and iata not in SMALL_EXTRAS:
            continue
        seen.add(iata)
        rows.append({"iata": iata, "icao": a["icao_code"].strip() or a["gps_code"].strip(), "name": a["name"].strip(),
                     "city": _city(a["municipality"]), "country": countries.get(a["iso_country"], a["iso_country"]),
                     "rank": KEEP_TYPES[a["type"]], "lat": round(float(a["latitude_deg"]), 4),
                     "lon": round(float(a["longitude_deg"]), 4)})
    rows.sort(key=lambda r: (r["rank"], r["city"].lower(), r["name"].lower()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["iata", "icao", "name", "city", "country", "rank", "lat", "lon"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} airports to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
