from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from app.ai_risk import MockAIRiskProvider
from app.alpaca import AlpacaConfigurationError, AlpacaPortfolioError, AlpacaPortfolioProvider
from app.models import MarketRiskSnapshot, TradeProposal
from app.pipeline import DecisionPipeline
from app.providers.featherless_risk import FeatherlessRiskProvider


def print_section(title: str, value: object) -> None:
    print(f"\n{title}")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    print(json.dumps(value, indent=2))


def main() -> int:
    load_dotenv()
    print("Portfolio source: ALPACA_PAPER")
    print("Execution: DISABLED")

    if os.getenv("FEATHERLESS_API_KEY", "").strip():
        ai_provider = FeatherlessRiskProvider()
        provider_name = "FEATHERLESS"
    else:
        ai_provider = MockAIRiskProvider()
        provider_name = "MOCK"
    print(f"AI provider: {provider_name}")

    try:
        portfolio = AlpacaPortfolioProvider().get_snapshot()
    except (AlpacaConfigurationError, AlpacaPortfolioError) as exc:
        print(f"Portfolio retrieval failed safely: {exc}")
        print("No fabricated portfolio was substituted.")
        return 1

    proposal = TradeProposal(
        symbol="NVDA",
        side="BUY",
        quantity=10,
        estimated_price=180.0,
        strategy_confidence=0.76,
        thesis="Fictional demo thesis: supplied research supports a limited NVDA allocation.",
        invalidation_condition="Fictional demo invalidation: the upstream research score falls below threshold.",
    )
    market = MarketRiskSnapshot(
        symbol="NVDA",
        annualized_volatility=0.62,
        max_drawdown_30d=0.18,
        liquidity_score=0.96,
    )

    pipeline = DecisionPipeline(ai_provider=ai_provider)
    hard_risk, ai_risk, decision = pipeline.run(proposal, portfolio, market)

    print_section("Read-only portfolio", portfolio)
    print_section("Deterministic risk result", hard_risk)
    print_section("AI risk result", ai_risk)
    print_section("Governor decision", decision)
    print_section(
        "Audit events",
        [event.model_dump(mode="json") for event in pipeline.audit.list_for_proposal(proposal.proposal_id)],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
