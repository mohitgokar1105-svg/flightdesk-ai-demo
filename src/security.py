"""Security helpers: secret redaction, untrusted-text fingerprinting, prompt-injection heuristics.

The heuristics here are a *signal for the advisor*, not the defence itself. The real defence is
architectural: model output is constrained to schemas, every tool argument is validated in Python,
and consequential mutations require human approval regardless of what the model says.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),  # OpenAI-style keys (incl. sk-proj-...)
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s,;]+)"),
]

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|system|your)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines)")),
    ("role_reassignment", re.compile(r"(?i)\b(you are now|act as|pretend to be|new instructions|developer mode|jailbreak)\b")),
    ("system_prompt_probe", re.compile(r"(?i)\b(system prompt|hidden prompt|reveal (your|the) (prompt|instructions)|print your instructions)\b")),
    ("unauthorised_action", re.compile(r"(?i)\b(mark|set|change|update)\b.{0,30}\b(status|booking|quote|inquiry)\b.{0,30}\b(confirmed|booked|approved|paid|completed)\b")),
    ("pricing_manipulation", re.compile(r"(?i)\b(give|apply|grant|add)\b.{0,20}(\d{1,3}\s?%|\bfree\b|\bcomplimentary\b).{0,20}\b(discount|off|upgrade|flight)\b")),
    ("data_exfiltration", re.compile(r"(?i)\b(list|show|dump|export|send)\b.{0,30}\b(all|other)\b.{0,20}\b(customers?|clients?|records|inquiries|api keys?|passwords?)\b")),
    ("destructive_request", re.compile(r"(?i)\b(delete|drop|truncate|wipe)\b.{0,30}\b(database|table|records|all)\b")),
    ("tool_invocation", re.compile(r"(?i)\b(call|invoke|run|execute)\b.{0,20}\b(tool|function|sql|command|script)\b")),
]


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with API keys / bearer tokens / given secret values replaced by ``[REDACTED]``."""
    if not text:
        return text
    out = text
    for secret in extra_secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    out = _SECRET_PATTERNS[0].sub("[REDACTED]", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "[REDACTED]", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + "[REDACTED]", out)
    return out


def text_fingerprint(text: str) -> dict[str, object]:
    """Privacy-preserving description of untrusted text for logs/audit (length + short hash)."""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return {"chars": len(text or ""), "sha256_12": digest}


def summarise_untrusted(text: str, max_chars: int = 80) -> str:
    """Short single-line preview for internal display (never used in logs)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= max_chars else flat[: max_chars - 1] + "…"


def detect_prompt_injection(text: str) -> list[str]:
    """Return the names of injection heuristics matched by ``text`` (empty list if none)."""
    if not text:
        return []
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]
