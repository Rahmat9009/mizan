"""L5 security pins: caller-controlled risk inputs bypass the legacy gate.

Subject: ``app.risk_engine``, ``app.governor``, ``app.execution.gate`` (legacy).

These are PASSING tests that prove a weakness, so that the new core has a
concrete adversarial case to defeat. Every test is pure and offline.

Findings: security/findings.md F-1 (price poisoning), F-2 (market-risk poisoning),
F-4 (kill switch frozen at config load).

Hard Rules at stake: E2 (unknown risk != safe), E3 (no bypass), E4 (kill switch
checked immediately before the mutation), E9 (TOCTOU re-check of every value).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai_risk import MockAIRiskProvider
from app.execution.gate import ExecutionConfig, ExecutionGate
from app.governor import PortfolioGovernor
from app.models import Decision, MarketRiskSnapshot, PortfolioSnapshot, Side, TradeProposal
from app.risk_engine import RiskEngine

NOW = datetime(2026, 9, 2, 17, 40, 0, tzinfo=timezone.utc)


def portfolio() -> PortfolioSnapshot:
    # $10k account, $5k buying power, flat on the day, no positions.
    return PortfolioSnapshot(
        equity=10_000.0,
        cash=5_000.0,
        buying_power=5_000.0,
        daily_pnl_pct=0.0,
        source="ALPACA_PAPER",
    )


def market(*, liquidity: float = 0.95, volatility: float = 0.30) -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=volatility,
        max_drawdown_30d=0.10,
        liquidity_score=liquidity,
    )


def proposal(proposal_id: str, *, quantity: int, price: float) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=quantity,
        estimated_price=price,
        strategy_confidence=0.9,
        thesis="thesis",
        invalidation_condition="invalidation",
    )


# ---------------------------------------------------------------------------
# F-1: the valuation price is caller-supplied and never verified server-side
# ---------------------------------------------------------------------------

def test_honest_price_is_blocked_by_buying_power_and_allocation() -> None:
    """Control case: 1,000 AAPL at a realistic $250 is a $250k order on a $10k account."""

    report = RiskEngine().evaluate(proposal("honest", quantity=1000, price=250.0), portfolio(), market())
    assert report.blocked is True
    assert "Trade exceeds available buying power." in report.reasons


def test_legacy_price_poisoning_passes_every_notional_check() -> None:
    """F-1 (CRITICAL). Same 1,000 shares, caller claims $0.01 -> $10 notional.

    Every notional-based rule (buying_power, max_trade_size, position_concentration)
    is computed from ``proposal.estimated_price``; there is no quote lookup anywhere
    in the legacy evaluate or execute path (``_fresh_risk_check`` reuses the same
    caller-supplied price). The new core must value orders ONLY from
    ``RiskContext.market_snapshot`` quotes sourced by the broker context provider.
    """

    poisoned = proposal("poison", quantity=1000, price=0.01)
    report = RiskEngine().evaluate(poisoned, portfolio(), market())

    assert report.blocked is False
    assert report.recommended_quantity == 1000
    assert report.reasons == ["Proposal is within current portfolio risk policy."]


def test_legacy_price_poisoning_is_approved_and_authorized_end_to_end() -> None:
    """F-1 continued: governor APPROVEs and the execution gate AUTHORIZEs 1,000 shares."""

    poisoned = proposal("poison-e2e", quantity=1000, price=0.01)
    report = RiskEngine().evaluate(poisoned, portfolio(), market())
    ai = MockAIRiskProvider().analyze(poisoned, portfolio(), market(), report)
    decision = PortfolioGovernor().decide(poisoned, report, ai)

    assert decision.decision is Decision.APPROVE
    assert decision.approved_quantity == 1000

    gate = ExecutionGate(ExecutionConfig(paper=True, enabled=True, dry_run=True, kill_switch=False))
    # The governor stamps decided_at from the wall clock, so freshness must be checked against it.
    authorization = gate.authorize(poisoned, report, decision, market(), now=datetime.now(timezone.utc))
    assert authorization.approved_quantity == 1000


# ---------------------------------------------------------------------------
# F-2: market-risk inputs (liquidity, volatility) are caller-supplied
# ---------------------------------------------------------------------------

def test_honest_illiquidity_blocks() -> None:
    report = RiskEngine().evaluate(proposal("illiquid", quantity=1, price=250.0), portfolio(), market(liquidity=0.10))
    assert report.blocked is True
    assert "Liquidity is below execution safety threshold." in report.reasons


def test_legacy_market_risk_poisoning_bypasses_liquidity_block() -> None:
    """F-2 (CRITICAL). The BLOCK-severity liquidity rule is evaluated on a number the
    caller typed. Claiming liquidity_score=1.0 for an illiquid name removes the block.
    ``POST /proposals/evaluate`` accepts this snapshot from any unauthenticated caller.
    """

    report = RiskEngine().evaluate(proposal("claimed-liquid", quantity=1, price=250.0), portfolio(), market(liquidity=1.0))
    assert report.blocked is False


def test_legacy_volatility_haircut_is_caller_controlled() -> None:
    honest = RiskEngine().evaluate(proposal("vol-h", quantity=10, price=250.0), portfolio(), market(volatility=0.95))
    poisoned = RiskEngine().evaluate(proposal("vol-p", quantity=10, price=250.0), portfolio(), market(volatility=0.01))
    assert honest.recommended_quantity < poisoned.recommended_quantity


# ---------------------------------------------------------------------------
# F-4: kill switch is read once at config load, not immediately before mutation
# ---------------------------------------------------------------------------

def test_legacy_kill_switch_is_frozen_at_config_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-4 (HIGH). ``BackendServices.__init__`` calls ``ExecutionConfig.from_environment()``
    once; ``ExecutionGate._configuration_gate`` then reads the frozen dataclass forever.
    Flipping ``ALPACA_EXECUTION_KILL_SWITCH=true`` on a running process has no effect.
    E4 requires the switch to be read immediately before the mutation, every time.
    """

    monkeypatch.setenv("ALPACA_EXECUTION_KILL_SWITCH", "false")
    monkeypatch.setenv("ALPACA_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("ALPACA_EXECUTION_DRY_RUN", "true")
    config = ExecutionConfig.from_environment()
    gate = ExecutionGate(config)
    assert config.kill_switch is False

    monkeypatch.setenv("ALPACA_EXECUTION_KILL_SWITCH", "true")
    assert ExecutionConfig.from_environment().kill_switch is True, "the environment did change"

    # The live gate still believes the switch is off.
    gate._configuration_gate()  # does not raise
    assert gate.config.kill_switch is False
