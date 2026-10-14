"""OpenAI client tests driven through the real SDK with an HTTP mock transport (no network, no cost)."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx2
import pytest

from src.ai.client import OpenAIClient, TokenUsage, build_ai_client, estimate_cost_usd
from src.ai.extraction import run_openai
from src.ai.prompts import EXTRACTION_INSTRUCTIONS
from src.config import get_settings
from src.exceptions import AINotConfigured, AIRequestRejected, AIResponseInvalid, AITransientError
from src.schemas import LLMInquiryExtraction

FAKE_KEY = "sk-test-not-a-real-key-1234567890"

GOOD = {
    "origin": "Los Angeles", "destination": "Aspen", "trip_type": None,
    "departure": {"exact": None, "earliest": "2026-12-01", "latest": "2026-12-31", "source_text": "in December"},
    "return_date": {"exact": None, "earliest": None, "latest": None, "source_text": None},
    "passenger_count": 6, "passenger_counts_mentioned": [6], "trip_purpose": None, "date_flexibility": "flexible",
    "budget_amount": None, "currency": None, "requested_services": ["accommodation"], "ambiguities": [],
    "inconsistencies": [], "evidence": [{"field": "destination", "quote": "to Aspen"}], "embedded_instructions_detected": False,
}


def _response(output: list, status: str = "completed", usage=(500, 120)) -> dict:
    return {"id": "resp_test", "object": "response", "created_at": 1760000000, "status": status, "model": "gpt-4.1-2025-04-14",
            "output": output, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "error": None,
            "incomplete_details": None, "instructions": None, "metadata": {}, "temperature": 0, "top_p": 1,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage),
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}


def _message(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


class Recorder:
    def __init__(self, responses: list[tuple[int, dict | str]]):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        code, body = self.responses.pop(0)
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx2.Response(code, content=content.encode(), headers={"content-type": "application/json"})


def _client(rec: Recorder, retries: int = 2) -> OpenAIClient:
    return OpenAIClient(api_key=FAKE_KEY, model="gpt-4.1", max_retries=retries,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(rec)), sleep=lambda s: None)


def test_structured_parse_success_and_request_shape():
    rec = Recorder([(200, _response([_message(json.dumps(GOOD))]))])
    res = _client(rec).parse(instructions=EXTRACTION_INSTRUCTIONS, user_input="<inquiry>x</inquiry>",
                             schema=LLMInquiryExtraction, purpose="test")
    assert isinstance(res.parsed, LLMInquiryExtraction) and res.parsed.passenger_count == 6
    assert res.usage == TokenUsage(500, 120) and res.attempts == 1
    body = json.loads(rec.requests[0].content)
    assert rec.requests[0].url.path.endswith("/responses")
    assert body["model"] == "gpt-4.1" and body["store"] is False and body["temperature"] == 0
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    assert "SECURITY RULES" in body["instructions"]


def test_malformed_json_is_invalid_output_and_not_retried():
    rec = Recorder([(200, _response([_message("{not json")]))])
    with pytest.raises(AIResponseInvalid):
        _client(rec).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")
    assert len(rec.requests) == 1


def test_schema_mismatch_is_invalid_output():
    bad = dict(GOOD)
    bad.pop("passenger_count")
    rec = Recorder([(200, _response([_message(json.dumps(bad))]))])
    with pytest.raises(AIResponseInvalid):
        _client(rec).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")
    assert len(rec.requests) == 1


def test_incomplete_response_is_invalid_output():
    rec = Recorder([(200, _response([_message(json.dumps(GOOD))], status="incomplete"))])
    with pytest.raises(AIResponseInvalid):
        _client(rec).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")


def test_rate_limit_is_retried_then_succeeds():
    err = {"error": {"message": "Rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}
    rec = Recorder([(429, err), (200, _response([_message(json.dumps(GOOD))]))])
    res = _client(rec).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")
    assert res.attempts == 2 and len(rec.requests) == 2


def test_server_errors_retry_is_bounded():
    err = {"error": {"message": "boom", "type": "server_error"}}
    rec = Recorder([(500, err)] * 5)
    with pytest.raises(AITransientError):
        _client(rec, retries=2).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")
    assert len(rec.requests) == 3  # 1 attempt + 2 bounded retries


def test_authentication_error_not_retried_and_key_not_leaked():
    err = {"error": {"message": f"Incorrect API key provided: {FAKE_KEY}", "type": "invalid_request_error"}}
    rec = Recorder([(401, err)])
    with pytest.raises(AIRequestRejected) as exc:
        _client(rec).parse(instructions="x", user_input="y", schema=LLMInquiryExtraction, purpose="test")
    assert len(rec.requests) == 1
    assert FAKE_KEY not in str(exc.value)


def test_tool_turn_parses_function_calls():
    out = [{"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "get_inquiry",
            "arguments": '{"inquiry_id": "INQ-ABCDEF1234"}', "status": "completed"}]
    rec = Recorder([(200, _response(out))])
    turn = _client(rec).tool_turn(instructions="x", input_items=[{"role": "user", "content": "hi"}],
                                  tools=[{"type": "function", "name": "get_inquiry", "parameters": {"type": "object"}, "strict": True}],
                                  purpose="test")
    assert turn.tool_calls[0].name == "get_inquiry" and turn.tool_calls[0].call_id == "call_abc"
    assert turn.output_items[0]["type"] == "function_call"
    body = json.loads(rec.requests[0].content)
    assert body["parallel_tool_calls"] is False and body["tools"][0]["name"] == "get_inquiry"


def test_run_openai_end_to_end_with_mock_transport():
    from datetime import date

    rec = Recorder([(200, _response([_message(json.dumps(GOOD))]))])
    text = "Hi, I need a private flight from Los Angeles to Aspen for six people in December. Our dates are flexible. Could you also help us arrange a hotel?"
    outcome = run_openai(text, date(2026, 10, 11), _client(rec))
    assert outcome.ok and outcome.schema_valid and outcome.usage.input_tokens == 500
    assert outcome.extraction.destination == "Aspen" and outcome.extraction.grounding["destination"]
    assert "<inquiry>" in json.loads(rec.requests[0].content)["input"]


def test_run_openai_api_failure_returns_error_not_fabrication():
    from datetime import date

    rec = Recorder([(401, {"error": {"message": "bad key", "type": "invalid_request_error"}})])
    outcome = run_openai("Fly LA to Aspen", date(2026, 10, 11), _client(rec))
    assert not outcome.ok and outcome.extraction is None and outcome.error_category == "ai_request_rejected"


def test_build_client_requires_key_and_demo_off():
    base = get_settings(load_env_file=False)
    with pytest.raises(AINotConfigured):
        build_ai_client(replace(base, demo_mode=True, openai_api_key=FAKE_KEY))
    with pytest.raises(AINotConfigured):
        build_ai_client(replace(base, demo_mode=False, openai_api_key=None))
    assert build_ai_client(replace(base, demo_mode=False, openai_api_key=FAKE_KEY)).model == base.openai_model


def test_cost_estimate_uses_configured_prices():
    s = replace(get_settings(load_env_file=False), price_input_per_1m=2.0, price_output_per_1m=8.0)
    assert estimate_cost_usd(TokenUsage(1_000_000, 500_000), s) == pytest.approx(6.0)
