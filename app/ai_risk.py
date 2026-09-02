from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import AIRiskAnalysis, Decision, MarketRiskSnapshot, PortfolioSnapshot, RiskReport, TradeProposal


class AIRiskProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        market: MarketRiskSnapshot,
        hard_risk: RiskReport,
    ) -> AIRiskAnalysis:
        raise NotImplementedError


class MockAIRiskProvider(AIRiskProvider):
    """
    Offline deterministic stand-in for a real LLM.
    """

    def analyze(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        market: MarketRiskSnapshot,
        hard_risk: RiskReport,
    ) -> AIRiskAnalysis:
        hidden_risks: list[str] = []
        reasoning: list[str] = []

        recommendation = Decision.APPROVE
        recommended_quantity = hard_risk.recommended_quantity
        confidence = 0.80

        if hard_risk.blocked:
            recommendation = Decision.REJECT
            recommended_quantity = 0
            confidence = 0.98
            reasoning.append("Hard risk policy contains a blocking condition.")
        else:
            if market.max_drawdown_30d >= 0.20:
                hidden_risks.append("Recent 30-day drawdown is elevated.")
                reasoning.append("Recent downside behavior suggests position-size caution.")
                recommendation = Decision.REDUCE
                recommended_quantity = max(min(recommended_quantity, proposal.quantity // 2), 1)
                confidence = 0.86

            if proposal.strategy_confidence < 0.70:
                hidden_risks.append("Strategy confidence is only moderately above the policy floor.")
                reasoning.append("The trade thesis may not justify full-size exposure.")
                recommendation = Decision.REDUCE
                recommended_quantity = max(min(recommended_quantity, proposal.quantity // 2), 1)
                confidence = max(confidence, 0.84)

            if market.annualized_volatility > 0.60:
                hidden_risks.append("Volatility is elevated even though it remains below the hard limit.")
                reasoning.append("Higher volatility raises sizing and stop-loss sensitivity.")
                recommendation = Decision.REDUCE
                recommended_quantity = max(min(recommended_quantity, proposal.quantity // 2), 1)

        if not hidden_risks:
            hidden_risks.append("No additional material contextual risks detected by the mock AI layer.")

        if not reasoning:
            reasoning.append("Hard policy and contextual signals support the proposed sizing.")

        return AIRiskAnalysis(
            proposal_id=proposal.proposal_id,
            recommendation=recommendation,
            confidence=confidence,
            recommended_quantity=recommended_quantity,
            risk_thesis="Contextual AI review after deterministic risk-policy evaluation.",
            hidden_risks=hidden_risks,
            reasoning=reasoning,
            model_name="mock-risk-model-v1",
        )
