from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

from app.ai_risk import MockAIRiskProvider
from app.alpaca import AlpacaConfigurationError, AlpacaPortfolioError, AlpacaPortfolioProvider
from app.execution.gate import ExecutionConfig, ExecutionConfigurationError
from app.execution.models import ExecutionState
from app.execution.service import ControlledPaperExecutionService
from app.models import MarketRiskSnapshot, TradeProposal
from app.pipeline import DecisionPipeline
from app.providers.featherless_risk import FeatherlessRiskProvider


def _print_json(title: str, value: object) -> None:
    print(f"\n{title}")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    print(json.dumps(value, indent=2))


def _demo_price() -> float:
    raw = os.getenv("DEMO_ESTIMATED_PRICE", "250.00").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("DEMO_ESTIMATED_PRICE must be numeric.") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("DEMO_ESTIMATED_PRICE must be a finite positive number.")
    return float(value)


def main() -> int:
    load_dotenv()
    try:
        config = ExecutionConfig.from_environment()
    except ExecutionConfigurationError as exc:
        print(f"Execution configuration rejected safely: {exc}")
        print("NO ORDER SUBMITTED")
        return 1

    print("Mode: ALPACA PAPER")
    print(f"Execution enabled: {str(config.enabled).lower()}")
    print(f"Dry run: {str(config.dry_run).lower()}")
    print(f"Kill switch: {str(config.kill_switch).lower()}")
    print("Live trading supported: false")

    if not config.paper:
        print("\nExecution result: BLOCKED")
        print("ALPACA_PAPER must be true; live trading is unsupported.")
        print("NO ORDER SUBMITTED")
        return 1

    try:
        portfolio = AlpacaPortfolioProvider().get_snapshot()
    except (AlpacaConfigurationError, AlpacaPortfolioError) as exc:
        print(f"\nPortfolio retrieval failed safely: {exc}")
        print("NO ORDER SUBMITTED")
        return 1

    symbol = os.getenv("DEMO_SYMBOL", "AAPL").strip().upper()
    try:
        proposal = TradeProposal(
            symbol=symbol,
            side="BUY",
            quantity=1,
            estimated_price=_demo_price(),
            strategy_confidence=0.90,
            thesis="Fictional Phase 5 protocol demo for one ordinary liquid US equity share.",
            invalidation_condition="Do not proceed if any risk or execution safety gate fails.",
        )
    except Exception as exc:
        print(f"\nDemo proposal rejected safely ({type(exc).__name__}).")
        print("NO ORDER SUBMITTED")
        return 1

    market = MarketRiskSnapshot(
        symbol=symbol,
        annualized_volatility=0.30,
        max_drawdown_30d=0.10,
        liquidity_score=0.95,
    )

    if os.getenv("FEATHERLESS_API_KEY", "").strip():
        ai_provider = FeatherlessRiskProvider()
        provider_name = "FEATHERLESS"
    else:
        ai_provider = MockAIRiskProvider()
        provider_name = "MOCK"

    pipeline = DecisionPipeline(ai_provider=ai_provider)
    hard_risk, ai_risk, governor = pipeline.run(proposal, portfolio, market)
    print(f"AI provider: {provider_name}")
    _print_json("Trade proposal", proposal)
    _print_json("Deterministic risk", hard_risk)
    _print_json("AI risk", ai_risk)
    _print_json("Governor decision", governor)

    execution = ControlledPaperExecutionService(config, audit=pipeline.audit)
    result = execution.execute(proposal, hard_risk, governor, market)
    _print_json("Execution result", result)

    timeline = [
        {
            "created_at": event.created_at.isoformat(),
            "actor": event.actor,
            "action": event.action,
            "payload": event.payload,
        }
        for event in pipeline.audit.list_for_proposal(proposal.proposal_id)
    ]
    _print_json("Audit timeline", timeline)

    if result.status == ExecutionState.SUBMITTED:
        print("\nPAPER ORDER SUBMITTED")
    elif result.status == ExecutionState.WOULD_SUBMIT:
        print("\nWOULD_SUBMIT only — DRY RUN, NO ORDER SUBMITTED")
    elif result.status == ExecutionState.RECONCILED_EXISTING_ORDER:
        print("\nEXISTING PAPER ORDER RECONCILED — NO DUPLICATE SUBMITTED")
    else:
        print("\nNO ORDER SUBMITTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
