from __future__ import annotations

from app.ai_risk import AIRiskProvider, MockAIRiskProvider
from app.audit import InMemoryAuditLog
from app.governor import PortfolioGovernor
from app.models import MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.risk_engine import RiskEngine
from app.audit import AuditLog
from app.persistence.repositories import LifecycleRepository


class DecisionPipeline:
    def __init__(
        self,
        ai_provider: AIRiskProvider | None = None,
        *,
        audit: AuditLog | None = None,
        repository: LifecycleRepository | None = None,
    ):
        self.risk = RiskEngine()
        self.ai = ai_provider or MockAIRiskProvider()
        self.governor = PortfolioGovernor()
        self.audit = audit or InMemoryAuditLog()
        self.repository = repository

    def run(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        market: MarketRiskSnapshot,
    ):
        if self.repository is not None:
            self.repository.save_proposal(proposal)
            self.repository.save_portfolio_snapshot(proposal.proposal_id, portfolio)
            self.repository.save_market_risk(proposal.proposal_id, market)

        hard_report = self.risk.evaluate(proposal, portfolio, market)
        if self.repository is not None:
            self.repository.save_risk_report(hard_report)
        self.audit.append_risk(hard_report)

        ai_analysis = self.ai.analyze(proposal, portfolio, market, hard_report)
        if self.repository is not None:
            self.repository.save_ai_risk_analysis(ai_analysis)
        self.audit.append_ai_risk(ai_analysis)

        decision = self.governor.decide(proposal, hard_report, ai_analysis)
        if self.repository is not None:
            self.repository.save_governor_decision(decision)
        self.audit.append_governor(decision)

        return hard_report, ai_analysis, decision
