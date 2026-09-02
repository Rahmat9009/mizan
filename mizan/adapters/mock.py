"""A scriptable in-memory broker. No network, no SDK, no hidden state.

Its purpose is to let a test drive the execution gate through situations a real broker cannot be asked
to produce on demand: an order that already exists, a portfolio that shrinks between two reads, a
venue that is unreachable. Every call is written to :attr:`MockBroker.log`, so a test can assert the
*order* of the gate's checks and not merely their outcome - which is the only way to prove Hard Rule
E4 (the kill switch is read after the last broker read, immediately before the mutation).

Missing data raises rather than returning an empty snapshot: a broker that answers "no positions" when
it means "I could not tell you" is exactly how E2 gets violated.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from mizan.adapters.base import BrokerOrder, OrderRequest
from mizan.contracts import Environment, MarketSnapshot, PortfolioSnapshot, ReasonCode, format_ts
from mizan.contracts.errors import BrokerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

__all__ = ["MockBroker"]


class MockBroker:
    """Scriptable in-memory broker for tests and demos. Never touches a network.

    Hooks fire *inside* a call, after the log entry and before the value is returned, so a test can
    mutate the world at the exact instant the gate is mid-read:

        broker = MockBroker(portfolio_snapshot=..., market_snapshot=...)
        broker.on_portfolio_read = lambda: broker.set_portfolio_snapshot(starved)
    """

    name = "mock"
    environment: Environment = "paper"

    def __init__(
        self,
        *,
        portfolio_snapshot: PortfolioSnapshot | None = None,
        market_snapshot: MarketSnapshot | None = None,
        log: list[str] | None = None,
        on_portfolio_read: Callable[[], Any] | None = None,
        on_market_read: Callable[[], Any] | None = None,
        on_find_order: Callable[[], Any] | None = None,
        on_before_submit: Callable[[], Any] | None = None,
        fail_with: BrokerError | None = None,
    ) -> None:
        self.portfolio_snapshot = portfolio_snapshot
        self.market_snapshot = market_snapshot
        self.submitted: list[OrderRequest] = []
        self.orders: dict[str, BrokerOrder] = {}
        self.log: list[str] = log if log is not None else []
        self.on_portfolio_read = on_portfolio_read
        self.on_market_read = on_market_read
        self.on_find_order = on_find_order
        self.on_before_submit = on_before_submit
        #: When set, ``submit_order`` raises it instead of accepting the order.
        self.fail_with = fail_with

    # -- scripting ----------------------------------------------------------------------------
    def set_portfolio_snapshot(self, snapshot: PortfolioSnapshot | None) -> None:
        self.portfolio_snapshot = snapshot

    def set_market_snapshot(self, snapshot: MarketSnapshot | None) -> None:
        self.market_snapshot = snapshot

    def seed_order(self, order: BrokerOrder) -> BrokerOrder:
        """Pretend this order was already accepted, so the gate's idempotency step finds it."""
        self.orders[order.client_order_id] = order
        return order

    # -- reads --------------------------------------------------------------------------------
    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot:
        self.log.append("broker.get_portfolio_snapshot")
        _fire(self.on_portfolio_read)
        if self.portfolio_snapshot is None:
            raise BrokerError(
                "Portfolio state is unavailable.", reason_codes=[ReasonCode.PORTFOLIO_STATE_MISSING]
            )
        return self.portfolio_snapshot

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: datetime
    ) -> MarketSnapshot:
        self.log.append("broker.get_market_snapshot")
        _fire(self.on_market_read)
        if self.market_snapshot is None:
            raise BrokerError("Market data is unavailable.", reason_codes=[ReasonCode.MARKET_DATA_MISSING])
        return self.market_snapshot

    def find_order(self, client_order_id: str) -> BrokerOrder | None:
        self.log.append("broker.find_order")
        _fire(self.on_find_order)
        return self.orders.get(client_order_id)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        self.log.append("broker.get_order")
        for order in self.orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        raise BrokerError("No such order.", reason_codes=[ReasonCode.BROKER_REJECTED])

    # -- the one and only mutation ------------------------------------------------------------
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        self.log.append("broker.submit_order")
        _fire(self.on_before_submit)
        if self.fail_with is not None:
            raise self.fail_with
        self.submitted.append(request)
        order = BrokerOrder(
            broker_order_id=f"mock-{len(self.submitted)}",
            client_order_id=request.client_order_id,
            status="accepted",
            submitted_at=format_ts(_now()),
        )
        self.orders[request.client_order_id] = order
        return order


def _fire(hook: Callable[[], Any] | None) -> None:
    if hook is not None:
        hook()


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)
