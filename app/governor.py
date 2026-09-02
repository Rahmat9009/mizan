from __future__ import annotations

from app.models import AIRiskAnalysis, Decision, GovernorDecision, RiskReport, TradeProposal


class PortfolioGovernor:
    """
    AI may only make the final result equally or more conservative.
    It can never bypass a hard block or increase quantity above hard-policy sizing.
    """

    def decide(
        self,
        proposal: TradeProposal,
        hard_risk: RiskReport,
        ai_risk: AIRiskAnalysis,
    ) -> GovernorDecision:
        if hard_risk.proposal_id != proposal.proposal_id or ai_risk.proposal_id != proposal.proposal_id:
            return GovernorDecision(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                decision=Decision.REJECT,
                original_quantity=proposal.quantity,
                approved_quantity=0,
                risk_score=hard_risk.risk_score,
                reason="Risk analysis identity mismatch; fail-closed policy requires rejection.",
            )

        if hard_risk.blocked:
            return GovernorDecision(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                decision=Decision.REJECT,
                original_quantity=proposal.quantity,
                approved_quantity=0,
                risk_score=hard_risk.risk_score,
                reason="Hard policy rejection: " + "; ".join(hard_risk.reasons),
            )

        hard_cap = min(proposal.quantity, hard_risk.recommended_quantity)

        if ai_risk.recommendation == Decision.REJECT:
            return GovernorDecision(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                decision=Decision.REJECT,
                original_quantity=proposal.quantity,
                approved_quantity=0,
                risk_score=hard_risk.risk_score,
                reason="AI risk agent recommends rejection: " + " ".join(ai_risk.reasoning),
            )

        approved_quantity = max(min(ai_risk.recommended_quantity, hard_cap), 0)

        if approved_quantity <= 0:
            return GovernorDecision(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                decision=Decision.REJECT,
                original_quantity=proposal.quantity,
                approved_quantity=0,
                risk_score=hard_risk.risk_score,
                reason="Combined risk controls reduce permitted quantity to zero.",
            )

        if approved_quantity < proposal.quantity:
            return GovernorDecision(
                proposal_id=proposal.proposal_id,
                symbol=proposal.symbol,
                side=proposal.side,
                decision=Decision.REDUCE,
                original_quantity=proposal.quantity,
                approved_quantity=approved_quantity,
                risk_score=hard_risk.risk_score,
                reason="Trade size reduced by combined hard-policy and AI risk review. " + " ".join(ai_risk.reasoning),
            )

        return GovernorDecision(
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            decision=Decision.APPROVE,
            original_quantity=proposal.quantity,
            approved_quantity=proposal.quantity,
            risk_score=hard_risk.risk_score,
            reason="Hard policy passed and AI risk review supports the proposed trade.",
        )
