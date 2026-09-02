from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable, Protocol

from app.alpaca.execution import (
    AlpacaExecutionError,
    AlpacaOrderNotFoundError,
    PaperExecutionAlpacaAdapter,
)
from app.audit import AuditLog, SQLiteAuditLog
from app.execution.models import (
    BrokerOrder,
    BrokerOrderSnapshot,
    OrderLifecycleState,
)
from app.persistence.repositories import LifecycleRepository


class OrderReconciliationError(RuntimeError):
    pass


class OrderNotFoundError(OrderReconciliationError):
    pass


class ReconciliationAdapterProtocol(Protocol):
    @property
    def paper_mode_verified(self) -> bool: ...

    def get_order_by_id(self, alpaca_order_id: str) -> BrokerOrder: ...

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...


def map_order_lifecycle(raw_status: str) -> OrderLifecycleState:
    normalized = raw_status.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"new", "accepted"}:
        return OrderLifecycleState.NEW
    if normalized == "partially_filled":
        return OrderLifecycleState.PARTIALLY_FILLED
    if normalized == "filled":
        return OrderLifecycleState.FILLED
    if normalized in {"canceled", "cancelled", "done_for_day", "replaced"}:
        return OrderLifecycleState.CANCELED
    if normalized == "expired":
        return OrderLifecycleState.EXPIRED
    if normalized in {"rejected", "failed"}:
        return OrderLifecycleState.REJECTED
    if normalized in {
        "pending",
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "pending_review",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "held",
        "calculated",
    }:
        return OrderLifecycleState.PENDING
    return OrderLifecycleState.UNKNOWN


def broker_order_snapshot(
    order: BrokerOrder,
    proposal_id: str,
    *,
    now: datetime | None = None,
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        alpaca_order_id=order.alpaca_order_id,
        client_order_id=order.client_order_id,
        proposal_id=proposal_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        lifecycle_status=map_order_lifecycle(order.status),
        broker_status=order.status,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
        filled_quantity=order.filled_quantity or 0,
        filled_avg_price=order.filled_avg_price,
        updated_at=now or datetime.now(timezone.utc),
    )


def _meaningful_state(order: BrokerOrderSnapshot) -> tuple[object, ...]:
    return (
        order.lifecycle_status,
        order.broker_status.casefold(),
        order.filled_quantity,
        order.filled_avg_price,
        order.filled_at,
    )


class OrderReconciliationService:
    """Read-only REST reconciliation; it cannot cancel, replace, or close orders."""

    def __init__(
        self,
        repository: LifecycleRepository,
        *,
        audit: AuditLog | None = None,
        adapter_factory: Callable[[], ReconciliationAdapterProtocol] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.audit = audit or SQLiteAuditLog(repository)
        self.adapter_factory = adapter_factory or PaperExecutionAlpacaAdapter.from_environment
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def reconcile(
        self,
        *,
        client_order_id: str | None = None,
        alpaca_order_id: str | None = None,
    ) -> BrokerOrderSnapshot:
        if not client_order_id and not alpaca_order_id:
            raise ValueError("client_order_id or alpaca_order_id is required.")

        persisted = (
            self.repository.get_broker_order(client_order_id)
            if client_order_id
            else self.repository.get_broker_order_by_alpaca_id(alpaca_order_id or "")
        )
        proposal_id = persisted.proposal_id if persisted else None
        if proposal_id is None:
            raise OrderNotFoundError("Order is not known to durable state.")

        try:
            adapter = self.adapter_factory()
            if adapter.paper_mode_verified is not True:
                raise AlpacaExecutionError("Reconciliation adapter did not verify paper mode.")
            if alpaca_order_id or persisted.alpaca_order_id:
                current = adapter.get_order_by_id(alpaca_order_id or persisted.alpaca_order_id)
            else:
                current = adapter.find_order_by_client_id(persisted.client_order_id)
        except AlpacaOrderNotFoundError as exc:
            raise OrderNotFoundError(str(exc)) from exc
        except Exception as exc:
            raise OrderReconciliationError(
                f"Alpaca PAPER reconciliation failed ({type(exc).__name__})."
            ) from exc
        if current is None:
            raise OrderNotFoundError("Alpaca PAPER order was not found.")
        if (
            current.client_order_id != persisted.client_order_id
            or current.alpaca_order_id != persisted.alpaca_order_id
            or current.symbol != persisted.symbol
            or current.side != persisted.side
            or current.quantity != persisted.quantity
        ):
            raise OrderReconciliationError("Broker order identity does not match durable state.")

        snapshot = broker_order_snapshot(current, proposal_id, now=self.now_provider())
        changed = _meaningful_state(snapshot) != _meaningful_state(persisted)
        self.repository.save_broker_order(snapshot)
        if changed:
            self.audit.append_execution(
                proposal_id,
                "ORDER_STATE_CHANGED",
                {
                    "client_order_id": snapshot.client_order_id,
                    "alpaca_order_id": snapshot.alpaca_order_id,
                    "previous_lifecycle_status": persisted.lifecycle_status.value,
                    "lifecycle_status": snapshot.lifecycle_status.value,
                    "broker_status": snapshot.broker_status,
                    "filled_quantity": snapshot.filled_quantity,
                    "filled_avg_price": snapshot.filled_avg_price,
                    "filled_at": snapshot.filled_at.isoformat() if snapshot.filled_at else None,
                    "paper": True,
                },
            )
        return snapshot

    def reconcile_until_terminal(
        self,
        client_order_id: str,
        *,
        timeout_seconds: float = 30,
        interval_seconds: float = 2,
    ) -> BrokerOrderSnapshot:
        if not 0 <= timeout_seconds <= 300:
            raise ValueError("Reconciliation timeout must be between 0 and 300 seconds.")
        if not 0.1 <= interval_seconds <= 60:
            raise ValueError("Reconciliation interval must be between 0.1 and 60 seconds.")
        deadline = time.monotonic() + timeout_seconds
        current = self.repository.get_broker_order(client_order_id)
        if current is None:
            raise OrderNotFoundError("Order is not known to durable state.")
        while True:
            current = self.reconcile(client_order_id=client_order_id)
            if current.lifecycle_status.terminal or time.monotonic() >= deadline:
                return current
            time.sleep(min(interval_seconds, max(deadline - time.monotonic(), 0)))
