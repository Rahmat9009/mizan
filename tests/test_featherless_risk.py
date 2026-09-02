from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.governor import PortfolioGovernor
from app.models import Decision, MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.pipeline import DecisionPipeline
from app.providers.featherless_risk import (
    DEFAULT_FEATHERLESS_MODEL,
    FALLBACK_REASON,
    FeatherlessRiskProvider,
)
from app.risk_engine import RiskEngine


def proposal(**updates) -> TradeProposal:
    value = TradeProposal(
        proposal_id="proposal-123",
        symbol="NVDA",
        side="BUY",
        quantity=20,
        estimated_price=200,
        strategy_confidence=0.85,
        thesis="Demand supports the position.",
        invalidation_condition="Margins fall below the strategy threshold.",
    )
    return value.model_copy(update=updates)


def portfolio(**updates) -> PortfolioSnapshot:
    value = PortfolioSnapshot(
        equity=100_000,
        cash=60_000,
        buying_power=60_000,
        daily_pnl_pct=-0.005,
        current_positions={"NVDA": 2_000, "AMD": 4_000},
    )
    return value.model_copy(update=updates)


def market(**updates) -> MarketRiskSnapshot:
    value = MarketRiskSnapshot(
        symbol="NVDA",
        annualized_volatility=0.45,
        max_drawdown_30d=0.12,
        liquidity_score=0.95,
    )
    return value.model_copy(update=updates)


def valid_payload(**updates) -> dict:
    value = {
        "recommendation": "REDUCE",
        "confidence": 0.88,
        "recommended_quantity": 8,
        "risk_thesis": "Correlated semiconductor exposure argues for smaller sizing.",
        "hidden_risks": ["Existing AMD exposure may increase thematic concentration."],
        "reasoning": ["Volatility and overlapping exposure warrant a reduction."],
    }
    value.update(updates)
    return value


def make_response(
    content: object = None,
    *,
    finish_reason: str = "stop",
    tool_calls: list | None = None,
    reasoning: str | None = None,
) -> SimpleNamespace:
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    message = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning=reasoning,
        reasoning_content=None,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=message)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80, total_tokens=180),
    )


def provider_with_response(response: SimpleNamespace) -> tuple[FeatherlessRiskProvider, Mock]:
    client = Mock()
    client.chat.completions.create.return_value = response
    provider = FeatherlessRiskProvider(client=client, model="test-org/test-risk-model")
    return provider, client


def assert_conservative_fallback(analysis, hard_quantity: int) -> None:
    assert analysis.recommendation in {Decision.REDUCE, Decision.REJECT}
    assert analysis.recommended_quantity < hard_quantity
    assert analysis.risk_thesis == FALLBACK_REASON
    assert FALLBACK_REASON in analysis.reasoning


def analyze_response(response: SimpleNamespace, *, trade: TradeProposal | None = None):
    trade = trade or proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider, client = provider_with_response(response)
    return provider.analyze(trade, portfolio(), market(), hard_risk), hard_risk, client


def test_default_model_is_instruction_tuned_qwen(monkeypatch) -> None:
    monkeypatch.setenv("FEATHERLESS_MODEL", "")
    provider = FeatherlessRiskProvider(client=Mock())
    assert provider.model == DEFAULT_FEATHERLESS_MODEL
    assert provider.model == "Qwen/Qwen3-30B-A3B-Instruct-2507"


def test_valid_json_mode_output_is_locally_identified_and_validated() -> None:
    analysis, _, client = analyze_response(make_response(valid_payload()))

    assert analysis.proposal_id == "proposal-123"
    assert analysis.model_name == "test-org/test-risk-model"
    assert analysis.recommendation == Decision.REDUCE
    assert analysis.recommended_quantity == 8

    request = client.chat.completions.create.call_args.kwargs
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "tools" not in request
    assert "tool_choice" not in request
    assert "hard_policy_quantity_cap" in request["messages"][1]["content"]
    assert "proposal_id" not in request["messages"][1]["content"].split("OUTPUT_SCHEMA:", 1)[1].split("INPUTS:", 1)[0]


@pytest.mark.parametrize(
    "content",
    [
        "{not valid JSON",
        "",
        None,
        json.dumps([valid_payload()]),
        "```json\n" + json.dumps(valid_payload()) + "\n```",
    ],
)
def test_malformed_or_missing_json_fails_conservatively(content) -> None:
    analysis, hard_risk, _ = analyze_response(make_response(content))
    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)


