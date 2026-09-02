"""The broker abstraction: what an adapter must do, and everything it is not allowed to say.

The :class:`BrokerAdapter` Protocol deliberately has no ``cancel_order``, ``replace_order``,
``close_position`` or ``close_all_positions``. Cancel/replace automation is out of scope for v1
(Hard Rule B4), and the way to guarantee that is to give the abstraction no vocabulary for it: a
capability that cannot be named cannot be reached by a bug, a debug flag or a helpful refactor.

Four reads and exactly one mutation. Everything crossing this boundary is a contract type, so a raw
SDK object - with its own retries, its own mutability and its own idea of a number - never reaches the
engine (F-19: an adapter that merely *claims* to be in paper mode proves nothing).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

__all__ = [
    "PAPER_HOST",
    "BrokerAdapter",
    "BrokerOrder",
    "ContextProvider",
    "OrderRequest",
]

#: The only broker host this build knows. There is no constant for the production host, and no
#: configuration value, environment variable or argument can introduce one.
PAPER_HOST = "paper-api.alpaca.markets"


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

    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot: ...

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: datetime
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
        now: datetime,
        recent_orders: Sequence[RecentOrder] = (),
    ) -> RiskContext: ...
