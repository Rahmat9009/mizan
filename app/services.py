from __future__ import annotations

from typing import Any, Callable

from app.ai_risk import AIRiskProvider
from app.alpaca.execution import PaperExecutionAlpacaAdapter
from app.alpaca.portfolio import AlpacaPortfolioProvider
from app.audit import SQLiteAuditLog
from app.execution.gate import ExecutionConfig
from app.execution.models import ExecutionResult, ExecutionState
from app.execution.reconciliation import (
    OrderNotFoundError,
    OrderReconciliationError,
    OrderReconciliationService,
)
from app.execution.service import ControlledPaperExecutionService
from app.market_risk import SuppliedMarketRiskProvider
from app.models import MarketRiskSnapshot, PortfolioSnapshot, TradeProposal
from app.persistence.database import Database
from app.persistence.repositories import LifecycleRepository, PersistenceError
from app.pipeline import DecisionPipeline
from app.providers.featherless_risk import FeatherlessRiskProvider


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class BackendServices:
    """Application service layer shared by HTTP routes and lifecycle demos."""

    def __init__(
        self,
        *,
        database: Database | None = None,
        repository: LifecycleRepository | None = None,
        ai_provider: AIRiskProvider | None = None,
        portfolio_provider: AlpacaPortfolioProvider | Any | None = None,
        execution_config: ExecutionConfig | None = None,
        adapter_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.database = database or Database()
        self.repository = repository or LifecycleRepository(self.database)
        self.audit = SQLiteAuditLog(self.repository)
        self.ai_provider = ai_provider or FeatherlessRiskProvider()
        # Keep broker initialization lazy so /health and durable read endpoints
        # remain available even when credentials are absent or Alpaca is down.
        self.portfolio_provider = portfolio_provider
        self.execution_config = execution_config or ExecutionConfig.from_environment()
        self.adapter_factory = adapter_factory or PaperExecutionAlpacaAdapter.from_environment
        self.pipeline = DecisionPipeline(
            self.ai_provider,
            audit=self.audit,
            repository=self.repository,
        )
        self.execution = ControlledPaperExecutionService(
            self.execution_config,
            audit=self.audit,
            repository=self.repository,
            adapter_factory=self.adapter_factory,
        )
        self.reconciliation = OrderReconciliationService(
            self.repository,
            audit=self.audit,
            adapter_factory=self.adapter_factory,
        )

    @property
    def ai_provider_name(self) -> str:
        if isinstance(self.ai_provider, FeatherlessRiskProvider):
            return "FEATHERLESS"
        return type(self.ai_provider).__name__

    def portfolio(self) -> PortfolioSnapshot:
        try:
            provider = self.portfolio_provider or AlpacaPortfolioProvider()
            return provider.get_snapshot()
        except Exception as exc:
            raise ServiceError(
                "BROKER_UNAVAILABLE",
                f"Alpaca PAPER portfolio is unavailable ({type(exc).__name__}).",
                503,
            ) from exc

    def evaluate(
        self,
        proposal: TradeProposal,
        market_risk: MarketRiskSnapshot,
    ) -> dict[str, Any]:
        try:
            market = SuppliedMarketRiskProvider(market_risk).get_snapshot(proposal.symbol)
            portfolio = self.portfolio()
            risk, ai, governor = self.pipeline.run(proposal, portfolio, market)
            return {
                "proposal": proposal,
                "risk_report": risk,
                "ai_risk_analysis": ai,
                "governor_decision": governor,
            }
        except ServiceError:
            raise
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc
        except ValueError as exc:
            raise ServiceError("INVALID_MARKET_RISK", str(exc), 422) from exc
        except Exception as exc:
            raise ServiceError(
                "AI_UNAVAILABLE",
                f"Proposal evaluation failed safely ({type(exc).__name__}).",
                503,
            ) from exc

    def execute_proposal(self, proposal_id: str) -> ExecutionResult:
        try:
            lifecycle = self.repository.lifecycle(proposal_id)
            if lifecycle is None:
                raise ServiceError("PROPOSAL_NOT_FOUND", "Proposal was not found.", 404)

            # Restart-safe fast path: a durable broker order can only be read/reconciled.
            # No new authorization or mutation is attempted for an already-known order.
            known_order = lifecycle["broker_order"]
            if known_order is not None:
                try:
                    known_order = self.reconciliation.reconcile(
                        client_order_id=known_order.client_order_id
                    )
                except OrderReconciliationError:
                    pass
                if not float(known_order.quantity).is_integer():
                    raise ServiceError(
                        "DATABASE_ERROR", "Persisted order quantity is not an integer.", 503
                    )
                result = ExecutionResult(
                    proposal_id=proposal_id,
                    client_order_id=known_order.client_order_id,
                    alpaca_order_id=known_order.alpaca_order_id,
                    symbol=known_order.symbol,
                    side=known_order.side,
                    quantity=int(known_order.quantity),
                    status=ExecutionState.RECONCILED_EXISTING_ORDER,
                    submitted_at=known_order.submitted_at,
                    filled_at=known_order.filled_at,
                    filled_quantity=known_order.filled_quantity,
                    filled_avg_price=known_order.filled_avg_price,
                    broker_status=known_order.broker_status,
                    execution_mode=self.execution_config.mode,
                    message="Existing Alpaca PAPER order reconciled after durable-state lookup; no duplicate was submitted.",
                )
                self.repository.save_execution_result(result)
                return result

            required = ("risk_report", "governor_decision", "market_risk_snapshot")
            if any(lifecycle[name] is None for name in required):
                raise ServiceError(
                    "DATABASE_ERROR", "Persisted proposal lifecycle is incomplete.", 503
                )
            return self.execution.execute(
                lifecycle["proposal"],
                lifecycle["risk_report"],
                lifecycle["governor_decision"],
                lifecycle["market_risk_snapshot"],
            )
        except ServiceError:
            raise
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc

    def lifecycle(self, proposal_id: str) -> dict[str, Any]:
        try:
            value = self.repository.lifecycle(proposal_id)
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc
        if value is None:
            raise ServiceError("PROPOSAL_NOT_FOUND", "Proposal was not found.", 404)
        return value

    def audit_events(self, proposal_id: str) -> list[Any]:
        self.lifecycle(proposal_id)
        try:
            return self.audit.list_for_proposal(proposal_id)
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc

    def order(self, client_order_id: str, *, reconcile: bool) -> Any:
        try:
            persisted = self.repository.get_broker_order(client_order_id)
            if persisted is None:
                raise ServiceError("ORDER_NOT_FOUND", "Order was not found.", 404)
            if not reconcile:
                return persisted
            try:
                return self.reconciliation.reconcile(client_order_id=client_order_id)
            except OrderNotFoundError as exc:
                raise ServiceError("ORDER_NOT_FOUND", str(exc), 404) from exc
            except OrderReconciliationError as exc:
                raise ServiceError("BROKER_UNAVAILABLE", str(exc), 503) from exc
        except ServiceError:
            raise
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc

    def recent(self, limit: int) -> list[dict[str, Any]]:
        try:
            return self.repository.recent(limit)
        except PersistenceError as exc:
            raise ServiceError("DATABASE_ERROR", str(exc), 503) from exc
