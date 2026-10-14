"""Visual building blocks: airport/city suggestion field, route map with distance, demand map, booking funnel and the
navy hero banners. Every number drawn here comes from stored records or airport coordinates — nothing is estimated."""

from __future__ import annotations

import html
from collections import Counter
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from src.places import Place, distance_km, label_for, place_values, resolve_place
from src.ui.common import SERIES

NAVY = "#0B1B33"
ACCENT = "#2F6BDB"
GREEN = "#1E7F5C"
KM_PER_NM = 1.852

# Decorative flight-path illustration for hero banners (pure SVG, no external assets).
FLIGHT_SVG = """
<svg class="fd-hero-art" viewBox="0 0 420 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs><linearGradient id="fdpath" x1="0" x2="1"><stop offset="0" stop-color="#8FB4FF" stop-opacity="0"/>
  <stop offset="1" stop-color="#8FB4FF" stop-opacity="0.9"/></linearGradient></defs>
  <g fill="none" stroke="rgba(255,255,255,0.10)">
    <circle cx="330" cy="40" r="60"/><circle cx="330" cy="40" r="110"/><circle cx="330" cy="40" r="160"/></g>
  <path id="fdroute" d="M60 178 C 150 90, 260 50, 370 70" stroke="url(#fdpath)" stroke-width="2.2" fill="none" stroke-dasharray="6 7"/>
  <circle cx="60" cy="178" r="4.5" fill="#8FD3B6"/><circle cx="370" cy="70" r="5.5" fill="#FFFFFF"/>
  <circle cx="370" cy="70" r="12" fill="none" stroke="rgba(255,255,255,0.35)"/>
  <path d="M-11 -5 L11 0 L-11 5 L-6 0 Z" fill="#FFFFFF"><animateMotion dur="7s" repeatCount="indefinite" rotate="auto">
    <mpath href="#fdroute"/></animateMotion></path>
</svg>"""

HERO_CSS = """
<style>
.fd-hero2 { background: linear-gradient(120deg, #0B1B33 0%, #13294B 62%, #1D4170 100%); color:#fff; border-radius:20px;
            padding:26px 30px; position:relative; overflow:hidden; min-height:132px; margin-bottom:6px; }
.fd-hero2 h2 { color:#fff !important; font-size:1.5rem !important; margin:0 0 6px 0 !important; position:relative; z-index:1; }
.fd-hero2 p { color:#CBD6EA; margin:0; font-size:0.98rem; max-width:56%; position:relative; z-index:1; line-height:1.5; }
.fd-hero2 .eyebrow { color:#8FD3B6; font-size:0.74rem; font-weight:700; letter-spacing:0.12em; text-transform:uppercase;
                     margin-bottom:8px; position:relative; z-index:1; }
.fd-hero2 .chips { margin-top:14px; display:flex; gap:8px; flex-wrap:wrap; position:relative; z-index:1; }
.fd-hero2 .chips span { background:rgba(255,255,255,0.10); border:1px solid rgba(255,255,255,0.16); color:#EAF0FA;
                        border-radius:999px; padding:4px 12px; font-size:0.8rem; font-weight:600; }
.fd-hero-art { position:absolute; right:0; top:0; height:100%; width:min(42%, 440px); opacity:0.95; }
.fd-route { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:0.86rem; color:#44506A; margin:2px 0 6px; }
.fd-route b { color:#0E1A2B; }
.fd-route .pill { background:#EAF1FD; color:#1D4F91; border-radius:999px; padding:2px 10px; font-weight:600; font-size:0.78rem; }
@media (max-width: 760px) { .fd-hero2 p { max-width:100%; } .fd-hero-art { opacity:0.35; width:100%; } }
</style>
"""


def hero(title: str, subtitle: str, eyebrow: str = "", chips: list[str] | None = None) -> None:
    """Navy banner with the flight-path illustration."""
    chip_html = "".join(f"<span>{html.escape(c)}</span>" for c in (chips or []))
    st.markdown(HERO_CSS + f'<div class="fd-hero2">{FLIGHT_SVG}'
                + (f'<div class="eyebrow">{html.escape(eyebrow)}</div>' if eyebrow else "")
                + f"<h2>{html.escape(title)}</h2><p>{html.escape(subtitle)}</p>"
                + (f'<div class="chips">{chip_html}</div>' if chip_html else "") + "</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------- airport / city suggestions
def place_select(label: str, key: str, current: str | None = None, *, container: Any = None,
                 placeholder: str = "Start typing a city, airport or code…", help: str | None = None) -> str | None:
    """Type-ahead field over ~4,600 airports (fuzzy search by name, city, country or IATA/ICAO code).
    Anything not in the list can still be typed and is kept as written."""
    options = list(place_values())
    if current and current not in options:
        options.insert(0, current)
    kwargs: dict[str, Any] = {}
    if key not in st.session_state:
        kwargs["index"] = options.index(current) if current else None
    target = container if container is not None else st
    value = target.selectbox(label, options, key=key, format_func=label_for, accept_new_options=True,
                             placeholder=placeholder, help=help or "Type to search airports and cities. Not listed? Just "
                             "type the place and press Enter.", **kwargs)
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------- maps
def _geo_layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_geos(projection_type="natural earth", showland=True, landcolor="#ECE6DA", showocean=True,
                    oceancolor="#F6F3EC", showcountries=True, countrycolor="#FFFFFF", countrywidth=0.6,
                    showcoastlines=False, showframe=False, bgcolor="rgba(0,0,0,0)")
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
                      hoverlabel=dict(bgcolor="#FFFFFF", font_size=12, font_family="Inter"))
    return fig


