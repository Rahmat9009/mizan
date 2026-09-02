from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.alpaca.execution import (
    AlpacaAssetNotFoundError,
    AlpacaExecutionError,
    PaperExecutionAlpacaAdapter,
)
from app.alpaca.portfolio import AlpacaPortfolioProvider
from app.audit import AuditLog, InMemoryAuditLog
from app.execution.gate import ExecutionBlocked, ExecutionConfig, ExecutionGate
from app.execution.models import (
    BrokerOrder,
    ExecutionAsset,
    ExecutionAuthorization,
    ExecutionResult,
    ExecutionState,
    IntendedPaperOrder,
    MarketClockSnapshot,
)
from app.execution.reconciliation import broker_order_snapshot
from app.models import GovernorDecision, MarketRiskSnapshot, RiskReport, Side, TradeProposal
from app.risk_engine import RiskEngine
from app.persistence.repositories import LifecycleRepository


class PaperExecutionAdapterProtocol(Protocol):
    @property
    def paper_mode_verified(self) -> bool: ...

    def get_account(self) -> Any: ...

    def get_all_positions(self) -> Any: ...

    def get_clock(self) -> MarketClockSnapshot: ...

    def get_asset(self, symbol: str) -> ExecutionAsset: ...

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def submit_market_order(self, intended: IntendedPaperOrder) -> BrokerOrder: ...


def deterministic_client_order_id(proposal_id: str) -> str:
    """One stable Alpaca ID per proposal, independent of retries or process restarts."""

    digest = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:40]
    return f"pgv5-{digest}"


