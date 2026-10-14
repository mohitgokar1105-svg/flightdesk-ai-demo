"""Futuristic visual layer: the sign-in scene, the post-sign-in launch sequence and in-app polish.

Pure CSS + inline SVG (no scripts, no external assets beyond the existing Google Font). Every animation is switched off
for people who ask their system to reduce motion, and the in-app layer can be turned off in Settings. Text stays at
WCAG AA contrast; the effects are decoration only and never carry information.
"""

from __future__ import annotations

import html
import random

import streamlit as st

REDUCED_MOTION = "@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; " \
                 "transition: none !important; } }"


def fx_enabled() -> bool:
    return bool(st.session_state.get("fx_pref", True))


def _stars(n: int, seed: int, w: int = 2400, h: int = 1600) -> str:
    rng = random.Random(seed)
    return ", ".join(f"{rng.randint(0, w)}px {rng.randint(0, h)}px rgba(200,220,255,{rng.uniform(0.25, 0.9):.2f})"
                     for _ in range(n))


# ---------------------------------------------------------------------------- sign-in scene
LOGIN_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap');
section[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl'] {{ display:none; }}
header[data-testid='stHeader'] {{ background: transparent; }}
.stApp {{ background: radial-gradient(1200px 700px at 15% 10%, #13305e 0%, transparent 60%),
                     radial-gradient(900px 600px at 90% 90%, #2b1b5e 0%, transparent 55%),
                     linear-gradient(160deg, #040913 0%, #07142a 55%, #0a1022 100%) !important; }}
.block-container {{ max-width: 1180px !important; padding-top: 3rem !important; position: relative; z-index: 2; }}
.stApp [data-testid='stMarkdownContainer'], .stApp [data-testid='stMarkdownContainer'] p {{ color: #D5E1F5; }}
.stApp h1, .stApp h2, .stApp h3 {{ color: #FFFFFF !important; }}
.stApp [data-testid='stCaptionContainer'], .stApp .stCaption, .stApp small {{ color: #9DB0CF !important; }}
.stApp hr {{ border-color: rgba(140,170,230,0.18) !important; }}
/* starfield + aurora */
.fx-stars, .fx-stars2 {{ position: fixed; top: 0; left: 0; width: 2px; height: 2px; border-radius: 50%; z-index: 0;
                       pointer-events: none; }}
.fx-stars {{ box-shadow: {_stars(140, 1)}; animation: fxDrift 140s linear infinite; }}
.fx-stars2 {{ width: 1px; height: 1px; box-shadow: {_stars(220, 2)}; animation: fxDrift 220s linear infinite reverse; }}
@keyframes fxDrift {{ from {{ transform: translateY(0); }} to {{ transform: translateY(-600px); }} }}
.fx-aurora {{ position: fixed; inset: -20%; z-index: 0; pointer-events: none; opacity: 0.55; filter: blur(60px);
             background: conic-gradient(from 0deg at 50% 50%, rgba(0,194,255,0.10), rgba(108,92,231,0.16),
                         rgba(47,107,219,0.10), rgba(0,194,255,0.10)); animation: fxSpin 60s linear infinite; }}
@keyframes fxSpin {{ to {{ transform: rotate(360deg); }} }}
/* left: scene */
.fx-scene {{ position: relative; min-height: 560px; }}
.fx-kicker {{ font-family: 'JetBrains Mono', monospace; font-size: 0.74rem; letter-spacing: 0.22em; color: #6FE3C6;
             text-transform: uppercase; }}
.fx-kicker .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:#36F1C1; margin-right:8px;
                  box-shadow: 0 0 12px #36F1C1; animation: fxPulse 1.6s ease-in-out infinite; vertical-align: middle; }}
@keyframes fxPulse {{ 50% {{ opacity: 0.35; transform: scale(0.8); }} }}
.fx-title {{ font-family: 'Inter Tight','Inter',sans-serif; font-size: 2.9rem; line-height: 1.05; font-weight: 700;
            letter-spacing: -0.03em; margin: 14px 0 10px; color: #fff;
            background: linear-gradient(90deg, #FFFFFF 0%, #BFD8FF 45%, #7FE7FF 70%, #FFFFFF 100%);
            background-size: 200% auto; -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
            animation: fxShine 6s linear infinite, fxReveal 1.2s cubic-bezier(.2,.8,.2,1) both; }}
@keyframes fxShine {{ to {{ background-position: 200% center; }} }}
@keyframes fxReveal {{ from {{ clip-path: inset(0 100% 0 0); }} to {{ clip-path: inset(0 0 0 0); }} }}
.fx-lede {{ color: #B9C8E3; font-size: 1.02rem; max-width: 460px; line-height: 1.6; animation: fxFadeUp 1s .5s both; }}
@keyframes fxFadeUp {{ from {{ opacity: 0; transform: translateY(14px); }} to {{ opacity: 1; transform: none; }} }}
.fx-globe-wrap {{ position: relative; width: 380px; height: 380px; margin: 18px 0 0 10px; animation: fxFadeUp 1.2s .3s both; }}
.fx-radar {{ position: absolute; inset: -30px; border-radius: 50%;
            background: conic-gradient(from 0deg, rgba(54,241,193,0.0) 0deg, rgba(54,241,193,0.22) 40deg, rgba(54,241,193,0) 70deg);
            animation: fxSpin 5s linear infinite; mask: radial-gradient(circle, #000 62%, transparent 63%);
            -webkit-mask: radial-gradient(circle, #000 62%, transparent 63%); }}
.fx-ring {{ position: absolute; border-radius: 50%; border: 1px solid rgba(127,190,255,0.18); }}
.fx-globe {{ position: absolute; inset: 0; }}
.fx-globe .lon {{ transform-origin: 190px 190px; animation: fxLon 9s linear infinite; }}
@keyframes fxLon {{ 0% {{ transform: scaleX(1); }} 50% {{ transform: scaleX(-1); }} 100% {{ transform: scaleX(1); }} }}
.fx-arc {{ fill: none; stroke: url(#fxArc); stroke-width: 1.8; stroke-dasharray: 6 8; animation: fxDash 3s linear infinite; }}
@keyframes fxDash {{ to {{ stroke-dashoffset: -56; }} }}
.fx-hud {{ position: absolute; font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; color: #8FB4E8; letter-spacing: 0.08em;
          background: rgba(10,22,44,0.55); border: 1px solid rgba(127,190,255,0.22); padding: 4px 8px; border-radius: 6px;
          backdrop-filter: blur(6px); }}
.fx-hud b {{ color: #E8F1FF; font-weight: 600; }}
.fx-ticker {{ margin-top: 26px; overflow: hidden; white-space: nowrap; border-top: 1px solid rgba(127,190,255,0.16);
             border-bottom: 1px solid rgba(127,190,255,0.16); padding: 8px 0; max-width: 520px;
             mask: linear-gradient(90deg, transparent, #000 10%, #000 90%, transparent);
             -webkit-mask: linear-gradient(90deg, transparent, #000 10%, #000 90%, transparent); }}
.fx-ticker span {{ display: inline-block; padding-left: 100%; font-family: 'JetBrains Mono', monospace; font-size: 0.72rem;
                  color: #9CC2F5; letter-spacing: 0.12em; animation: fxTicker 32s linear infinite; }}
@keyframes fxTicker {{ to {{ transform: translateX(-100%); }} }}
/* right: glass sign-in panel */
.stApp [data-testid='stForm'] {{ background: linear-gradient(160deg, rgba(20,38,72,0.72), rgba(10,20,40,0.62)) !important;
    border: 1px solid rgba(127,190,255,0.30) !important; border-radius: 18px !important; padding: 22px 22px 14px !important;
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    box-shadow: 0 0 0 1px rgba(0,194,255,0.06), 0 20px 60px rgba(0,0,0,0.45), 0 0 40px rgba(47,107,219,0.25);
    animation: fxGlow 4s ease-in-out infinite, fxFadeUp .9s .2s both; }}
@keyframes fxGlow {{ 50% {{ box-shadow: 0 0 0 1px rgba(0,194,255,0.12), 0 20px 60px rgba(0,0,0,0.45), 0 0 60px rgba(108,92,231,0.35); }} }}
.stApp [data-testid='stWidgetLabel'] p {{ color: #A9C1E6 !important; font-family: 'JetBrains Mono', monospace;
    font-size: 0.74rem !important; letter-spacing: 0.14em; text-transform: uppercase; }}
.stApp [data-baseweb='input'], .stApp [data-baseweb='base-input'] {{ background: rgba(255,255,255,0.05) !important;
    border-color: rgba(127,190,255,0.28) !important; border-radius: 10px !important; }}
.stApp [data-baseweb='input'] > div, .stApp [data-baseweb='input'] input, .stApp [data-baseweb='input'] button {{
    background: transparent !important; }}
.stApp [data-baseweb='input'], .stApp [data-testid='stTextInputRootElement'] {{ background: rgba(8,18,38,0.65) !important; }}
.stApp [data-testid='stTextInputRootElement'] div {{ background: transparent !important; }}
.stApp [data-baseweb='input'] input, .stApp [data-testid='stTextInputRootElement'] input {{ color: #F2F7FF !important;
    -webkit-text-fill-color: #F2F7FF !important; caret-color: #36F1C1; }}
.stApp [data-testid='stTextInputRootElement'] input::placeholder {{ color: #7F93B5 !important; -webkit-text-fill-color: #7F93B5 !important; }}
.stApp [data-testid='stTextInputRootElement']:focus-within {{ border-color: #36F1C1 !important;
    box-shadow: 0 0 0 3px rgba(54,241,193,0.18), 0 0 18px rgba(54,241,193,0.25) !important; }}
.stApp [data-baseweb='input'] input:-webkit-autofill {{ -webkit-box-shadow: 0 0 0 40px #0a1730 inset !important; }}
.stApp [data-baseweb='input']:focus-within {{ border-color: #36F1C1 !important; box-shadow: 0 0 0 3px rgba(54,241,193,0.18),
    0 0 18px rgba(54,241,193,0.25) !important; }}
.stApp [data-baseweb='input'] button svg {{ fill: #A9C1E6 !important; }}
.stApp [data-testid='stFormSubmitButton'] button {{ border: none !important; color: #fff !important; font-weight: 650;
    letter-spacing: 0.06em; background: linear-gradient(90deg, #2F6BDB, #6C5CE7, #00A8E8, #2F6BDB) !important;
    background-size: 300% 100% !important; animation: fxBtn 6s linear infinite; box-shadow: 0 8px 30px rgba(47,107,219,0.45);
    height: 46px; }}
.stApp [data-testid='stFormSubmitButton'] button:hover {{ box-shadow: 0 8px 36px rgba(0,194,255,0.55); transform: translateY(-1px); }}
@keyframes fxBtn {{ to {{ background-position: 300% 0; }} }}
.stApp [data-testid='stPageLink'] a {{ border: 1px solid rgba(127,190,255,0.22); border-radius: 10px; background: rgba(255,255,255,0.03); }}
.stApp [data-testid='stPageLink'] a:hover {{ border-color: #36F1C1; background: rgba(54,241,193,0.06); }}
.stApp [data-testid='stPageLink'] a span, .stApp [data-testid='stPageLink'] a p {{ color: #CFE0FA !important; }}
.fx-signin-head {{ font-family: 'JetBrains Mono', monospace; color: #6FE3C6; font-size: 0.74rem; letter-spacing: 0.22em; }}
.fx-signin-title {{ font-family: 'Inter Tight','Inter',sans-serif; font-weight: 700; font-size: 1.7rem; color: #fff; margin: 4px 0 2px; }}
.fx-signin-sub {{ color: #9DB0CF; font-size: 0.9rem; margin-bottom: 14px; }}
@media (max-width: 900px) {{ .fx-globe-wrap {{ width: 280px; height: 280px; }} .fx-title {{ font-size: 2.1rem; }} }}
{REDUCED_MOTION}
</style>
"""


def _globe_svg() -> str:
    lons = "".join(f'<ellipse class="lon" cx="190" cy="190" rx="{r}" ry="150" style="animation-delay:-{d}s"/>'
                   for r, d in ((150, 0), (110, 1.1), (70, 2.2), (30, 3.4), (150, 4.5), (110, 5.6), (70, 6.7)))
    lats = "".join(f'<ellipse cx="190" cy="{190 + y}" rx="{w}" ry="{h}"/>'
                   for y, w, h in ((0, 150, 22), (-60, 138, 18), (60, 138, 18), (-105, 106, 12), (105, 106, 12)))
    arcs = [("M95 230 Q 190 40 300 150", "5s"), ("M120 120 Q 220 260 320 210", "7s"), ("M70 175 Q 160 300 260 290", "6s")]
    paths = "".join(f'<path id="fxA{i}" class="fx-arc" d="{d}"/>' for i, (d, _) in enumerate(arcs))
    planes = "".join(
        f'<g><path d="M-7 -3 L7 0 L-7 3 L-4 0 Z" fill="#FFFFFF"><animateMotion dur="{t}" repeatCount="indefinite" '
        f'rotate="auto"><mpath href="#fxA{i}"/></animateMotion></path></g>' for i, (_, t) in enumerate(arcs))
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="3.2" fill="#36F1C1"><animate attributeName="r" values="2.5;5;2.5" '
                   f'dur="2.4s" begin="{b}s" repeatCount="indefinite"/></circle>'
                   for x, y, b in ((95, 230, 0), (300, 150, .6), (120, 120, 1.2), (320, 210, 1.8), (70, 175, .9), (260, 290, 1.5)))
    return (
        '<svg class="fx-globe" viewBox="0 0 380 380" aria-hidden="true">'
        '<defs><radialGradient id="fxG" cx="38%" cy="32%"><stop offset="0" stop-color="#1D4C8F" stop-opacity="0.85"/>'
        '<stop offset="1" stop-color="#06142B" stop-opacity="0.95"/></radialGradient>'
        '<linearGradient id="fxArc" x1="0" x2="1"><stop offset="0" stop-color="#36F1C1"/><stop offset="1" stop-color="#7FB3FF"/>'
        '</linearGradient></defs>'
        '<circle cx="190" cy="190" r="152" fill="url(#fxG)" stroke="rgba(127,190,255,0.45)" stroke-width="1.2"/>'
        f'<g fill="none" stroke="rgba(127,190,255,0.28)" stroke-width="0.9">{lats}</g>'
        f'<g fill="none" stroke="rgba(127,190,255,0.22)" stroke-width="0.9">{lons}</g>'
        f'{paths}{planes}{dots}</svg>')


def login_scene(app_name: str, ticker_items: list[str], home: str | None) -> None:
    """Background (aurora + stars) and the animated left-hand scene."""
    st.markdown(LOGIN_CSS + '<div class="fx-aurora"></div><div class="fx-stars"></div><div class="fx-stars2"></div>',
                unsafe_allow_html=True)
    ticker = "   ◆   ".join(html.escape(t) for t in ticker_items)
    rings = "".join(f'<div class="fx-ring" style="inset:{-i * 26}px"></div>' for i in (1, 2, 3))
    hud = (f'<div class="fx-hud" style="top:8px;right:-24px">HOME BASE <b>{html.escape(home)}</b></div>' if home else "") + \
        '<div class="fx-hud" style="bottom:22px;left:-18px">AI CORE <b>ONLINE</b></div>' \
        '<div class="fx-hud" style="bottom:-6px;right:12px">ROUTES <b>LIVE MAP</b></div>'
    st.markdown(
        f'<div class="fx-scene"><div class="fx-kicker"><span class="dot"></span>{html.escape(app_name)} // operations '
        f'system</div><div class="fx-title">Every charter request,<br>handled.</div>'
        '<div class="fx-lede">Inquiries, quotes, customers and follow-ups in one place — with an AI co-pilot that '
        'answers from your own records and never sends anything without you.</div>'
        f'<div class="fx-globe-wrap"><div class="fx-radar"></div>{rings}{_globe_svg()}{hud}</div>'
        f'<div class="fx-ticker"><span>{ticker}</span></div></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------- launch sequence
LAUNCH_CSS = """
<style>
.fx-launch { position: fixed; inset: 0; z-index: 999999; display: flex; align-items: center; justify-content: center;
             background: radial-gradient(900px 600px at 50% 40%, #0f2a55 0%, #050b18 70%); pointer-events: none;
             animation: fxLaunchOut .7s ease-in 2.7s forwards; }
@keyframes fxLaunchOut { to { opacity: 0; visibility: hidden; } }
.fx-launch .core { position: relative; width: 150px; height: 150px; margin: 0 auto 28px; }
.fx-launch .core i { position: absolute; inset: 0; border-radius: 50%; border: 1.5px solid rgba(54,241,193,0.7);
                     animation: fxPing 1.8s cubic-bezier(0,.5,.5,1) infinite; }
.fx-launch .core i:nth-child(2) { animation-delay: .6s; border-color: rgba(127,179,255,0.7); }
.fx-launch .core i:nth-child(3) { animation-delay: 1.2s; border-color: rgba(108,92,231,0.7); }
@keyframes fxPing { from { transform: scale(.25); opacity: 1; } to { transform: scale(1.35); opacity: 0; } }
.fx-launch .core b { position: absolute; inset: 52px; border-radius: 50%; background: radial-gradient(circle, #36F1C1, #1b7f8f);
                     box-shadow: 0 0 40px #36F1C1; }
.fx-launch .lines { font-family: 'JetBrains Mono', ui-monospace, monospace; color: #CFE3FF; font-size: 0.92rem; letter-spacing: .06em;
                    min-width: 420px; }
.fx-launch .lines div { opacity: 0; animation: fxLine .35s ease-out forwards; margin: 7px 0; }
.fx-launch .lines div span { color: #36F1C1; }
@keyframes fxLine { from { opacity: 0; transform: translateX(-10px); } to { opacity: 1; transform: none; } }
.fx-launch .bar { height: 3px; background: rgba(127,179,255,0.18); border-radius: 3px; margin-top: 18px; overflow: hidden; }
.fx-launch .bar i { display: block; height: 100%; width: 0; background: linear-gradient(90deg, #36F1C1, #7FB3FF, #6C5CE7);
                    animation: fxLoad 2.4s ease-in-out .2s forwards; box-shadow: 0 0 12px #36F1C1; }
@keyframes fxLoad { to { width: 100%; } }
.fx-launch .hello { font-family: 'Inter Tight','Inter',sans-serif; color: #fff; font-size: 1.6rem; font-weight: 700; margin-top: 16px;
                    opacity: 0; animation: fxLine .5s ease-out 2.05s forwards; }
@media (prefers-reduced-motion: reduce) { .fx-launch { display: none; } }
</style>
"""


def launch_sequence(lines: list[str], hello: str) -> None:
    """Full-screen sign-in sequence that plays once (about 3 s) and then fades away. Purely decorative."""
    body = "".join(f'<div style="animation-delay:{0.25 + i * 0.38:.2f}s"><span>›</span> {html.escape(t)}</div>'
                   for i, t in enumerate(lines))
    st.markdown(LAUNCH_CSS + f'<div class="fx-launch" aria-hidden="true"><div><div class="core"><i></i><i></i><i></i><b></b>'
                f'</div><div class="lines">{body}<div class="bar"><i></i></div></div>'
                f'<div class="hello" style="text-align:center">{html.escape(hello)}</div></div></div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------------------- in-app polish
APP_CSS = f"""
<style>
.stApp {{ background-color: #FAF8F4;
         background-image: linear-gradient(rgba(47,107,219,0.035) 1px, transparent 1px),
                           linear-gradient(90deg, rgba(47,107,219,0.035) 1px, transparent 1px);
         background-size: 32px 32px; }}
header[data-testid='stHeader'] {{ background: transparent; }}
header[data-testid='stHeader']::after {{ content: ""; position: absolute; left: 0; right: 0; top: 0; height: 2px;
    background: linear-gradient(90deg, transparent, #36F1C1, #2F6BDB, #6C5CE7, transparent); background-size: 200% 100%;
    animation: fxBar 8s linear infinite; }}
@keyframes fxBar {{ to {{ background-position: 200% 0; }} }}
.fd-card {{ position: relative; transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
           animation: fxRise .55s cubic-bezier(.2,.8,.2,1) both; }}
[data-testid='stColumn']:nth-child(2) .fd-card {{ animation-delay: .06s; }}
[data-testid='stColumn']:nth-child(3) .fd-card {{ animation-delay: .12s; }}
[data-testid='stColumn']:nth-child(4) .fd-card {{ animation-delay: .18s; }}
[data-testid='stColumn']:nth-child(5) .fd-card {{ animation-delay: .24s; }}
@keyframes fxRise {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: none; }} }}
.fd-card:hover {{ transform: translateY(-3px); border-color: rgba(47,107,219,0.45);
                 box-shadow: 0 10px 30px rgba(11,27,51,0.10), 0 0 0 1px rgba(47,107,219,0.12), 0 0 26px rgba(54,241,193,0.12); }}
.fd-card::after {{ content: ""; position: absolute; left: 18px; right: 18px; top: 0; height: 2px; border-radius: 2px;
                  background: linear-gradient(90deg, #36F1C1, #2F6BDB, #6C5CE7); opacity: .0; transition: opacity .2s; }}
.fd-card:hover::after {{ opacity: 1; }}
.fd-card .val {{ background: linear-gradient(90deg, #0B1B33, #2F6BDB); -webkit-background-clip: text; background-clip: text;
                -webkit-text-fill-color: transparent; }}
.fd-summary {{ position: relative; overflow: hidden; }}
.fd-summary::before {{ content: ""; position: absolute; top: 0; left: -40%; width: 40%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(47,107,219,0.06), transparent); animation: fxSweep 6s ease-in-out infinite; }}
@keyframes fxSweep {{ 0%, 60% {{ left: -40%; }} 100% {{ left: 110%; }} }}
.fd-hero2, .fd-portal-hero {{ background-size: 220% 220% !important; animation: fxAurora 14s ease-in-out infinite; }}
.fd-hero2 {{ background-image: linear-gradient(120deg, #0B1B33 0%, #13294B 40%, #1D4170 60%, #24316e 80%, #0B1B33 100%) !important; }}
@keyframes fxAurora {{ 50% {{ background-position: 100% 50%; }} }}
.fd-hero-art path[stroke-dasharray] {{ animation: fxDash2 2.4s linear infinite; }}
@keyframes fxDash2 {{ to {{ stroke-dashoffset: -26; }} }}
section[data-testid='stSidebar'] {{ background: linear-gradient(180deg, #0B1B33 0%, #0a1830 60%, #0d1d3d 100%) !important; }}
section[data-testid='stSidebar']::before {{ content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px; z-index: 5;
    background: linear-gradient(90deg, #36F1C1, #2F6BDB, #6C5CE7); }}
section[data-testid='stSidebar'] a[aria-current='page'] {{ box-shadow: inset 3px 0 0 #36F1C1, 0 0 22px rgba(54,241,193,0.12) !important; }}
.fd-brand .pulse {{ display:inline-block; width:7px; height:7px; border-radius:50%; background:#36F1C1; margin-left:8px;
                   box-shadow: 0 0 10px #36F1C1; animation: fxPulse 1.8s ease-in-out infinite; vertical-align: middle; }}
@keyframes fxPulse {{ 50% {{ opacity: .35; }} }}
.stButton button[kind='primary'], [data-testid='stBaseButton-primary'] {{ background: linear-gradient(90deg, #2F6BDB, #4B5FE0) !important;
    border: none !important; box-shadow: 0 4px 14px rgba(47,107,219,0.28); transition: box-shadow .18s, transform .18s; }}
.stButton button[kind='primary']:hover, [data-testid='stBaseButton-primary']:hover {{ box-shadow: 0 6px 22px rgba(47,107,219,0.42),
    0 0 18px rgba(54,241,193,0.25); transform: translateY(-1px); }}
{REDUCED_MOTION}
</style>
"""


def app_fx() -> None:
    if fx_enabled():
        st.markdown(APP_CSS, unsafe_allow_html=True)