def route_summary(origin: str | None, destination: str | None) -> tuple[Place | None, Place | None, float | None]:
    a, b = resolve_place(origin), resolve_place(destination)
    return a, b, (distance_km(a, b) if a and b and a != b else None)


def route_map(origin: str | None, destination: str | None, *, height: int = 260, key: str | None = None) -> bool:
    """Draw the great-circle route between two places, with the distance. Returns False if a place isn't recognised."""
    a, b, km = route_summary(origin, destination)
    if not (a and b and km):
        return False
    nm = km / KM_PER_NM
    st.markdown(f'<div class="fd-route"><b>{html.escape(a.iata)}</b> {html.escape(a.city or a.name)} → '
                f'<b>{html.escape(b.iata)}</b> {html.escape(b.city or b.name)} <span class="pill">≈ {km:,.0f} km · '
                f'{nm:,.0f} nm great-circle</span></div>', unsafe_allow_html=True)
    fig = go.Figure()
    fig.add_trace(go.Scattergeo(lon=[a.lon, b.lon], lat=[a.lat, b.lat], mode="lines",
                                line=dict(width=2.6, color=ACCENT), hoverinfo="skip"))
    fig.add_trace(go.Scattergeo(lon=[a.lon, b.lon], lat=[a.lat, b.lat], mode="markers+text",
                                text=[a.iata, b.iata], textposition=["bottom center", "top center"],
                                textfont=dict(size=12, color=NAVY, family="Inter"),
                                marker=dict(size=[10, 12], color=[GREEN, NAVY], line=dict(width=2, color="#FFFFFF")),
                                hovertext=[a.label, b.label], hoverinfo="text"))
    fig.update_geos(fitbounds="locations", lataxis_range=None)
    st.plotly_chart(_geo_layout(fig, height), width="stretch", config={"displayModeBar": False}, key=key)
    st.caption("Straight-line (great-circle) distance between the airports — the actual flight path and time will differ.")
    return True


def demand_map(routes: list[tuple[str | None, str | None]], *, height: int = 330, key: str | None = None) -> dict[str, int]:
    """Routes customers asked for, drawn as arcs; airports sized by how often they appear. Returns counts used."""
    pairs: Counter[tuple[str, str]] = Counter()
    places: dict[str, Place] = {}
    unplaced = 0
    for o, d in routes:
        a, b = resolve_place(o), resolve_place(d)
        if not (a and b) or a == b:
            unplaced += 1
            continue
        places[a.iata], places[b.iata] = a, b
        pairs[(a.iata, b.iata)] += 1
    if not pairs:
        return {"mapped": 0, "unplaced": unplaced}
    fig = go.Figure()
    top = max(pairs.values())
    for (x, y), n in pairs.most_common(60):
        a, b = places[x], places[y]
        fig.add_trace(go.Scattergeo(lon=[a.lon, b.lon], lat=[a.lat, b.lat], mode="lines", hoverinfo="text",
                                    hovertext=f"{a.iata} → {b.iata}: {n} request{'s' if n != 1 else ''}",
                                    line=dict(width=1.4 + 2.6 * n / top, color=SERIES[0]), opacity=0.35 + 0.55 * n / top))
    seen: Counter[str] = Counter()
    for (x, y), n in pairs.items():
        seen[x] += n
        seen[y] += n
    pts = [places[c] for c in seen]
    fig.add_trace(go.Scattergeo(lon=[p.lon for p in pts], lat=[p.lat for p in pts], mode="markers",
                                marker=dict(size=[7 + 9 * seen[p.iata] / max(seen.values()) for p in pts], color=NAVY,
                                            line=dict(width=1.5, color="#FFFFFF")),
                                hovertext=[f"{p.label}<br>{seen[p.iata]} request(s) to/from here" for p in pts],
                                hoverinfo="text"))
    fig.update_geos(fitbounds="locations")
    st.plotly_chart(_geo_layout(fig, height), width="stretch", config={"displayModeBar": False}, key=key)
    return {"mapped": sum(pairs.values()), "unplaced": unplaced}


# ---------------------------------------------------------------------------- funnel
def funnel(stages: list[tuple[str, int]], *, height: int = 260, key: str | None = None) -> None:
    """Horizontal funnel for one cohort (each stage is a subset of the previous)."""
    labels, values = [s for s, _ in stages], [v for _, v in stages]
    first = values[0] or 1
    fig = go.Figure(go.Funnel(y=labels, x=values, textinfo="value", texttemplate="%{value}",
                              marker=dict(color=[SERIES[0]] * len(values), line=dict(width=0)),
                              connector=dict(line=dict(color="#E8E3D9", width=1)),
                              hovertemplate="%{y}: %{x}<extra></extra>", opacity=0.92))
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=6, b=6), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(family="Inter", color="#0E1A2B", size=13))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)
    rates = " → ".join(f"{label} {v / first:.0%}" for label, v in stages[1:])
    st.caption(f"Of {values[0]} inquiries received in the period: {rates}." if values[0] else "No inquiries in this period.")