class ControlledPaperExecutionService:
    def __init__(
        self,
        config: ExecutionConfig,
        *,
        audit: AuditLog | None = None,
        repository: LifecycleRepository | None = None,
        risk_engine: RiskEngine | None = None,
        adapter_factory: Callable[[], PaperExecutionAdapterProtocol] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.gate = ExecutionGate(config)
        self.audit = audit or InMemoryAuditLog()
        self.repository = repository
        self.risk_engine = risk_engine or RiskEngine()
        self.adapter_factory = adapter_factory or PaperExecutionAlpacaAdapter.from_environment
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        proposal: TradeProposal,
        hard_risk: RiskReport,
        governor: GovernorDecision,
        market: MarketRiskSnapshot,
        *,
        authorization: ExecutionAuthorization | None = None,
    ) -> ExecutionResult:
        result = self._execute(
            proposal,
            hard_risk,
            governor,
            market,
            authorization=authorization,
        )
        if self.repository is not None:
            self.repository.save_execution_result(result)
        return result

    def _execute(
        self,
        proposal: TradeProposal,
        hard_risk: RiskReport,
        governor: GovernorDecision,
        market: MarketRiskSnapshot,
        *,
        authorization: ExecutionAuthorization | None = None,
    ) -> ExecutionResult:
        if not isinstance(proposal, TradeProposal):
            raise TypeError("A valid TradeProposal is required for execution.")

        try:
            checked_now = self.now_provider()
            if authorization is None:
                authorization = self.gate.authorize(
                    proposal,
                    hard_risk,
                    governor,
                    market,
                    now=checked_now,
                )
            else:
                self.gate.validate_authorization(
                    authorization,
                    proposal,
                    hard_risk,
                    governor,
                    market,
                    now=checked_now,
                )
        except ExecutionBlocked as exc:
            return self._blocked_result(proposal, governor, exc.state, exc.message)

        self._audit(
            proposal.proposal_id,
            "EXECUTION_AUTHORIZATION_CREATED",
            authorization.model_dump(mode="json"),
        )
        if self.repository is not None:
            self.repository.save_execution_authorization(authorization)
        self._audit(
            proposal.proposal_id,
            "EXECUTION_GATE_PASSED",
            {
                "state": ExecutionState.AUTHORIZED.value,
                "approved_quantity": authorization.approved_quantity,
                "paper": True,
                "dry_run": self.config.dry_run,
            },
        )

        client_order_id = deterministic_client_order_id(proposal.proposal_id)
        intended = IntendedPaperOrder(
            proposal_id=proposal.proposal_id,
            client_order_id=client_order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=authorization.approved_quantity,
        )

        try:
            adapter = self.adapter_factory()
            if adapter.paper_mode_verified is not True:
                raise AlpacaExecutionError("Execution adapter did not verify paper mode.")
        except Exception as exc:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Paper execution adapter unavailable ({type(exc).__name__}).",
            )

        try:
            existing = adapter.find_order_by_client_id(client_order_id)
        except Exception as exc:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Idempotency lookup failed ({type(exc).__name__}).",
            )

        self._audit(
            proposal.proposal_id,
            "ORDER_IDEMPOTENCY_CHECKED",
            {
                "client_order_id": client_order_id,
                "existing_order_found": existing is not None,
                "paper": True,
            },
        )
        if existing is not None:
            mismatch = self._broker_order_mismatch(existing, intended)
            if mismatch:
                return self._failed_result(
                    proposal,
                    authorization.approved_quantity,
                    client_order_id,
                    f"Existing idempotent order does not match authorization: {mismatch}",
                )
            self._persist_broker_order(proposal.proposal_id, existing)
            self._audit_order("ORDER_RECONCILED", proposal.proposal_id, existing)
            return self._broker_result(
                proposal,
                existing,
                ExecutionState.RECONCILED_EXISTING_ORDER,
                "Existing Alpaca paper order reconciled; no duplicate was submitted.",
            )

        fresh_result = self._fresh_risk_check(adapter, proposal, authorization, market)
        if fresh_result is not None:
            return fresh_result

        try:
            asset = adapter.get_asset(proposal.symbol)
        except AlpacaAssetNotFoundError as exc:
            return self._blocked_result(
                proposal,
                governor,
                ExecutionState.ASSET_NOT_TRADABLE,
                str(exc),
                client_order_id=client_order_id,
            )
        except Exception as exc:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Asset validation failed ({type(exc).__name__}).",
            )

        if (
            asset.symbol != proposal.symbol
            or asset.asset_class != "us_equity"
            or asset.status != "active"
            or not asset.tradable
        ):
            return self._blocked_result(
                proposal,
                governor,
                ExecutionState.ASSET_NOT_TRADABLE,
                "Asset is not an active, tradable US equity.",
                client_order_id=client_order_id,
            )

        try:
            clock = adapter.get_clock()
        except Exception as exc:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Market clock lookup failed ({type(exc).__name__}).",
            )
        if not clock.is_open:
            return self._blocked_result(
                proposal,
                governor,
                ExecutionState.MARKET_CLOSED,
                "US equity market is closed; DAY market order was not submitted.",
                client_order_id=client_order_id,
            )

        # Network checks may consume authorization lifetime. Re-check immediately
        # before constructing a dry-run result or invoking the sole mutation.
        try:
            self.gate.validate_authorization(
                authorization,
                proposal,
                hard_risk,
                governor,
                market,
                now=self.now_provider(),
            )
        except ExecutionBlocked as exc:
            return self._blocked_result(
                proposal,
                governor,
                exc.state,
                exc.message,
                client_order_id=client_order_id,
            )

        if self.config.dry_run:
            self._audit(
                proposal.proposal_id,
                "ORDER_DRY_RUN_READY",
                {
                    **intended.model_dump(mode="json"),
                    "state": ExecutionState.WOULD_SUBMIT.value,
                    "paper": True,
                },
            )
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                client_order_id=client_order_id,
                symbol=proposal.symbol,
                side=proposal.side,
                quantity=authorization.approved_quantity,
                status=ExecutionState.WOULD_SUBMIT,
                execution_mode=self.config.mode,
                message="All execution gates passed; dry run prevented order submission.",
            )

        self._audit(
            proposal.proposal_id,
            "ORDER_SUBMITTING",
            {**intended.model_dump(mode="json"), "paper": True},
        )
        try:
            submitted = adapter.submit_market_order(intended)
        except Exception as exc:
            self._audit(
                proposal.proposal_id,
                "ORDER_SUBMISSION_FAILED",
                {
                    "client_order_id": client_order_id,
                    "error_type": type(exc).__name__,
                    "paper": True,
                },
            )
            # A timeout or duplicate-ID race may mean Alpaca accepted the order.
            try:
                reconciled = adapter.find_order_by_client_id(client_order_id)
            except Exception:
                reconciled = None
            if reconciled is not None and not self._broker_order_mismatch(reconciled, intended):
                self._persist_broker_order(proposal.proposal_id, reconciled)
                self._audit_order("ORDER_RECONCILED", proposal.proposal_id, reconciled)
                return self._broker_result(
                    proposal,
                    reconciled,
                    ExecutionState.RECONCILED_EXISTING_ORDER,
                    "Submission outcome reconciled using deterministic client order ID.",
                )
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Paper order submission failed ({type(exc).__name__}).",
                audit=False,
            )

        mismatch = self._broker_order_mismatch(submitted, intended)
        if mismatch:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                client_order_id,
                f"Submitted order response does not match authorization: {mismatch}",
            )
        self._persist_broker_order(proposal.proposal_id, submitted)
        self._audit_order("ORDER_SUBMITTED", proposal.proposal_id, submitted)
        return self._broker_result(
            proposal,
            submitted,
            ExecutionState.SUBMITTED,
            "Alpaca accepted the PAPER order; fill status must be reconciled separately.",
        )

    def _fresh_risk_check(
        self,
        adapter: PaperExecutionAdapterProtocol,
        proposal: TradeProposal,
        authorization: ExecutionAuthorization,
        market: MarketRiskSnapshot,
    ) -> ExecutionResult | None:
        try:
            fresh_portfolio = AlpacaPortfolioProvider(client=adapter).get_snapshot()
            fresh_risk = self.risk_engine.evaluate(proposal, fresh_portfolio, market)
        except Exception as exc:
            return self._failed_result(
                proposal,
                authorization.approved_quantity,
                deterministic_client_order_id(proposal.proposal_id),
                f"Fresh portfolio safety check failed ({type(exc).__name__}).",
            )

        self._audit(
            proposal.proposal_id,
            "EXECUTION_FRESH_RISK_EVALUATED",
            {
                "blocked": fresh_risk.blocked,
                "recommended_quantity": fresh_risk.recommended_quantity,
                "approved_quantity": authorization.approved_quantity,
                "daily_pnl_available": fresh_portfolio.daily_pnl_pct is not None,
                "paper": True,
            },
        )
        policy_notional_cap = fresh_portfolio.equity * self.risk_engine.policy.max_trade_pct
        approved_notional = authorization.approved_quantity * proposal.estimated_price
        sell_available = True
        if proposal.side == Side.SELL:
            long_quantity = next(
                (
                    position.quantity
                    for position in fresh_portfolio.positions
                    if position.symbol == proposal.symbol and position.quantity > 0
                ),
                0.0,
            )
            sell_available = authorization.approved_quantity <= long_quantity

        if (
            fresh_risk.blocked
            or fresh_risk.recommended_quantity < authorization.approved_quantity
            or approved_notional > policy_notional_cap
            or (proposal.side == Side.BUY and approved_notional > fresh_portfolio.buying_power)
            or not sell_available
        ):
            return self._result(
                proposal,
                authorization.approved_quantity,
                ExecutionState.REAUTHORIZATION_REQUIRED,
                "Fresh deterministic risk no longer supports the authorized order; run Governor again.",
                client_order_id=deterministic_client_order_id(proposal.proposal_id),
            )
        return None

    @staticmethod
    def _broker_order_mismatch(order: BrokerOrder, intended: IntendedPaperOrder) -> str | None:
        if order.client_order_id != intended.client_order_id:
            return "client_order_id"
        if order.symbol != intended.symbol:
            return "symbol"
        if order.side != intended.side:
            return "side"
        if order.quantity != intended.quantity:
            return "quantity"
        return None

    def _broker_result(
        self,
        proposal: TradeProposal,
        order: BrokerOrder,
        state: ExecutionState,
        message: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            client_order_id=order.client_order_id,
            alpaca_order_id=order.alpaca_order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=int(order.quantity),
            status=state,
            submitted_at=order.submitted_at,
            filled_at=order.filled_at,
            filled_quantity=order.filled_quantity,
            filled_avg_price=order.filled_avg_price,
            broker_status=order.status,
            execution_mode=self.config.mode,
            message=message,
        )

    def _blocked_result(
        self,
        proposal: TradeProposal,
        governor: GovernorDecision,
        state: ExecutionState,
        message: str,
        *,
        client_order_id: str | None = None,
    ) -> ExecutionResult:
        quantity = max(governor.approved_quantity, 0)
        self._audit(
            proposal.proposal_id,
            "EXECUTION_BLOCKED",
            {
                "state": state.value,
                "message": message,
                "client_order_id": client_order_id,
                "paper": True,
            },
        )
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            client_order_id=client_order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=quantity,
            status=state,
            execution_mode=self.config.mode,
            message=message,
        )

    def _failed_result(
        self,
        proposal: TradeProposal,
        quantity: int,
        client_order_id: str | None,
        message: str,
        *,
        audit: bool = True,
    ) -> ExecutionResult:
        if audit:
            self._audit(
                proposal.proposal_id,
                "EXECUTION_FAILED",
                {
                    "state": ExecutionState.FAILED.value,
                    "message": message,
                    "client_order_id": client_order_id,
                    "paper": True,
                },
            )
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            client_order_id=client_order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=max(quantity, 0),
            status=ExecutionState.FAILED,
            execution_mode=self.config.mode,
            message=message,
        )

    def _result(
        self,
        proposal: TradeProposal,
        quantity: int,
        state: ExecutionState,
        message: str,
        *,
        client_order_id: str | None,
    ) -> ExecutionResult:
        self._audit(
            proposal.proposal_id,
            "EXECUTION_BLOCKED",
            {
                "state": state.value,
                "message": message,
                "client_order_id": client_order_id,
                "paper": True,
            },
        )
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            client_order_id=client_order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=max(quantity, 0),
            status=state,
            execution_mode=self.config.mode,
            message=message,
        )

    def _audit_order(self, action: str, proposal_id: str, order: BrokerOrder) -> None:
        self._audit(
            proposal_id,
            action,
            {
                "proposal_id": proposal_id,
                "client_order_id": order.client_order_id,
                "alpaca_order_id": order.alpaca_order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "status": order.status,
                "submitted_at": order.submitted_at.isoformat(),
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
                "filled_quantity": order.filled_quantity,
                "filled_avg_price": order.filled_avg_price,
                "paper": True,
            },
        )

    def _persist_broker_order(self, proposal_id: str, order: BrokerOrder) -> None:
        if self.repository is not None:
            self.repository.save_broker_order(
                broker_order_snapshot(order, proposal_id, now=self.now_provider())
            )

    def _audit(self, proposal_id: str, action: str, payload: dict[str, Any]) -> None:
        self.audit.append_execution(proposal_id, action, payload)
