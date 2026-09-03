"""The context provider: everything the pure engine is allowed to know, assembled in one place.

Per ADR-0006 ``mizan.risk.evaluate`` is a pure function with no hidden state, so path-dependence,
aggregate multi-agent exposure, agent budgets, the graduated-response level and the calendar are
*inputs* on the ``RiskContext``. This module is where they are assembled, captured verbatim in the
DecisionRecord and therefore reproduced exactly by replay (A1).

**Prices come from the broker, never from the proposal.** Finding F-1 was a gate bypassed by price
poisoning: the caller-supplied estimated price was the only valuation input, so 1,000 shares claimed
at one cent passed every notional rule. Here the market snapshot is read from the broker and the
proposal contributes only *which* symbols to fetch. ``legs[].limit_price`` is a bound the agent asks
for; it is never what anything is worth.

Missing state is ``None``, never a zero (E2): a context that says "no positions" when it means "I
could not read the account" turns a fail-closed engine into a fail-open one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from mizan.adapters.base import BrokerAdapter
from mizan.contracts import (
    ENGINE_VERSION,
    AgentState,
    AggregateState,
    CalendarState,
    MarketSnapshot,
    PathState,
    Policy,
    PortfolioSnapshot,
    RecentOrder,
    RiskContext,
    TradeProposal,
    canonical_json,
    format_ts,
    sha256_hex,
)
from mizan.contracts.types import dstr, parse_ts

__all__ = ["BrokerContextProvider", "StateSources"]

#: A state input may be a fixed value, a callable returning one, or ``None`` for "not available".
#: The callable form is the Sprint-3 seam: swap in a ledger-backed derivation without touching the
#: gate, the SDK or a single test that uses the fixed form.
StateSources = Any


class BrokerContextProvider:
    """Builds a RiskContext from a broker's snapshots plus injectable state.

    ``path_state``, ``aggregate_state``, ``agent_state``, ``response_level`` and ``calendar`` are the
    Addendum-1 inputs. Each accepts a value or a zero-argument callable. **Sprint 3** replaces the
    injected values with derivations over the tenant's ledger (realised P&L path, book-level exposure
    across every agent, per-agent budgets consumed today, the exchange calendar) - the seam is here so
    that the engine, the gate and the recorded contract never have to change to accommodate them.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        *,
        ledger: Any | None = None,
        path_state: PathState | Callable[[], PathState | None] | None = None,
        aggregate_state: AggregateState | Callable[[], AggregateState | None] | None = None,
        agent_state: AgentState | Callable[[], AgentState | None] | None = None,
        response_level: int | Callable[[], int] = 0,
        calendar: CalendarState | Callable[[], CalendarState | None] | None = None,
        include_position_symbols: bool = True,
    ) -> None:
        self.broker = broker
        #: The tenant's decision chain. Used to derive `recent_orders` (ESC-4): without it the
        #: `duplicate_order` check is enabled, blocking, and structurally incapable of failing, because
        #: its loop body iterates a list nothing ever fills. The other state derivations described above
        #: remain deliberately unwired - a half-derived state is worse than an honestly absent one (E2).
        self.ledger = ledger
        self.path_state = path_state
        self.aggregate_state = aggregate_state
        self.agent_state = agent_state
        self.response_level = response_level
        self.calendar = calendar
        self.include_position_symbols = include_position_symbols

    #: Statuses that mean an order actually reached the venue. WOULD_SUBMIT is a dry run: nothing was
    #: placed, so treating it as a live duplicate would block real orders that never had a predecessor.
    AT_VENUE = frozenset({"SUBMITTED", "RECONCILED_EXISTING"})

    #: How far back to read when the policy sets no window. Bounded so this can never walk a long chain.
    DEFAULT_LOOKBACK_SECONDS = 300
    MAX_RECORDS_SCANNED = 200

    def _recent_orders_from_ledger(
        self, tenant_id: str, now: datetime, policy: Policy
    ) -> list[RecentOrder]:
        """Orders this tenant already placed, newest first, within the duplicate-detection window.

        Derived rather than declared: the window comes from the policy that will be evaluated against
        it, so a tenant widening `duplicate_order.window_seconds` widens what the check can see, and the
        two cannot silently disagree. Absent ledger or unreadable chain yields an empty list - the check
        then reports what it always did, but that is now visible rather than structural.
        """
        if self.ledger is None:
            return []
        config = policy.check_config("duplicate_order")
        window = config.window_seconds or self.DEFAULT_LOOKBACK_SECONDS
        horizon = now - timedelta(seconds=window)

        try:
            tenant_ledger = self.ledger.for_tenant(tenant_id)
            records = tenant_ledger.list(limit=self.MAX_RECORDS_SCANNED)
        except Exception:
            # A ledger that cannot be read must not take the decision path down with it. The check sees
            # an empty list and says so; it does not get to claim it verified anything.
            return []

        orders: list[RecentOrder] = []
        for record in records:
            execution = record.execution
            if execution is None or execution.status not in self.AT_VENUE:
                continue
            submitted_at = execution.submitted_at or record.recorded_at
            if parse_ts(submitted_at) < horizon:
                break  # newest-first, so everything after this is older still
            orders.append(
                RecentOrder(
                    proposal_id=record.proposal.proposal_id,
                    symbol=record.proposal.symbol,
                    side=record.proposal.legs[0].side,
                    # total_quantity is a derived Decimal on the proposal; RecentOrder carries DecimalStr.
                    total_quantity=dstr(record.proposal.total_quantity),
                    submitted_at=submitted_at,
                    status=execution.broker_status or execution.status,
                )
            )
        return orders

    def build(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        proposal: TradeProposal,
        policy: Policy,
        now: datetime,
        recent_orders: Sequence[RecentOrder] = (),
    ) -> RiskContext:
        # ESC-4: an explicit list from the caller wins; otherwise derive from the tenant's own chain.
        # A control that cannot fail is worse than a disabled one, and this one could not fail because
        # nothing populated what it reads.
        orders = list(recent_orders) or self._recent_orders_from_ledger(tenant_id, now, policy)
        portfolio = self.broker.get_portfolio_snapshot(as_of=now)
        market = self.broker.get_market_snapshot(
            symbols=self._symbols(proposal, portfolio),
            occ_symbols=self._occ_symbols(proposal),
            as_of=now,
        )
        return _context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            policy=policy,
            now=now,
            market=market,
            portfolio=portfolio,
            recent_orders=orders,
            path_state=_resolve(self.path_state),
            aggregate_state=_resolve(self.aggregate_state),
            agent_state=_resolve(self.agent_state),
            response_level=int(_resolve(self.response_level) or 0),
            calendar=_resolve(self.calendar),
        )

    # -- what to fetch ----------------------------------------------------------------------------
    def _symbols(self, proposal: TradeProposal, portfolio: PortfolioSnapshot | None) -> list[str]:
        """The proposal's symbol plus every symbol already held, so the whole book can be valued."""
        wanted = {proposal.symbol}
        if self.include_position_symbols and portfolio is not None:
            wanted.update(position.symbol for position in portfolio.positions)
        return sorted(wanted)

    @staticmethod
    def _occ_symbols(proposal: TradeProposal) -> list[str]:
        occ = {leg.occ_symbol(proposal.symbol) for leg in proposal.legs if leg.is_option}
        return sorted(occ)


