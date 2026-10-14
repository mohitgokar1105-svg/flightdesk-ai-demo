"""Application configuration loaded from environment variables (and an optional local .env file).

Secrets are held only in memory. ``Settings.safe_summary`` is the only representation that should
ever be displayed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "FlightDesk AI"
APP_VERSION = "0.1.0"
APP_TAGLINE = "Inquiry intelligence, advisor workflow automation and quote analytics for private-aviation services."
DISCLAIMER = (
    "Independent portfolio prototype. Not affiliated with, endorsed by, or connected to Flygreen or any "
    "aviation operator. All customers, quotes and bookings are fictional."
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EXPORT_DIR = DATA_DIR / "exports"
SAMPLES_DIR = DATA_DIR / "samples"
LOG_DIR = DATA_DIR / "logs"
EVAL_DATASET_DIR = PROJECT_ROOT / "evals" / "datasets"
TEST_STATUS_FILE = DATA_DIR / "test_status.json"

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Construct with :func:`get_settings`."""

    openai_api_key: str | None = field(repr=False)
    openai_model: str
    app_env: str
    demo_mode: bool
    database_url: str
    openai_timeout_seconds: float
    openai_max_retries: int
    price_input_per_1m: float
    price_output_per_1m: float

    @property
    def ai_key_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def live_ai_available(self) -> bool:
        """Live AI calls are permitted only when demo mode is off *and* a key is configured."""
        return (not self.demo_mode) and self.ai_key_configured

    @property
    def ai_mode_label(self) -> str:
        if self.demo_mode:
            return "Demo mode (no live AI calls)"
        if not self.ai_key_configured:
            return "Live AI disabled — OPENAI_API_KEY missing"
        return f"Live AI ({self.openai_model})"

    def safe_summary(self) -> dict[str, object]:
        """Configuration safe to display: never includes the key itself."""
        return {
            "app_env": self.app_env,
            "demo_mode": self.demo_mode,
            "openai_model": self.openai_model,
            "openai_api_key": "configured" if self.ai_key_configured else "not configured",
            "database_url": self.database_url,
            "openai_timeout_seconds": self.openai_timeout_seconds,
            "openai_max_retries": self.openai_max_retries,
            "price_input_per_1m_usd (assumed)": self.price_input_per_1m,
            "price_output_per_1m_usd (assumed)": self.price_output_per_1m,
        }


ENV_FILE = PROJECT_ROOT / ".env"


def get_settings(load_env_file: bool = True) -> Settings:
    """Read settings. The project's ``.env`` (if present) takes priority over shell variables, so a stale global
    ``OPENAI_API_KEY`` cannot silently break the app. Set ``FLIGHTDESK_DISABLE_DOTENV=1`` to ignore ``.env``
    (the test suite does this so automated tests can never pick up a real key)."""
    if load_env_file and os.getenv("FLIGHTDESK_DISABLE_DOTENV") != "1" and ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)
    default_db = f"sqlite:///{(DATA_DIR / 'flightdesk.db').as_posix()}"
    key = os.getenv("OPENAI_API_KEY") or None
    return Settings(
        openai_api_key=key.strip() if key else None,
        openai_model=(os.getenv("OPENAI_MODEL") or "gpt-4.1").strip(),
        app_env=(os.getenv("APP_ENV") or "development").strip(),
        demo_mode=_env_bool("DEMO_MODE", True),
        database_url=os.getenv("DATABASE_URL") or default_db,
        openai_timeout_seconds=_env_float("OPENAI_TIMEOUT_SECONDS", 30.0),
        openai_max_retries=max(0, min(_env_int("OPENAI_MAX_RETRIES", 2), 5)),
        price_input_per_1m=_env_float("OPENAI_PRICE_INPUT_PER_1M", 2.00),
        price_output_per_1m=_env_float("OPENAI_PRICE_OUTPUT_PER_1M", 8.00),
    )
