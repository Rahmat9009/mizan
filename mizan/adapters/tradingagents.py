"""The TradingAgents adapter (W8). Goal: ten lines of integration.

TradingAgents-style frameworks end a run with a *final trade decision*: an action, a ticker, a size and
the debate transcript that produced them. This module turns that object into a ``TradeProposal`` and
puts it behind the gate. Nothing else changes in the caller's program - which is the whole point: a
governance layer that requires an agent rewrite does not get adopted, and one that gets adopted only
by the careful is not a governance layer.

**No dependency on the upstream package.** Its licence is unverified (see ``ledger/escalations.md``),
so this adapter is written against the shape of the decision, not against an import. Anything with the
attributes or keys below works: the real framework, a thin shim, a dictionary from a queue, or the
:class:`StubTradingAgent` in the examples. If the upstream package is later cleared, nothing here
needs to change.

The decision it accepts (attributes or mapping keys, whichever the object offers):

===================  ==========================================================================
``action``           ``BUY`` / ``SELL`` / ``HOLD`` (case-insensitive). ``HOLD`` yields nothing.
``ticker``           the underlying symbol
``quantity``         a whole number of shares or contracts, as a string or an int
``reasoning``        the debate transcript - **audit only**, and it never reaches enforcement
``confidence``       optional 0..1 estimate; the policy may haircut it, it is never authority
``signal_sources``   optional provenance strings, e.g. ``vendor:polygon``
===================  ==========================================================================

Options are supported by adding ``contract_type``, ``strike`` and ``expiry``; the strategy is then
``long_call`` / ``long_put`` and the OCC symbol is derived by the contract, never supplied.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mizan.sdk import Mizan

from mizan.contracts import (
    AgentIdentity,
    DecisionRecord,
    ExecutionResult,
    ModelIdentity,
    TradeProposal,
    dstr,
    format_ts,
)
from mizan.contracts.errors import ValidationFailed

__all__ = [
    "HOLD_ACTIONS",
    "TradingAgentsAdapter",
    "proposal_from_decision",
]

#: Actions that mean "do nothing". A proposal is never built for them: the cheapest trade to govern is
#: the one that was never proposed.
HOLD_ACTIONS = frozenset({"hold", "none", "no_action", "wait"})

_SIDES = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}
_DEFAULT_TTL_MINUTES = 5


def _field(decision: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an object or a mapping, whichever the caller happens to have."""
    if isinstance(decision, Mapping):
        return decision.get(name, default)
    return getattr(decision, name, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _quantity(value: Any) -> str:
    """A whole, positive number of units. Anything else is refused, not rounded."""
    if value is None:
        raise ValidationFailed(detail="the decision carries no quantity")
    try:
        amount = Decimal(str(value).strip())
    except (ArithmeticError, ValueError) as failure:
        raise ValidationFailed(detail="quantity is not a number") from failure
    if amount <= 0 or amount != amount.to_integral_value():
        raise ValidationFailed(detail="quantity must be a positive whole number of units")
    return dstr(amount)


def proposal_from_decision(
    decision: Any,
    *,
    agent: AgentIdentity,
    model: ModelIdentity,
    now: datetime,
    ttl: timedelta = timedelta(minutes=_DEFAULT_TTL_MINUTES),
    market_snapshot_ref: str = "tradingagents:run",
    portfolio_snapshot_ref: str = "tradingagents:run",
) -> TradeProposal | None:
    """Translate one framework decision into a ``TradeProposal``. ``None`` for a hold.

    The agent's free text is carried into the audit-only field and nowhere else; the proposal id is
    computed by the contract over everything *except* that text, so a poisoned transcript produces a
    byte-identical proposal to a clean one (invariant 17).
    """
    action = _text(_field(decision, "action")).strip().casefold()
    if not action or action in HOLD_ACTIONS:
        return None
    side = _SIDES.get(action)
    if side is None:
        raise ValidationFailed(detail=f"unsupported action {action!r}")

    symbol = _text(_field(decision, "ticker") or _field(decision, "symbol")).strip().upper()
    if not symbol:
        raise ValidationFailed(detail="the decision names no symbol")

    contract_type = _field(decision, "contract_type")
    strike = _field(decision, "strike")
    expiry = _field(decision, "expiry")
    is_option = contract_type is not None
    limit_price = _field(decision, "limit_price")

    leg: dict[str, Any] = {
        "leg_index": 0,
        "side": side,
        "contract_type": contract_type,
        "strike": None if strike is None else dstr(Decimal(str(strike))),
        "expiry": expiry,
        "quantity": _quantity(_field(decision, "quantity")),
        "limit_price": None if limit_price is None else dstr(Decimal(str(limit_price))),
        "order_type": "limit" if limit_price is not None else "market",
    }
    if is_option:
        strategy = "long_call" if contract_type == "call" else "long_put"
    else:
        strategy = "long_equity" if side == "buy" else "short_equity"

    confidence = _field(decision, "confidence")
    sources = _field(decision, "signal_sources") or []
    return TradeProposal.build(
        agent=agent,
        model=model,
        created_at=format_ts(now),
        expires_at=format_ts(now + ttl),
        intent=_text(_field(decision, "intent", "open")) or "open",
        symbol=symbol,
        asset_class="equity_option" if is_option else "equity",
        strategy=strategy,
        legs=[leg],
        reasoning=_text(_field(decision, "reasoning"))[:20000],
        confidence=None if confidence is None else dstr(Decimal(str(confidence))),
        signal_sources=[str(source) for source in sources][:16],
        market_snapshot_ref=market_snapshot_ref,
        portfolio_snapshot_ref=portfolio_snapshot_ref,
    )


class TradingAgentsAdapter:
    """Ten lines: construct, wrap the framework's decision function, keep calling it.

        mizan = Mizan(tenant_id="acme", agent=AGENT, policy=POLICY, broker=broker, ledger=ledger)
        guard = TradingAgentsAdapter(mizan, model=MODEL)
        decide = guard.wrap(agents.propose)
        record = decide(ticker="AAPL")

    ``wrap`` returns a function with the framework's own signature. It calls the framework, governs
    what came back and returns the ``DecisionRecord`` - so the agent keeps its own control flow and
    gains an auditable, replayable decision it cannot talk its way past.
    """

    def __init__(self, mizan: Any, *, model: ModelIdentity, ttl: timedelta | None = None) -> None:
        # Typed for the checker but accepted as Any at runtime: annotating the PARAMETER as Mizan
        # would make mizan.sdk a hard import of this module and close an import cycle. The attribute
        # annotation gives the checker the return types without creating one.
        self.mizan: Mizan = mizan
        self.model = model
        self.ttl = ttl if ttl is not None else timedelta(minutes=_DEFAULT_TTL_MINUTES)

    # -- one decision -----------------------------------------------------------------------------
    def to_proposal(self, decision: Any) -> TradeProposal | None:
        return proposal_from_decision(
            decision,
            agent=self.mizan.agent,
            model=self.model,
            now=self.mizan.now(),
            ttl=self.ttl,
        )

    def evaluate(self, decision: Any) -> DecisionRecord | None:
        """Govern one framework decision. ``None`` when the framework decided to do nothing."""
        proposal = self.to_proposal(decision)
        return None if proposal is None else self.mizan.evaluate(proposal)

    def execute(self, record: DecisionRecord) -> ExecutionResult:
        return self.mizan.execute(record.decision_id)

    # -- the integration --------------------------------------------------------------------------
    def wrap(self, fn: Callable[..., Any]) -> Callable[..., DecisionRecord | None]:
        """Wrap the framework's decision function so its output is governed before it can be acted on."""

        def governed(*args: Any, **kwargs: Any) -> DecisionRecord | None:
            return self.evaluate(fn(*args, **kwargs))

        governed.__name__ = getattr(fn, "__name__", "governed")
        governed.__doc__ = getattr(fn, "__doc__", None)
        return governed
