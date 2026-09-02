from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.audit import SQLiteAuditLog
from app.execution.models import BrokerOrder, BrokerOrderSnapshot, OrderLifecycleState
from app.execution.reconciliation import (
    OrderNotFoundError,
    OrderReconciliationError,
    OrderReconciliationService,
    map_order_lifecycle,
)
from app.models import Side, TradeProposal
from app.persistence import Database, LifecycleRepository


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


class ReconcileAdapter:
    paper_mode_verified = True

    def __init__(self, order: BrokerOrder | Exception) -> None:
        self.order = order
        self.by_id: list[str] = []

    def get_order_by_id(self, order_id: str) -> BrokerOrder:
        self.by_id.append(order_id)
        if isinstance(self.order, Exception):
            raise self.order
        return self.order

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        if isinstance(self.order, Exception):
            raise self.order
        return self.order


def trade() -> TradeProposal:
    return TradeProposal(
        proposal_id="reconcile-p1",
        symbol="AAPL",
        side="BUY",
        quantity=1,
        estimated_price=200,
        strategy_confidence=.9,
        thesis="Reconcile.",
        invalidation_condition="Reverse.",
        created_at=NOW,
    )


def broker(status: str, **changes) -> BrokerOrder:
    values = {
        "alpaca_order_id": "alpaca-reconcile-1",
        "client_order_id": "pgv5-reconcile-client",
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 1,
        "status": status,
        "submitted_at": NOW,
        "filled_at": None,
        "filled_quantity": 0,
        "filled_avg_price": None,
    }
    values.update(changes)
    return BrokerOrder(**values)


def persisted(status: str = "new") -> BrokerOrderSnapshot:
    value = broker(status)
    return BrokerOrderSnapshot(
        alpaca_order_id=value.alpaca_order_id,
        client_order_id=value.client_order_id,
        proposal_id="reconcile-p1",
        symbol=value.symbol,
        side=value.side,
        quantity=value.quantity,
        lifecycle_status=map_order_lifecycle(status),
        broker_status=status,
        submitted_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def state(tmp_path):
    database = Database(tmp_path / "orders.db")
    repository = LifecycleRepository(database)
    repository.save_proposal(trade())
    repository.save_broker_order(persisted())
    return repository, SQLiteAuditLog(repository)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("new", OrderLifecycleState.NEW),
        ("accepted", OrderLifecycleState.NEW),
        ("partially_filled", OrderLifecycleState.PARTIALLY_FILLED),
        ("filled", OrderLifecycleState.FILLED),
        ("canceled", OrderLifecycleState.CANCELED),
        ("replaced", OrderLifecycleState.CANCELED),
        ("expired", OrderLifecycleState.EXPIRED),
        ("rejected", OrderLifecycleState.REJECTED),
        ("pending_replace", OrderLifecycleState.PENDING),
        ("pending_review", OrderLifecycleState.PENDING),
        ("suspended", OrderLifecycleState.PENDING),
        ("future_alpaca_state", OrderLifecycleState.UNKNOWN),
    ],
)
def test_status_mapping(raw, expected) -> None:
    assert map_order_lifecycle(raw) == expected


def test_partial_fill_fields_are_persisted_and_audited(state) -> None:
    repository, audit = state
    current = broker(
        "partially_filled",
        filled_quantity=.5,
        filled_avg_price=201.25,
    )
    adapter = ReconcileAdapter(current)
    service = OrderReconciliationService(
        repository, audit=audit, adapter_factory=lambda: adapter, now_provider=lambda: NOW
    )
    result = service.reconcile(client_order_id=current.client_order_id)
    assert result.lifecycle_status == OrderLifecycleState.PARTIALLY_FILLED
    assert result.filled_quantity == .5
    assert result.filled_avg_price == 201.25
    assert repository.get_broker_order(current.client_order_id) == result
    assert [event.action for event in audit.list_for_proposal("reconcile-p1")] == [
        "ORDER_STATE_CHANGED"
    ]


def test_filled_at_and_fill_values_are_captured(state) -> None:
    repository, audit = state
    current = broker(
        "filled",
        filled_at=NOW,
        filled_quantity=1,
        filled_avg_price=202.5,
    )
    result = OrderReconciliationService(
        repository,
        audit=audit,
        adapter_factory=lambda: ReconcileAdapter(current),
        now_provider=lambda: NOW,
    ).reconcile(client_order_id=current.client_order_id)
    assert result.lifecycle_status.terminal is True
    assert result.filled_at == NOW
    assert result.filled_quantity == 1


def test_bounded_reconciliation_stops_immediately_at_terminal_state(state) -> None:
    repository, audit = state
    current = broker(
        "filled", filled_at=NOW, filled_quantity=1, filled_avg_price=200
    )
    result = OrderReconciliationService(
        repository,
        audit=audit,
        adapter_factory=lambda: ReconcileAdapter(current),
    ).reconcile_until_terminal(
        current.client_order_id, timeout_seconds=30, interval_seconds=.1
    )
    assert result.lifecycle_status == OrderLifecycleState.FILLED


def test_unchanged_state_does_not_spam_audit(state) -> None:
    repository, audit = state
    service = OrderReconciliationService(
        repository,
        audit=audit,
        adapter_factory=lambda: ReconcileAdapter(broker("new")),
        now_provider=lambda: NOW,
    )
    service.reconcile(client_order_id="pgv5-reconcile-client")
    service.reconcile(client_order_id="pgv5-reconcile-client")
    assert audit.list_for_proposal("reconcile-p1") == []


def test_reconciliation_uses_known_alpaca_order_id(state) -> None:
    repository, audit = state
    adapter = ReconcileAdapter(broker("new"))
    OrderReconciliationService(
        repository, audit=audit, adapter_factory=lambda: adapter
    ).reconcile(client_order_id="pgv5-reconcile-client")
    assert adapter.by_id == ["alpaca-reconcile-1"]


def test_reconciliation_can_start_from_alpaca_order_id(state) -> None:
    repository, audit = state
    result = OrderReconciliationService(
        repository,
        audit=audit,
        adapter_factory=lambda: ReconcileAdapter(broker("new")),
    ).reconcile(alpaca_order_id="alpaca-reconcile-1")
    assert result.client_order_id == "pgv5-reconcile-client"


def test_unknown_durable_order_fails_before_broker_call(tmp_path) -> None:
    repository = LifecycleRepository(Database(tmp_path / "missing.db"))
    with pytest.raises(OrderNotFoundError):
        OrderReconciliationService(repository).reconcile(client_order_id="missing")


def test_broker_error_is_sanitized(state) -> None:
    repository, audit = state
    service = OrderReconciliationService(
        repository,
        audit=audit,
        adapter_factory=lambda: ReconcileAdapter(RuntimeError("secret broker detail")),
    )
    with pytest.raises(OrderReconciliationError) as caught:
        service.reconcile(client_order_id="pgv5-reconcile-client")
    assert "secret broker detail" not in str(caught.value)


def test_broker_identity_mismatch_fails_closed(state) -> None:
    repository, audit = state
    wrong = broker("new").model_copy(update={"client_order_id": "different"})
    with pytest.raises(OrderReconciliationError, match="identity"):
        OrderReconciliationService(
            repository, audit=audit, adapter_factory=lambda: ReconcileAdapter(wrong)
        ).reconcile(client_order_id="pgv5-reconcile-client")
