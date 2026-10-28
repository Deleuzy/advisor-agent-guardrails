import json

import httpx
import pytest

from advisor import data
from advisor.llm import AnthropicLLM, MistralLLM, MockLLM, cost_usd, select_llm
from advisor.tools import product_catalog

CATALOG = product_catalog(data.connect())


def test_default_is_mock_and_needs_no_key(monkeypatch):
    monkeypatch.delenv("ADVISOR_MODEL_PROVIDER", raising=False)
    assert isinstance(select_llm(CATALOG), MockLLM)


@pytest.mark.parametrize("provider,env", [("anthropic", "ANTHROPIC_API_KEY"), ("mistral", "MISTRAL_API_KEY")])
def test_real_provider_needs_key(monkeypatch, provider, env):
    monkeypatch.delenv(env, raising=False)
    with pytest.raises(RuntimeError):
        select_llm(CATALOG, provider)


def test_mock_plans_suitability():
    steps = json.loads(MockLLM(CATALOG).plan("Is Global Equity Index suitable for client 1042?").text)["steps"]
    assert [s["tool"] for s in steps] == ["get_risk_profile", "check_suitability", "draft_client_note"]
    assert steps[1]["args"] == {"client_id": 1042, "product_id": "P06"}


def _fake_post(payload):
    def post(url, **kw):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))
    return post


def test_anthropic_response_parsing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(httpx, "post", _fake_post({
        "content": [{"type": "text", "text": 'Plan:\n{"steps": []}'}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}))
    c = AnthropicLLM(CATALOG).plan("note for client 1042")
    assert json.loads(c.text) == {"steps": []} and (c.tokens_in, c.tokens_out) == (10, 5)


def test_mistral_response_parsing(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(httpx, "post", _fake_post({
        "choices": [{"message": {"content": "A note."}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
    c = MistralLLM(CATALOG).write_note("r", [])
    assert c.text == "A note." and c.tokens_in == 7


def test_cost_needs_configured_prices(monkeypatch):
    monkeypatch.delenv("ADVISOR_PRICE_IN_PER_MTOK", raising=False)
    assert cost_usd(1000, 1000) is None
    monkeypatch.setenv("ADVISOR_PRICE_IN_PER_MTOK", "1")
    monkeypatch.setenv("ADVISOR_PRICE_OUT_PER_MTOK", "2")
    assert cost_usd(1_000_000, 500_000) == 2.0
