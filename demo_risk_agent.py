from __future__ import annotations

import json
import os

from dotenv import load_dotenv

from app.ai_risk import MockAIRiskProvider
from app.models import MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.pipeline import DecisionPipeline
from app.providers.featherless_risk import FeatherlessRiskProvider


def print_section(title: str, value: object) -> None:
    print(f"\n{title}")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    print(json.dumps(value, indent=2))


def main() -> None:
    load_dotenv()
    if os.getenv("FEATHERLESS_API_KEY", "").strip():
        provider = FeatherlessRiskProvider()
        provider_name = "FEATHERLESS"
    else:
        provider = MockAIRiskProvider()
        provider_name = "MOCK"

    proposal = TradeProposal(
        symbol="NVDA",
        side="BUY",
        quantity=40,
        estimated_price=180.0,
        strategy_confidence=0.76,
        thesis="AI infrastructure demand supports continued earnings growth.",
        invalidation_condition="Revenue growth or data-center margins fall below the strategy threshold.",
    )
    portfolio = PortfolioSnapshot(
        equity=100_000.0,
        cash=45_000.0,
        buying_power=45_000.0,
        daily_pnl_pct=-0.012,
        current_positions={"NVDA": 9_000.0, "AMD": 7_500.0, "MSFT": 8_000.0},
    )
    market = MarketRiskSnapshot(
        symbol="NVDA",
        annualized_volatility=0.62,
        max_drawdown_30d=0.18,
        liquidity_score=0.96,
    )

    print("Portfolio source: FICTIONAL_DEMO")
    print(f"AI provider: {provider_name}")
    print("Execution: DISABLED")
    pipeline = DecisionPipeline(ai_provider=provider)
    hard_risk, ai_risk, decision = pipeline.run(proposal, portfolio, market)

    print_section("Deterministic risk result", hard_risk)
    print_section("AI risk result", ai_risk)
    print_section("Governor decision", decision)
    print_section(
        "Audit events",
        [event.model_dump(mode="json") for event in pipeline.audit.list_for_proposal(proposal.proposal_id)],
    )


if __name__ == "__main__":
    main()
