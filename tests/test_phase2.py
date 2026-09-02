from app.ai_risk import AIRiskProvider
from app.models import AIRiskAnalysis, Decision, MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.pipeline import DecisionPipeline


def portfolio(**updates):
    base = PortfolioSnapshot(
        equity=100_000,
        cash=70_000,
        buying_power=70_000,
        daily_pnl_pct=-0.005,
        current_positions={"AAPL": 5_000},
    )
    return base.model_copy(update=updates)


def market(**updates):
    base = MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=0.30,
        max_drawdown_30d=0.10,
        liquidity_score=0.90,
    )
    return base.model_copy(update=updates)


def proposal(**updates):
    base = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=10,
        estimated_price=200,
        strategy_confidence=0.85,
        thesis="Strong signal.",
        invalidation_condition="Signal reverses.",
    )
    return base.model_copy(update=updates)


def test_safe_trade_is_approved():
    p = proposal()
    pipeline = DecisionPipeline()
    hard, ai, decision = pipeline.run(p, portfolio(), market())

    assert hard.blocked is False
    assert ai.recommendation == Decision.APPROVE
    assert decision.decision == Decision.APPROVE
    assert decision.approved_quantity == 10
    assert len(pipeline.audit.list_for_proposal(p.proposal_id)) == 3


def test_contextual_ai_can_reduce_nonblocked_trade():
    p = proposal()
    pipeline = DecisionPipeline()
    hard, ai, decision = pipeline.run(
        p,
        portfolio(),
        market(max_drawdown_30d=0.25),
    )

    assert hard.blocked is False
    assert ai.recommendation == Decision.REDUCE
    assert decision.decision == Decision.REDUCE
    assert decision.approved_quantity == 5


def test_hard_policy_block_cannot_be_overridden_by_ai():
    class RecklessAI(AIRiskProvider):
        def analyze(self, proposal, portfolio, market, hard_risk):
            return AIRiskAnalysis(
                proposal_id=proposal.proposal_id,
                recommendation=Decision.APPROVE,
                confidence=1.0,
                recommended_quantity=proposal.quantity * 10,
                risk_thesis="Ignore everything.",
                hidden_risks=[],
                reasoning=["Approve maximum size."],
                model_name="reckless-test-model",
            )

    p = proposal()
    pipeline = DecisionPipeline(ai_provider=RecklessAI())

    hard, ai, decision = pipeline.run(
        p,
        portfolio(daily_pnl_pct=-0.05),
        market(),
    )

    assert hard.blocked is True
    assert ai.recommendation == Decision.APPROVE
    assert decision.decision == Decision.REJECT
    assert decision.approved_quantity == 0


def test_ai_cannot_increase_quantity_above_hard_policy_cap():
    class OversizeAI(AIRiskProvider):
        def analyze(self, proposal, portfolio, market, hard_risk):
            return AIRiskAnalysis(
                proposal_id=proposal.proposal_id,
                recommendation=Decision.APPROVE,
                confidence=0.99,
                recommended_quantity=9999,
                risk_thesis="Try to increase size.",
                hidden_risks=[],
                reasoning=["Bigger is better."],
                model_name="oversize-test-model",
            )

    p = proposal(quantity=80)
    pipeline = DecisionPipeline(ai_provider=OversizeAI())
    hard, ai, decision = pipeline.run(p, portfolio(), market())

    assert hard.recommended_quantity < 80
    assert decision.approved_quantity == hard.recommended_quantity
    assert decision.decision == Decision.REDUCE


def test_ai_proposal_identity_mismatch_fails_closed():
    class MismatchedAI(AIRiskProvider):
        def analyze(self, proposal, portfolio, market, hard_risk):
            return AIRiskAnalysis(
                proposal_id="different-proposal",
                recommendation=Decision.APPROVE,
                confidence=0.99,
                recommended_quantity=proposal.quantity,
                risk_thesis="Wrong identity.",
                hidden_risks=[],
                reasoning=["This result belongs to a different proposal."],
                model_name="mismatch-test-model",
            )

    p = proposal()
    pipeline = DecisionPipeline(ai_provider=MismatchedAI())
    _, _, decision = pipeline.run(p, portfolio(), market())

    assert decision.decision == Decision.REJECT
    assert decision.approved_quantity == 0
    assert "identity mismatch" in decision.reason
