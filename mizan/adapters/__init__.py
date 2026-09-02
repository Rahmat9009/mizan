"""L3 — broker and framework adapters.

The :class:`BrokerAdapter` Protocol deliberately has no ``cancel_order``, ``replace_order``,
``close_position`` or ``close_all_positions``. Cancel/replace automation is out of scope for v1
(Hard Rule B4), and the way to guarantee that is to give the abstraction no vocabulary for it.

``AlpacaPaperBroker.from_environment`` proves paper mode before it constructs a client, so a
misconfigured environment fails before any network access (B1).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal, Protocol, Sequence, runtime_checkable

from pydantic import ConfigDict, Field

from mizan.contracts import (
    AssetClass,
    AuthorizedLeg,
    ContractModel,
    DecimalStr,
    Environment,
    Intent,
    MarketSnapshot,
    NonNegativeDecimalStr,
    Policy,
    PortfolioSnapshot,
    RecentOrder,
    Rfc3339,
    RiskContext,
    Sha256Hex,
    Symbol,
    TradeProposal,
)
from mizan.contracts.errors import LiveTradingForbidden

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

__all__ = [
    "PAPER_HOST",
    "AlpacaPaperBroker",
    "BrokerAdapter",
    "BrokerContextProvider",
    "BrokerOrder",
    "ContextProvider",
    "MockBroker",
    "OrderRequest",
]

#: The only broker host this build knows. There is no constant for the production host.
PAPER_HOST = "paper-api.alpaca.markets"

_TRUE = frozenset({"1", "true", "yes", "on"})


class OrderRequest(ContractModel):
    """What the execution gate hands a broker. ``environment`` cannot say anything but paper."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: Symbol
    asset_class: AssetClass
    intent: Intent
    legs: list[AuthorizedLeg] = Field(min_length=1, max_length=4)
    time_in_force: Literal["day"] = "day"
    environment: Environment = "paper"


class BrokerOrder(ContractModel):
    """Broker-neutral order state. A raw SDK object never escapes an adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    broker_order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    submitted_at: Rfc3339
    filled_quantity: NonNegativeDecimalStr = "0"
    avg_price: DecimalStr | None = None
    raw_hash: Sha256Hex | None = None


@runtime_checkable
class BrokerAdapter(Protocol):
    """Reads plus exactly one mutation. No cancel, no replace, no close (B4)."""

    name: str
    environment: Environment

    def get_portfolio_snapshot(self, *, as_of: "datetime") -> PortfolioSnapshot: ...

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: "datetime"
    ) -> MarketSnapshot: ...

    def find_order(self, client_order_id: str) -> BrokerOrder | None: ...

    def submit_order(self, request: OrderRequest) -> BrokerOrder: ...

    def get_order(self, broker_order_id: str) -> BrokerOrder: ...


@runtime_checkable
class ContextProvider(Protocol):
    """Assembles the RiskContext the engine evaluates against, including path/aggregate state."""

    def build(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        proposal: TradeProposal,
        policy: Policy,
        now: "datetime",
        recent_orders: Sequence[RecentOrder] = (),
    ) -> RiskContext: ...


class MockBroker:
    """Scriptable in-memory broker for tests and demos. Never touches a network."""

    name = "mock"
    environment: Environment = "paper"

    def __init__(
        self,
        *,
        portfolio_snapshot: PortfolioSnapshot | None = None,
        market_snapshot: MarketSnapshot | None = None,
    ) -> None:
        self.portfolio_snapshot = portfolio_snapshot
        self.market_snapshot = market_snapshot
        self.submitted: list[OrderRequest] = []
        self.orders: dict[str, BrokerOrder] = {}

    def get_portfolio_snapshot(self, *, as_of: "datetime") -> PortfolioSnapshot:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: "datetime"
    ) -> MarketSnapshot:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def find_order(self, client_order_id: str) -> BrokerOrder | None:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError("L3 implements this in Sprint 2")


class AlpacaPaperBroker:
    """Alpaca PAPER adapter. Constructing one against anything else is impossible."""

    name = "alpaca"
    environment: Environment = "paper"

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> "AlpacaPaperBroker":
        """Prove paper mode FIRST, then read credentials, then build a client.

        The ordering matters: a live-configured environment must fail before credentials are read and
        long before a socket is opened, so a misconfiguration cannot leak a key or reach a live venue.
        """
        raw = os.getenv("ALPACA_PAPER")
        if raw is None or raw.strip().casefold() not in _TRUE:
            raise LiveTradingForbidden(
                message="ALPACA_PAPER must be explicitly true; this build has no live trading path."
            )
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_portfolio_snapshot(self, *, as_of: "datetime") -> PortfolioSnapshot:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: "datetime"
    ) -> MarketSnapshot:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def find_order(self, client_order_id: str) -> BrokerOrder | None:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError("L3 implements this in Sprint 2")


class BrokerContextProvider:
    """Builds a RiskContext from a broker's snapshots plus ledger-derived state.

    Per ADR-0006 this is where path, aggregate, agent and calendar state are assembled: the engine is a
    pure function, so everything it needs to know arrives here.
    """

    def __init__(self, broker: BrokerAdapter, *, ledger: Any | None = None) -> None:
        self.broker = broker
        self.ledger = ledger

    def build(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        proposal: TradeProposal,
        policy: Policy,
        now: "datetime",
        recent_orders: Sequence[RecentOrder] = (),
    ) -> RiskContext:
        raise NotImplementedError("L3 implements this in Sprint 2")
