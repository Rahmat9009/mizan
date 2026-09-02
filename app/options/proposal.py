"""Options proposal domain model, structural rules, and payoff envelope.

Scope is deliberately narrow. V1 supports five defined-risk strategies and
nothing else:

  LONG_CALL, LONG_PUT, VERTICAL_DEBIT_SPREAD, VERTICAL_CREDIT_SPREAD, IRON_CONDOR

The structural rules live here rather than in a separate validation pass so an
invalid structure is *unconstructable*. That matches the fail-closed posture the
rest of this backend already takes: a malformed proposal cannot exist long
enough to be persisted, sized, approved, or submitted.

Everything in this module is pure. No broker call, no database, no clock. The
broker independently re-verifies every leg at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Side
from app.options.money import money_equal, to_decimal, to_money
from app.options.occ import OccSymbolError, parse_occ_symbol, strikes_equal

# The premium the caller quotes is per share. A half-cent per share is the
# finest resolution an options quote carries.
PREMIUM_TOLERANCE = 0.005
MAX_LEGS = 4


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class OptionStrategy(str, Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    VERTICAL_DEBIT_SPREAD = "VERTICAL_DEBIT_SPREAD"
    VERTICAL_CREDIT_SPREAD = "VERTICAL_CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"


LEG_COUNTS: dict[OptionStrategy, int] = {
    OptionStrategy.LONG_CALL: 1,
    OptionStrategy.LONG_PUT: 1,
    OptionStrategy.VERTICAL_DEBIT_SPREAD: 2,
    OptionStrategy.VERTICAL_CREDIT_SPREAD: 2,
    OptionStrategy.IRON_CONDOR: 4,
}

# True when the strategy must be opened for a net credit.
CREDIT_STRATEGIES = frozenset(
    {OptionStrategy.VERTICAL_CREDIT_SPREAD, OptionStrategy.IRON_CONDOR}
)


class OptionLeg(BaseModel):
    """One contract inside a strategy.

    The declared fields and the OCC symbol must agree. A leg that claims one
    strike while naming a symbol at another strike cannot be constructed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    option_symbol: str = Field(min_length=7, max_length=21)
    side: Side
    option_type: OptionType
    strike: float = Field(gt=0, allow_inf_nan=False)
    expiry: date
    ratio: int = Field(default=1, ge=1)
    position_effect: Literal["OPEN"] = "OPEN"

    @field_validator("option_symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def symbol_must_match_declared_fields(self) -> "OptionLeg":
        if self.ratio != 1:
            raise ValueError(
                "V1 supports only 1:1 legs; a ratio other than 1 is not an accepted structure."
            )
        try:
            parsed = parse_occ_symbol(self.option_symbol)
        except OccSymbolError as exc:
            raise ValueError(str(exc)) from exc
        if parsed.option_type != self.option_type.value:
            raise ValueError(
                f"Leg {self.option_symbol} is a {parsed.option_type} "
                f"but declares {self.option_type.value}."
            )
        if parsed.expiry != self.expiry:
            raise ValueError(
                f"Leg {self.option_symbol} expires {parsed.expiry.isoformat()} "
                f"but declares {self.expiry.isoformat()}."
            )
        if not strikes_equal(parsed.strike, self.strike):
            raise ValueError(
                f"Leg {self.option_symbol} strikes at {parsed.strike:g} "
                f"but declares {self.strike:g}."
            )
        return self

    @property
    def root(self) -> str:
        return parse_occ_symbol(self.option_symbol).root

    @property
    def is_long(self) -> bool:
        return self.side == Side.BUY


class OptionTradeProposal(BaseModel):
    """A defined-risk options structure proposed by the upstream Trader Agent.

    ``estimated_net_premium_per_unit`` is signed and per share: a positive value
    is a credit received, a negative value is a debit paid. It is supplied by the
    caller, exactly like ``MarketRiskSnapshot``, and is never presented as live
    market data.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_type: Literal["option"] = "option"
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    underlying: str = Field(min_length=1, max_length=16)
    strategy: OptionStrategy
    quantity: int = Field(gt=0)
    expiry: date
    legs: list[OptionLeg] = Field(min_length=1, max_length=MAX_LEGS)
    estimated_net_premium_per_unit: float = Field(allow_inf_nan=False)
    contract_multiplier: int = Field(default=100, gt=0)
    estimated_max_loss: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    estimated_max_profit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    strategy_confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=1)
    invalidation_condition: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("underlying")
    @classmethod
    def normalize_underlying(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_structure(self) -> "OptionTradeProposal":
        # Naked-short detection runs first on purpose. When a structure is both
        # mislabelled and uncovered, "no long leg covers this short" is the
        # message that names the actual safety violation.
        _validate_no_naked_short(self)
        _validate_common(self)
        _validate_strategy_shape(self)
        _validate_premium_bounds(self)
        _validate_declared_values(self)
        return self

    # -- payoff envelope -------------------------------------------------
    #
    # These are float conveniences over the authoritative Decimal computation
    # in `recompute_economics`. Anything that decides how much capital is at
    # risk should use that function directly rather than these.

    @property
    def net_credit_per_unit(self) -> float:
        """Positive when the structure is opened for a credit."""

        return self.estimated_net_premium_per_unit

    @property
    def net_debit_per_unit(self) -> float:
        """Positive when the structure is opened for a debit."""

        return -self.estimated_net_premium_per_unit

    @property
    def max_loss_per_unit(self) -> float:
        return float(recompute_economics(self).max_loss_per_unit)

    @property
    def max_profit_per_unit(self) -> float | None:
        value = recompute_economics(self).max_profit_per_unit
        return None if value is None else float(value)

    @property
    def max_loss_total(self) -> float:
        return float(recompute_economics(self).max_loss)

    @property
    def max_profit_total(self) -> float | None:
        value = recompute_economics(self).max_profit
        return None if value is None else float(value)

    @property
    def risk_width(self) -> float:
        """The strike width that defines the risk, in dollars per share.

        Zero for single-leg strategies, whose risk is the premium alone. This
        depends only on strikes, never on premium, so it is safe to use inside
        premium validation.
        """

        return float(risk_width_of(self))


# -- structural rules ----------------------------------------------------


def _validate_common(proposal: "OptionTradeProposal") -> None:
    expected = LEG_COUNTS[proposal.strategy]
    if len(proposal.legs) != expected:
        raise ValueError(
            f"{proposal.strategy.value} requires exactly {expected} leg(s); "
            f"{len(proposal.legs)} were supplied."
        )

    symbols = [leg.option_symbol for leg in proposal.legs]
    if len(set(symbols)) != len(symbols):
        raise ValueError("Every leg must name a distinct option contract.")

    for leg in proposal.legs:
        if leg.expiry != proposal.expiry:
            raise ValueError(
                "V1 supports single-expiry structures only; leg "
                f"{leg.option_symbol} expires {leg.expiry.isoformat()} but the "
                f"proposal declares {proposal.expiry.isoformat()}."
            )
        if leg.root != proposal.underlying:
            raise ValueError(
                f"Leg {leg.option_symbol} is written on {leg.root}, "
                f"not on the declared underlying {proposal.underlying}."
            )

    if abs(proposal.estimated_net_premium_per_unit) < PREMIUM_TOLERANCE:
        raise ValueError(
            "estimated_net_premium_per_unit must be a non-zero credit (>0) or debit (<0)."
        )

    wants_credit = proposal.strategy in CREDIT_STRATEGIES
    is_credit = proposal.estimated_net_premium_per_unit > 0
    if wants_credit and not is_credit:
        raise ValueError(
            f"{proposal.strategy.value} must be opened for a net credit; "
            "a negative premium was supplied."
        )
    if not wants_credit and is_credit:
        raise ValueError(
            f"{proposal.strategy.value} must be opened for a net debit; "
            "a positive premium was supplied."
        )


def _validate_no_naked_short(proposal: "OptionTradeProposal") -> None:
    """Every short leg needs a same-type long leg standing behind it.

    V1 permits no naked short option under any strategy label. The paper
    account's own options level 3 entitlement refuses them too; this rule makes
    the backend reach the same answer without relying on the broker.
    """

    for option_type in (OptionType.CALL, OptionType.PUT):
        shorts = [
            leg for leg in proposal.legs if leg.option_type == option_type and not leg.is_long
        ]
        longs = [leg for leg in proposal.legs if leg.option_type == option_type and leg.is_long]
        if shorts and not longs:
            raise ValueError(
                f"Naked short {option_type.value} is not permitted: no long "
                f"{option_type.value} leg covers it."
            )
        if len(shorts) > len(longs):
            raise ValueError(
                f"Unbalanced short {option_type.value} legs: {len(shorts)} short "
                f"against {len(longs)} long."
            )


def _validate_strategy_shape(proposal: "OptionTradeProposal") -> None:
    strategy = proposal.strategy
    legs = proposal.legs

    if strategy in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
        expected_type = (
            OptionType.CALL if strategy == OptionStrategy.LONG_CALL else OptionType.PUT
        )
        leg = legs[0]
        if not leg.is_long:
            raise ValueError(f"{strategy.value} requires a BUY leg.")
        if leg.option_type != expected_type:
            raise ValueError(
                f"{strategy.value} requires a {expected_type.value} leg; "
                f"a {leg.option_type.value} was supplied."
            )
        return

    if strategy in (
        OptionStrategy.VERTICAL_DEBIT_SPREAD,
        OptionStrategy.VERTICAL_CREDIT_SPREAD,
    ):
        _validate_vertical(proposal)
        return

    _validate_iron_condor(proposal)


def _validate_vertical(proposal: "OptionTradeProposal") -> None:
    long_leg, short_leg = _single_pair(proposal.legs, proposal.strategy.value)

    if long_leg.option_type != short_leg.option_type:
        raise ValueError("A vertical spread requires both legs to be the same option type.")
    if strikes_equal(long_leg.strike, short_leg.strike):
        raise ValueError("A vertical spread requires two distinct strikes.")

    is_call = long_leg.option_type == OptionType.CALL
    is_debit = proposal.strategy == OptionStrategy.VERTICAL_DEBIT_SPREAD
    # A call spread is a debit when the long strike is lower; a put spread is a
    # debit when the long strike is higher. Anything else contradicts the label.
    long_is_lower = long_leg.strike < short_leg.strike
    structurally_debit = long_is_lower if is_call else not long_is_lower

    if is_debit and not structurally_debit:
        raise ValueError(
            f"A {long_leg.option_type.value} debit spread must buy the "
            f"{'lower' if is_call else 'higher'} strike; the supplied strikes describe a credit spread."
        )
    if not is_debit and structurally_debit:
        raise ValueError(
            f"A {long_leg.option_type.value} credit spread must sell the "
            f"{'lower' if is_call else 'higher'} strike; the supplied strikes describe a debit spread."
        )


def _validate_iron_condor(proposal: "OptionTradeProposal") -> None:
    calls = [leg for leg in proposal.legs if leg.option_type == OptionType.CALL]
    puts = [leg for leg in proposal.legs if leg.option_type == OptionType.PUT]
    if len(calls) != 2 or len(puts) != 2:
        raise ValueError("An iron condor requires exactly two call legs and two put legs.")

    long_call, short_call = _single_pair(calls, "The call wing of an iron condor")
    long_put, short_put = _single_pair(puts, "The put wing of an iron condor")

    ordered = (
        long_put.strike < short_put.strike < short_call.strike < long_call.strike
    )
    if not ordered:
        raise ValueError(
            "An iron condor requires strikes ordered long put < short put < "
            "short call < long call; the supplied strikes are "
            f"{long_put.strike:g}/{short_put.strike:g}/"
            f"{short_call.strike:g}/{long_call.strike:g}."
        )


def _single_pair(legs: list[OptionLeg], label: str) -> tuple[OptionLeg, OptionLeg]:
    """Return (long, short) for a two-leg group, or raise."""

    longs = [leg for leg in legs if leg.is_long]
    shorts = [leg for leg in legs if not leg.is_long]
    if len(longs) != 1 or len(shorts) != 1:
        raise ValueError(f"{label} requires exactly one BUY leg and one SELL leg.")
    return longs[0], shorts[0]


def wing_width(proposal: "OptionTradeProposal", option_type: OptionType) -> Decimal:
    """The strike width of one wing, in dollars per share. Zero if it has none."""

    strikes = [to_decimal(leg.strike) for leg in proposal.legs if leg.option_type == option_type]
    if len(strikes) < 2:
        return Decimal(0)
    return abs(max(strikes) - min(strikes))


def risk_width_of(proposal: "OptionTradeProposal") -> Decimal:
    """The strike width that defines the risk.

    Iron condor wings are never assumed symmetric: the wider wing is the one
    that can actually be lost, so it is the one that sizes the risk.
    """

    if proposal.strategy in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
        return Decimal(0)
    return max(wing_width(proposal, OptionType.CALL), wing_width(proposal, OptionType.PUT))


def _wing_width(proposal: "OptionTradeProposal", option_type: OptionType) -> float:
    return float(wing_width(proposal, option_type))


def _validate_premium_bounds(proposal: "OptionTradeProposal") -> None:
    """Reject premiums that describe an impossible or arbitrage-shaped trade."""

    strategy = proposal.strategy
    debit = proposal.net_debit_per_unit
    credit = proposal.net_credit_per_unit

    if strategy == OptionStrategy.LONG_PUT:
        strike = proposal.legs[0].strike
        if debit >= strike:
            raise ValueError(
                f"A long put debit of {debit:g} cannot equal or exceed its "
                f"strike of {strike:g}."
            )
        return
    if strategy == OptionStrategy.LONG_CALL:
        return

    width = proposal.risk_width
    if strategy == OptionStrategy.VERTICAL_DEBIT_SPREAD:
        if debit >= width:
            raise ValueError(
                f"A debit of {debit:g} cannot equal or exceed the {width:g} "
                "strike width; the structure would have no possible profit."
            )
        return

    if credit >= width:
        raise ValueError(
            f"A credit of {credit:g} cannot equal or exceed the {width:g} "
            "strike width; the structure would carry no possible loss."
        )


# -- authoritative economics ---------------------------------------------


class ProfitBound(str, Enum):
    """Whether a strategy's maximum profit is a finite number at all."""

    BOUNDED = "BOUNDED"
    UNBOUNDED = "UNBOUNDED"


class InvalidOptionEconomics(ValueError):
    """The premium and strikes do not describe a tradeable defined-risk position."""


@dataclass(frozen=True)
class OptionEconomics:
    """Backend-recomputed economics. Decimal throughout, quantized to the cent.

    ``max_profit`` is ``None`` exactly when ``profit_bound`` is ``UNBOUNDED``.
    No sentinel number, no infinity, no arbitrary ceiling stands in for it.
    """

    strategy: OptionStrategy
    quantity: int
    multiplier: Decimal
    net_premium_per_share: Decimal
    net_debit_per_share: Decimal | None
    net_credit_per_share: Decimal | None
    risk_width: Decimal | None
    max_loss_per_unit: Decimal
    max_profit_per_unit: Decimal | None
    max_loss: Decimal
    max_profit: Decimal | None
    profit_bound: ProfitBound


def recompute_economics(proposal: "OptionTradeProposal") -> OptionEconomics:
    """Recompute a structure's payoff envelope from strikes, sides and premium.

    This is the backend's own arithmetic. Nothing the caller declared is read
    here, so a declared figure can be compared against a number that was never
    influenced by it.

    Raises ``InvalidOptionEconomics`` when the premium cannot describe the
    declared structure — a non-positive debit, or a premium at or beyond the
    strike width, which would leave the position with no possible loss or no
    possible profit.
    """

    strategy = proposal.strategy
    quantity = proposal.quantity
    if quantity <= 0:
        raise InvalidOptionEconomics("Contract quantity must be positive.")
    multiplier = to_decimal(proposal.contract_multiplier)
    if multiplier <= 0:
        raise InvalidOptionEconomics("Contract multiplier must be positive.")

    premium = to_decimal(proposal.estimated_net_premium_per_unit)
    if premium == 0:
        raise InvalidOptionEconomics("Net premium must be a non-zero credit or debit.")

    debit = -premium if premium < 0 else None
    credit = premium if premium > 0 else None
    scale = multiplier * quantity

    if strategy in (OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT):
        if debit is None or debit <= 0:
            raise InvalidOptionEconomics(
                f"{strategy.value} must be opened for a positive debit."
            )
        width = None
        loss_per_unit = debit * multiplier
        if strategy == OptionStrategy.LONG_CALL:
            profit_per_unit = None
            bound = ProfitBound.UNBOUNDED
        else:
            strike = to_decimal(proposal.legs[0].strike)
            if debit >= strike:
                raise InvalidOptionEconomics(
                    f"A long put debit of {debit} cannot equal or exceed its strike of {strike}."
                )
            profit_per_unit = max(strike - debit, Decimal(0)) * multiplier
            bound = ProfitBound.BOUNDED

    elif strategy == OptionStrategy.VERTICAL_DEBIT_SPREAD:
        width = risk_width_of(proposal)
        if debit is None or debit <= 0:
            raise InvalidOptionEconomics("A debit spread must be opened for a positive debit.")
        if debit >= width:
            raise InvalidOptionEconomics(
                f"A debit of {debit} cannot equal or exceed the {width} strike width."
            )
        loss_per_unit = debit * multiplier
        profit_per_unit = (width - debit) * multiplier
        bound = ProfitBound.BOUNDED

    elif strategy in (OptionStrategy.VERTICAL_CREDIT_SPREAD, OptionStrategy.IRON_CONDOR):
        width = risk_width_of(proposal)
        if credit is None or credit <= 0:
            raise InvalidOptionEconomics(
                f"{strategy.value} must be opened for a positive credit."
            )
        if credit >= width:
            raise InvalidOptionEconomics(
                f"A credit of {credit} cannot equal or exceed the {width} strike width."
            )
        loss_per_unit = (width - credit) * multiplier
        profit_per_unit = credit * multiplier
        bound = ProfitBound.BOUNDED

    else:
        raise InvalidOptionEconomics(f"Strategy {strategy!r} has no defined economics.")

    return OptionEconomics(
        strategy=strategy,
        quantity=quantity,
        multiplier=multiplier,
        net_premium_per_share=premium,
        net_debit_per_share=debit,
        net_credit_per_share=credit,
        risk_width=width,
        max_loss_per_unit=to_money(loss_per_unit),
        max_profit_per_unit=None if profit_per_unit is None else to_money(profit_per_unit),
        max_loss=to_money(loss_per_unit * quantity),
        max_profit=None if profit_per_unit is None else to_money(profit_per_unit * quantity),
        profit_bound=bound,
    )


def _validate_declared_values(proposal: "OptionTradeProposal") -> None:
    """Cross-check the caller's declared max loss and profit against our own math.

    The Trader Agent's arithmetic is evidence, not authority. A disagreement is
    a rejection, not a silent correction.
    """

    economics = recompute_economics(proposal)

    if proposal.estimated_max_loss is not None:
        if not money_equal(proposal.estimated_max_loss, economics.max_loss):
            raise ValueError(
                f"Declared max loss {proposal.estimated_max_loss:.2f} disagrees with the "
                f"structure's computed max loss {economics.max_loss}."
            )

    if economics.profit_bound is ProfitBound.UNBOUNDED:
        if proposal.estimated_max_profit is not None:
            raise ValueError(
                f"{proposal.strategy.value} has an unbounded maximum profit; "
                "estimated_max_profit must be omitted. A number is not equivalent "
                "to unbounded."
            )
        return

    if proposal.estimated_max_profit is not None:
        if not money_equal(proposal.estimated_max_profit, economics.max_profit):
            raise ValueError(
                f"Declared max profit {proposal.estimated_max_profit:.2f} disagrees with the "
                f"structure's computed max profit {economics.max_profit}."
            )