def _resolve(source: Any) -> Any:
    return source() if callable(source) else source


def _context(
    *,
    tenant_id: str,
    agent_id: str,
    policy: Policy,
    now: datetime,
    market: MarketSnapshot | None,
    portfolio: PortfolioSnapshot | None,
    recent_orders: list[RecentOrder],
    path_state: PathState | None,
    aggregate_state: AggregateState | None,
    agent_state: AgentState | None,
    response_level: int,
    calendar: CalendarState | None,
) -> RiskContext:
    """Assemble the context and give it a content-derived id.

    The id is a hash of everything the context contains, so two identical contexts are the same
    context and a changed one is visibly a different one - which is what makes ``fresh_context_id`` in
    an ExecutionResult worth reading.
    """
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "evaluated_at": format_ts(now),
        "policy": policy.ref.model_dump(mode="json"),
        "market_snapshot": None if market is None else market.model_dump(mode="json"),
        "portfolio_snapshot": None if portfolio is None else portfolio.model_dump(mode="json"),
        "recent_orders": [order.model_dump(mode="json") for order in recent_orders],
        "engine_version": ENGINE_VERSION,
        "path_state": None if path_state is None else path_state.model_dump(mode="json"),
        "aggregate_state": None if aggregate_state is None else aggregate_state.model_dump(mode="json"),
        "agent_state": None if agent_state is None else agent_state.model_dump(mode="json"),
        "response_level": response_level,
        "calendar": None if calendar is None else calendar.model_dump(mode="json"),
    }
    context_id = f"ctx-{sha256_hex(canonical_json(payload))[:32]}"
    return RiskContext.model_validate({**payload, "context_id": context_id})
