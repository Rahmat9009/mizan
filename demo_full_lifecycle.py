from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from pydantic import BaseModel

from app.execution.gate import ExecutionConfigurationError
from app.execution.models import ExecutionState
from app.models import MarketRiskSnapshot, TradeProposal
from app.services import BackendServices, ServiceError


def _number(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric.") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")
    return float(value)


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _print(title: str, value: object) -> None:
    value = _jsonable(value)
    print(f"\n{title}")
    print(json.dumps(value, indent=2, default=str))


def main() -> int:
    load_dotenv()
    print("Portfolio Governor full lifecycle demo")
    print("Safety: Alpaca PAPER only; LIVE TRADING IS NOT SUPPORTED.")
    print("Market risk source: MANUAL_DEMO_INPUT (not a live market-data feed).")

    try:
        services = BackendServices()
        proposal = TradeProposal(
            proposal_id=os.getenv("DEMO_PROPOSAL_ID", "demo-full-lifecycle-aapl-buy-1"),
            symbol=os.getenv("DEMO_SYMBOL", "AAPL").strip().upper(),
            side="BUY",
            quantity=1,
            estimated_price=_number("DEMO_ESTIMATED_PRICE", "250.00"),
            strategy_confidence=_number("DEMO_STRATEGY_CONFIDENCE", "0.82"),
            thesis="Hackathon one-share PAPER lifecycle demonstration.",
            invalidation_condition="Upstream strategy signal reverses.",
        )
        market = MarketRiskSnapshot(
            symbol=proposal.symbol,
            annualized_volatility=_number("DEMO_ANNUALIZED_VOLATILITY", "0.30"),
            max_drawdown_30d=_number("DEMO_MAX_DRAWDOWN_30D", "0.10"),
            liquidity_score=_number("DEMO_LIQUIDITY_SCORE", "0.95"),
        )

        print("\nPhase A — evaluate proposal")
        existing = services.repository.lifecycle(proposal.proposal_id)
        if existing is None:
            evaluation = services.evaluate(proposal, market)
        else:
            evaluation = {
                key: existing[key]
                for key in (
                    "proposal",
                    "risk_report",
                    "ai_risk_analysis",
                    "governor_decision",
                )
            }
            print("Existing durable evaluation loaded; it was not overwritten.")
        _print("Evaluation", evaluation)

        print("\nPhase B — verify persistence")
        _print("Durable lifecycle", services.lifecycle(proposal.proposal_id))

        print("\nPhase C — execute or dry-run through every safety gate")
        execution = services.execute_proposal(proposal.proposal_id)
        _print("Execution result", execution)

        print("\nPhase D — bounded REST reconciliation")
        if execution.client_order_id and execution.status in {
            ExecutionState.SUBMITTED,
            ExecutionState.RECONCILED_EXISTING_ORDER,
        }:
            timeout = _number("ORDER_RECONCILE_TIMEOUT_SECONDS", "30")
            interval = _number("ORDER_RECONCILE_INTERVAL_SECONDS", "2")
            order = services.reconciliation.reconcile_until_terminal(
                execution.client_order_id,
                timeout_seconds=timeout,
                interval_seconds=interval,
            )
            _print("Current broker order", order)
        else:
            print("No broker order exists; reconciliation is not applicable.")

        print("\nPhase E — durable audit timeline")
        _print("Audit events", services.audit_events(proposal.proposal_id))
        print(f"\nDatabase: {services.database.path}")
        return 0
    except (ExecutionConfigurationError, ServiceError, ValueError) as exc:
        print(f"Demo stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
