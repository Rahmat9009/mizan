from __future__ import annotations

from dataclasses import dataclass

from app.models import MarketRiskSnapshot, PortfolioSnapshot, RiskReport, RiskRuleResult, Side, TradeProposal


@dataclass(frozen=True)
class RiskPolicy:
    max_position_pct: float = 0.20
    max_trade_pct: float = 0.10
    max_daily_drawdown_pct: float = 0.03
    max_symbol_volatility: float = 0.80
    min_liquidity_score: float = 0.35
    min_strategy_confidence: float = 0.55


class RiskEngine:
    def __init__(self, policy: RiskPolicy | None = None):
        self.policy = policy or RiskPolicy()

    def evaluate(self, proposal: TradeProposal, portfolio: PortfolioSnapshot, market: MarketRiskSnapshot) -> RiskReport:
        checks: list[RiskRuleResult] = []
        reasons: list[str] = []
        recommended_qty = proposal.quantity
        blocked = False
        score = 0

        confidence_ok = proposal.strategy_confidence >= self.policy.min_strategy_confidence
        checks.append(RiskRuleResult(
            rule="strategy_confidence",
            passed=confidence_ok,
            severity="BLOCK" if not confidence_ok else "INFO",
            message=f"Confidence {proposal.strategy_confidence:.2f}; minimum {self.policy.min_strategy_confidence:.2f}.",
        ))
        if not confidence_ok:
            blocked = True
            score += 30
            reasons.append("Strategy confidence is below policy minimum.")

        if portfolio.daily_pnl_pct is None:
            blocked = True
            score += 35
            reasons.append("Portfolio daily P&L is unavailable; drawdown policy cannot be verified.")
            checks.append(RiskRuleResult(
                rule="daily_drawdown",
                passed=False,
                severity="BLOCK",
                message="Daily P&L is unavailable; fail-closed policy requires rejection.",
            ))
        else:
            drawdown_ok = portfolio.daily_pnl_pct >= -self.policy.max_daily_drawdown_pct
            checks.append(RiskRuleResult(
                rule="daily_drawdown",
                passed=drawdown_ok,
                severity="BLOCK" if not drawdown_ok else "INFO",
                message=f"Daily P&L {portfolio.daily_pnl_pct:.2%}; floor {-self.policy.max_daily_drawdown_pct:.2%}.",
            ))
            if not drawdown_ok:
                blocked = True
                score += 35
                reasons.append("Portfolio daily drawdown limit has been breached.")

        if proposal.side == Side.BUY:
            buying_power_ok = proposal.notional <= portfolio.buying_power
            checks.append(RiskRuleResult(
                rule="buying_power",
                passed=buying_power_ok,
                severity="BLOCK" if not buying_power_ok else "INFO",
                message=f"Trade notional ${proposal.notional:,.2f}; buying power ${portfolio.buying_power:,.2f}.",
            ))
            if not buying_power_ok:
                blocked = True
                score += 35
                reasons.append("Trade exceeds available buying power.")

            max_trade_value = portfolio.equity * self.policy.max_trade_pct
            if proposal.notional > max_trade_value:
                resized = max(int(max_trade_value // proposal.estimated_price), 0)
                recommended_qty = min(recommended_qty, resized)
                score += 15
                reasons.append("Trade size exceeds max trade allocation.")
                checks.append(RiskRuleResult(
                    rule="max_trade_size",
                    passed=False,
                    severity="HIGH",
                    message=f"Trade would use {proposal.notional / portfolio.equity:.2%} of equity; max is {self.policy.max_trade_pct:.2%}.",
                    recommended_quantity=resized,
                ))
            else:
                checks.append(RiskRuleResult(
                    rule="max_trade_size",
                    passed=True,
                    severity="INFO",
                    message="Trade size is within allocation policy.",
                ))

            current_value = portfolio.current_positions.get(proposal.symbol, 0.0)
            max_position_value = portfolio.equity * self.policy.max_position_pct
            room = max(max_position_value - current_value, 0.0)
            concentration_qty = max(int(room // proposal.estimated_price), 0)

            if proposal.notional + current_value > max_position_value:
                recommended_qty = min(recommended_qty, concentration_qty)
                score += 20
                reasons.append("Proposed order would exceed concentration limit.")
                checks.append(RiskRuleResult(
                    rule="position_concentration",
                    passed=False,
                    severity="HIGH",
                    message=f"Position would exceed {self.policy.max_position_pct:.2%} of equity.",
                    recommended_quantity=concentration_qty,
                ))
            else:
                checks.append(RiskRuleResult(
                    rule="position_concentration",
                    passed=True,
                    severity="INFO",
                    message="Position concentration is within policy.",
                ))

        volatility_ok = market.annualized_volatility <= self.policy.max_symbol_volatility
        checks.append(RiskRuleResult(
            rule="volatility",
            passed=volatility_ok,
            severity="HIGH" if not volatility_ok else "INFO",
            message=f"Annualized volatility {market.annualized_volatility:.2%}; limit {self.policy.max_symbol_volatility:.2%}.",
        ))
        if not volatility_ok:
            score += 20
            reasons.append("Symbol volatility exceeds risk threshold.")
            recommended_qty = max(recommended_qty // 2, 0)

        liquidity_ok = market.liquidity_score >= self.policy.min_liquidity_score
        checks.append(RiskRuleResult(
            rule="liquidity",
            passed=liquidity_ok,
            severity="BLOCK" if not liquidity_ok else "INFO",
            message=f"Liquidity score {market.liquidity_score:.2f}; minimum {self.policy.min_liquidity_score:.2f}.",
        ))
        if not liquidity_ok:
            blocked = True
            score += 30
            reasons.append("Liquidity is below execution safety threshold.")

        score = min(score, 100)

        if recommended_qty <= 0:
            blocked = True
            reasons.append("Risk-adjusted position size is zero.")

        if not reasons:
            reasons.append("Proposal is within current portfolio risk policy.")

        return RiskReport(
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            original_quantity=proposal.quantity,
            recommended_quantity=max(recommended_qty, 0),
            blocked=blocked,
            risk_score=score,
            reasons=reasons,
            checks=checks,
        )
