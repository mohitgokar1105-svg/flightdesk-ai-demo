"""Shared fixtures. The test suite never makes paid API calls:

* OPENAI_API_KEY is removed from the environment for the whole session (unless RUN_LIVE_TESTS=1, which only
  the explicitly marked live smoke test uses), and DEMO_MODE is forced on.
* AI behaviour is exercised with :class:`FakeAIClient` (scripted responses) and with an HTTP-level mock
  transport that drives the real OpenAI SDK (see tests/unit/test_ai_client.py).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_LIVE = os.getenv("RUN_LIVE_TESTS") == "1"
if not _LIVE:
    # Empty (not absent) so a local .env can never inject a key: python-dotenv does not override set variables.
    os.environ["OPENAI_API_KEY"] = ""
os.environ["DEMO_MODE"] = "true"
os.environ["FLIGHTDESK_DISABLE_DOTENV"] = "1"  # never read the developer's .env during tests
os.environ["APP_ENV"] = "test"

from src.ai.client import StructuredResult, TokenUsage, ToolCallRequest, ToolTurn  # noqa: E402
from src.config import TEST_STATUS_FILE, Settings, get_settings  # noqa: E402
from src.database import dispose_engine, init_db, session_scope  # noqa: E402
from src.exceptions import AIServiceError  # noqa: E402


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Public-page rate limits are process-wide; give every test a clean slate."""
    from src.ratelimit import FLIGHT_REQUESTS, STATUS_LOOKUPS

    FLIGHT_REQUESTS.reset()
    STATUS_LOOKUPS.reset()
    yield


@pytest.fixture(autouse=True)
def system_principal():
    """Tests act as the trusted system principal unless a test switches to a specific user role."""
    from src.auth import SYSTEM, as_principal

    with as_principal(SYSTEM):
        yield


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    s = replace(get_settings(load_env_file=False), database_url=url, openai_api_key=None, demo_mode=True)
    init_db(url)
    yield s
    dispose_engine(url)


@pytest.fixture
def session(settings: Settings):
    with session_scope(settings.database_url) as s:
        yield s


@pytest.fixture
def seeded(settings: Settings):
    from src.seed import seed_demo_data

    with session_scope(settings.database_url) as s:
        seed_demo_data(s, settings, now=datetime(2026, 10, 11, 12, tzinfo=UTC))
    with session_scope(settings.database_url) as s:
        yield s


class FakeAIClient:
    """Scripted stand-in for :class:`src.ai.client.OpenAIClient` (same interface)."""

    def __init__(self, parse_responses: list[Any] | None = None, tool_turns: list[Any] | None = None,
                 model: str = "fake-model") -> None:
        self.model = model
        self.parse_responses = list(parse_responses or [])
        self.tool_turns = list(tool_turns or [])
        self.parse_calls: list[dict[str, Any]] = []
        self.tool_calls_seen: list[dict[str, Any]] = []

    def parse(self, *, instructions: str, user_input: str, schema: type, purpose: str) -> StructuredResult:
        self.parse_calls.append({"instructions": instructions, "user_input": user_input, "schema": schema, "purpose": purpose})
        if not self.parse_responses:
            raise AIServiceError("No scripted response left")
        nxt = self.parse_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        parsed = nxt if isinstance(nxt, schema) else schema.model_validate(nxt)
        return StructuredResult(parsed=parsed, usage=TokenUsage(120, 80), model=self.model, latency_ms=5.0, attempts=1,
                                raw_json=parsed.model_dump(mode="json"))

    def tool_turn(self, *, instructions: str, input_items: list[Any], tools: list[dict[str, Any]], purpose: str) -> ToolTurn:
        self.tool_calls_seen.append({"input_items": list(input_items), "tools": tools})
        if not self.tool_turns:
            return ToolTurn(tool_calls=[], text="Done.", usage=TokenUsage(10, 5), model=self.model, latency_ms=1.0,
                            attempts=1, output_items=[])
        nxt = self.tool_turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, str):
            return ToolTurn(tool_calls=[], text=nxt, usage=TokenUsage(10, 5), model=self.model, latency_ms=1.0, attempts=1,
                            output_items=[{"type": "message", "content": nxt}])
        calls = [ToolCallRequest(call_id=f"call_{i}", name=n, arguments_json=a if isinstance(a, str) else json.dumps(a))
                 for i, (n, a) in enumerate(nxt)]
        return ToolTurn(tool_calls=calls, text="", usage=TokenUsage(30, 10), model=self.model, latency_ms=1.0, attempts=1,
                        output_items=[{"type": "function_call", "call_id": c.call_id, "name": c.name,
                                       "arguments": c.arguments_json} for c in calls])


@pytest.fixture
def fake_ai():
    return FakeAIClient


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Record the latest test-suite summary for the Settings & System Health page."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    stats = {k: len(v) for k, v in reporter.stats.items() if k in ("passed", "failed", "error", "skipped")}
    try:
        TEST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TEST_STATUS_FILE.write_text(json.dumps({
            "finished_at": datetime.now(UTC).isoformat(), "exit_status": int(exitstatus), "counts": stats,
            "collected": session.testscollected,
        }), encoding="utf-8")
    except OSError:
        pass
