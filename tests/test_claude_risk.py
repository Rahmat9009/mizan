from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.governor import PortfolioGovernor
from app.models import Decision, MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.providers.claude_risk import ClaudeRiskProvider, FALLBACK_REASON
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


def response_with(payload: object) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def provider_returning(payload: object) -> tuple[ClaudeRiskProvider, Mock]:
    client = Mock()
    client.messages.create.return_value = response_with(payload)
    return ClaudeRiskProvider(client=client, model="claude-test-model"), client


def valid_payload(**updates) -> dict:
    value = {
        "proposal_id": "proposal-123",
        "recommendation": "REDUCE",
        "confidence": 0.88,
        "recommended_quantity": 8,
        "risk_thesis": "Correlated semiconductor exposure argues for smaller sizing.",
        "hidden_risks": ["Existing AMD exposure may increase sector correlation."],
        "reasoning": ["Volatility and correlated exposure warrant a reduction."],
        "model_name": "model-authored-value",
    }
    value.update(updates)
    return value


def test_valid_structured_provider_output() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider, client = provider_returning(valid_payload())

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert analysis.recommendation == Decision.REDUCE
    assert analysis.recommended_quantity == 8
    assert analysis.model_name == "claude-test-model"

    request = client.messages.create.call_args.kwargs
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "skeptical portfolio risk reviewer" in request["system"]
    assert "hard_policy_quantity_cap" in request["messages"][0]["content"]


def test_malformed_json_uses_conservative_fallback() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider, _ = provider_returning("{not valid JSON")

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert analysis.recommendation in {Decision.REDUCE, Decision.REJECT}
    assert analysis.recommended_quantity < hard_risk.recommended_quantity
    assert analysis.risk_thesis == FALLBACK_REASON


def test_schema_invalid_output_uses_conservative_fallback() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    provider, _ = provider_returning(valid_payload(confidence=1.5))

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert analysis.recommendation in {Decision.REDUCE, Decision.REJECT}
    assert analysis.confidence == 0.0
    assert FALLBACK_REASON in analysis.reasoning


def test_provider_exception_uses_conservative_fallback() -> None:
    trade = proposal()
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    client = Mock()
    client.messages.create.side_effect = TimeoutError("request timed out")
    provider = ClaudeRiskProvider(client=client, model="claude-test-model")

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)

    assert analysis.recommendation in {Decision.REDUCE, Decision.REJECT}
    assert analysis.risk_thesis == FALLBACK_REASON
    assert "TimeoutError" in analysis.reasoning[-1]


def test_ai_quantity_above_hard_cap_fails_conservatively() -> None:
    trade = proposal(quantity=80)
    hard_risk = RiskEngine().evaluate(trade, portfolio(), market())
    assert hard_risk.recommended_quantity < trade.quantity
    provider, _ = provider_returning(
        valid_payload(recommendation="APPROVE", recommended_quantity=trade.quantity)
    )

    analysis = provider.analyze(trade, portfolio(), market(), hard_risk)
    decision = PortfolioGovernor().decide(trade, hard_risk, analysis)

    assert analysis.risk_thesis == FALLBACK_REASON
    assert analysis.recommended_quantity < hard_risk.recommended_quantity
    assert decision.approved_quantity <= hard_risk.recommended_quantity
    assert decision.decision != Decision.APPROVE


def test_hard_block_short_circuits_ai_and_cannot_be_overridden() -> None:
    trade = proposal()
    blocked_portfolio = portfolio(daily_pnl_pct=-0.05)
    hard_risk = RiskEngine().evaluate(trade, blocked_portfolio, market())
    provider, client = provider_returning(
        valid_payload(recommendation="APPROVE", recommended_quantity=trade.quantity)
    )

    analysis = provider.analyze(trade, blocked_portfolio, market(), hard_risk)
    decision = PortfolioGovernor().decide(trade, hard_risk, analysis)

    client.messages.create.assert_not_called()
    assert analysis.recommendation == Decision.REJECT
    assert analysis.recommended_quantity == 0
    assert decision.decision == Decision.REJECT
    assert decision.approved_quantity == 0