@pytest.mark.parametrize(
    "updates",
    [
        {"confidence": 1.5},
        {"recommendation": "HOLD"},
        {"recommended_quantity": -1},
        {"risk_thesis": "   "},
        {"reasoning": "not-an-array"},
        {"unknown_field": True},
        {"proposal_id": "model-supplied-id"},
        {"model_name": "model-supplied-name"},
    ],
)
def test_schema_invalid_output_fails_conservatively(updates: dict) -> None:
    analysis, hard_risk, _ = analyze_response(make_response(valid_payload(**updates)))
    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)


def test_oversized_quantity_fails_conservatively_and_governor_respects_cap() -> None:
    trade = proposal(quantity=80)
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider, _ = provider_with_response(
        make_response(valid_payload(recommendation="APPROVE", recommended_quantity=80))
    )

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)
    decision = PortfolioGovernor().decide(trade, hard_risk, analysis)

    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)
    assert decision.approved_quantity <= hard_risk.recommended_quantity
    assert decision.decision != Decision.APPROVE


def test_unexpected_tool_calls_fail_conservatively() -> None:
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name="wrong_tool", arguments="{}")),
        SimpleNamespace(function=SimpleNamespace(name="another_tool", arguments="{}")),
    ]
    analysis, hard_risk, _ = analyze_response(
        make_response(valid_payload(), tool_calls=tool_calls)
    )
    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)


def test_truncated_response_fails_conservatively() -> None:
    analysis, hard_risk, _ = analyze_response(
        make_response(valid_payload(), finish_reason="length")
    )
    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)


def test_hidden_reasoning_is_never_parsed() -> None:
    analysis, hard_risk, _ = analyze_response(
        make_response("", finish_reason="length", reasoning=json.dumps(valid_payload()))
    )
    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)


def test_timeout_fails_conservatively() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    client = Mock()
    client.chat.completions.create.side_effect = TimeoutError("request timed out")
    provider = FeatherlessRiskProvider(client=client, model="test-model")

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)
    assert "TimeoutError" in analysis.reasoning[-1]


def test_arbitrary_provider_exception_fails_conservatively() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("provider exploded")
    provider = FeatherlessRiskProvider(client=client, model="test-model")

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)
    assert "RuntimeError" in analysis.reasoning[-1]


def test_hard_policy_block_short_circuits_provider() -> None:
    trade = proposal()
    blocked_portfolio = portfolio(daily_pnl_pct=-0.05)
    hard_risk = RiskEngine().evaluate(trade, blocked_portfolio, market())
    provider, client = provider_with_response(make_response(valid_payload()))

    analysis = provider.analyze(trade, blocked_portfolio, market(), hard_risk)
    decision = PortfolioGovernor().decide(trade, hard_risk, analysis)

    client.chat.completions.create.assert_not_called()
    assert analysis.recommendation == Decision.REJECT
    assert analysis.recommended_quantity == 0
    assert decision.decision == Decision.REJECT


def test_missing_configuration_fallback_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("FEATHERLESS_API_KEY", "")
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider = FeatherlessRiskProvider(model="test-model")

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)
    assert "FEATHERLESS_API_KEY" in analysis.reasoning[-1]


def test_provider_fallback_is_explicit_in_pipeline_audit() -> None:
    trade = proposal()
    provider, _ = provider_with_response(make_response("{broken"))
    pipeline = DecisionPipeline(ai_provider=provider)

    _, analysis, decision = pipeline.run(trade, portfolio(), market())
    events = pipeline.audit.list_for_proposal(trade.proposal_id)

    assert analysis.risk_thesis == FALLBACK_REASON
    assert decision.decision in {Decision.REDUCE, Decision.REJECT}
    assert len(events) == 3
    assert events[1].action == "AI_RISK_ANALYZED"
    assert events[1].payload["risk_thesis"] == FALLBACK_REASON


def test_debug_logging_is_sanitized(monkeypatch, capsys) -> None:
    secret = "must-not-be-printed"
    monkeypatch.setenv("FEATHERLESS_DEBUG", "true")
    monkeypatch.setenv("FEATHERLESS_API_KEY", secret)
    payload = valid_payload(api_key=secret, authorization=secret)
    analysis, hard_risk, _ = analyze_response(make_response(payload))
    output = capsys.readouterr().out

    assert_conservative_fallback(analysis, hard_risk.recommended_quantity)
    assert "[FEATHERLESS DEBUG]" in output
    assert '"finish_reason": "stop"' in output
    assert '"content_exists": true' in output
    assert '"reasoning_exists": false' in output
    assert '"api_key": "[REDACTED]"' in output
    assert secret not in output
