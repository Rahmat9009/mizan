from app.models import (
    Decision,
    MarketRiskSnapshot,
    PortfolioSnapshot,
    TradeProposal,
)
from app.pipeline import DecisionPipeline


def base_portfolio():
    return PortfolioSnapshot(
        equity=100_000,
        cash=70_000,
        buying_power=70_000,
        daily_pnl_pct=-0.005,
        current_positions={"AAPL": 5_000},
    )


def base_market():
    return MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=0.30,
        max_drawdown_30d=0.10,
        liquidity_score=0.90,
    )


def test_approve_safe_trade():
    pipeline = DecisionPipeline()
    proposal = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=10,
        estimated_price=200,
        strategy_confidence=0.80,
        thesis="Strong signal.",
        invalidation_condition="Signal reverses.",
    )

    report, _, decision = pipeline.run(
        proposal,
        base_portfolio(),
        base_market(),
    )

    assert report.blocked is False
    assert decision.decision == Decision.APPROVE
    assert decision.approved_quantity == 10
    assert len(pipeline.audit.list_for_proposal(proposal.proposal_id)) == 3


def test_reduce_oversized_trade():
    pipeline = DecisionPipeline()
    proposal = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=80,
        estimated_price=200,
        strategy_confidence=0.85,
        thesis="Strong signal.",
        invalidation_condition="Signal reverses.",
    )

    report, _, decision = pipeline.run(
        proposal,
        base_portfolio(),
        base_market(),
    )

    assert report.blocked is False
    assert decision.decision == Decision.REDUCE
    assert decision.approved_quantity < proposal.quantity


def test_reject_when_drawdown_limit_breached():
    pipeline = DecisionPipeline()

    portfolio = base_portfolio().model_copy(
        update={"daily_pnl_pct": -0.05}
    )

    proposal = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=10,
        estimated_price=200,
        strategy_confidence=0.90,
        thesis="Strong signal.",
        invalidation_condition="Signal reverses.",
    )

    report, _, decision = pipeline.run(
        proposal,
        portfolio,
        base_market(),
    )

    assert report.blocked is True
    assert decision.decision == Decision.REJECT
    assert decision.approved_quantity == 0
