"""Logging setup with secret redaction.

Customer messages are never logged in full; callers log lengths/hashes via :func:`src.security.text_fingerprint`.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

from src.config import LOG_DIR
from src.security import redact_secrets

_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """Masks anything that looks like an API key (and the configured key value) in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            return True
        redacted = redact_secrets(message, extra_secrets=[os.getenv("OPENAI_API_KEY") or ""])
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: int = logging.INFO, to_file: bool = True) -> None:
    """Configure root logging once. Safe to call repeatedly (Streamlit reruns the script)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactingFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_DIR / "flightdesk.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only filesystem
            root.warning("File logging unavailable; continuing with console logging only.")

    # Third-party HTTP libraries can log request URLs/headers at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpx2", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
