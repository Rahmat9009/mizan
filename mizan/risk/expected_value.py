"""The expected-value gate: a defined-risk credit spread must expect to make money, or it is refused.

Full reasoning, and the derivation of every floor, is in ``docs/EV-GATE.md``. The floors were fixed in
advance and are not tuned to any position; this module is deliberately written so that the arithmetic
which produced a verdict is recoverable from the record without re-running anything.

The identity this check is built on
-----------------------------------
Writing ``r`` for credit-to-width, the expected value of one vertical credit spread per unit of width
collapses to a single term::

    EV / width = POP x r - (1 - POP) x (1 - r) = POP - (1 - r)

Fair pricing means ``credit = width x P(max loss)``, so ``credit_to_width`` IS the market's own implied
probability that the spread LOSES, and ``1 - credit_to_width`` the probability it wins. A spread priced
at that probability has an expected value of exactly zero - substitute ``POP = 1 - r`` above and the
whole expression vanishes. There is no edge in the structure of a credit spread, only in the difference
between your probability estimate and the price's.

Two consequences drive the code below:

* A missing volatility input is a BLOCKING failure, not a shrug. Without an independent probability
  estimate the expected value is zero by construction and negative after costs, so "we could not
  estimate the probability" and "this trade has no edge" are the same statement here. E2 and the
  arithmetic agree for once.
* ``credit_to_width`` is checked FIRST, because it is the only floor computable from the marks alone.
  A refusal that carries the real arithmetic is worth more in the record than a refusal that carries
  only "an input was absent", and both are blocking, so the order changes the evidence and never the
  verdict.

Discipline
----------
Decimal only, end to end - the square root and the normal CDF included (Hard Rule A6 / INV-15). Pure:
no clock, no network, no LLM (A1, E8). Prices come from the snapshot's option MARKS, never from
``leg.limit_price`` (F-1/F-2) - an EV gate valued off the agent's own limit price would let the agent
choose its own verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mizan.contracts import (
    DECIMAL_CONTEXT,
    OPTION_CONTRACT_MULTIPLIER,
    CheckResult,
    Leg,
    Policy,
    ReasonCode,
    RiskContext,
    TradeProposal,
    dec,
    dstr,
)
from mizan.risk.checks import fail, missing, ok
from mizan.risk.valuation import (
    ONE,
    ZERO,
    add,
    days_to_expiry,
    divide,
    multiply,
    option_quote_for,
    quote_for,
    subtract,
)

__all__ = ["CHECK_ID", "EvArithmetic", "expected_value", "normal_cdf"]

CHECK_ID = "expected_value"

#: Calendar days, not trading days. An option decays over the calendar, and the choice is stated rather
#: than implied because it moves POP: 252 would make every sigma larger and every POP smaller.
DAYS_PER_YEAR = Decimal(365)

#: Beyond this many standard deviations the normal CDF is 1 to far more precision than anything else
#: here carries, and short-circuiting keeps ``exp`` away from its extremes.
Z_SATURATION = Decimal(40)

_PI = Decimal("3.14159265358979323846264338328")
_SQRT_TWO_PI = DECIMAL_CONTEXT.sqrt(DECIMAL_CONTEXT.multiply(Decimal(2), _PI))
_MINUS_HALF = Decimal("-0.5")

# Abramowitz & Stegun 26.2.17. Stated absolute error < 7.5e-8 - four orders of magnitude smaller than
# the error in the volatility input, which is the term that actually decides anything here.
_AS_P = Decimal("0.2316419")
_AS_B = (
    Decimal("0.319381530"),
    Decimal("-0.356563782"),
    Decimal("1.781477937"),
    Decimal("-1.821255978"),
    Decimal("1.330274429"),
)


def normal_cdf(z: Decimal) -> Decimal:
    """The standard normal CDF, in Decimal, deterministic across machines.

    ``Context.exp`` and ``Context.sqrt`` are correctly rounded to the context precision, so this
    returns the same digits on every platform - which is the whole reason it is not ``math.erf``.
    ``math`` would be binary floating point, and A6 forbids that anywhere near a recorded number.

    The result is clamped to [0, 1]: the approximation can land a few units in the last place outside
    it in the far tail, and a "probability" of 1.0000000002 would be a nonsense to store in a record.
    """
    if z >= Z_SATURATION:
        return ONE
    if z <= -Z_SATURATION:
        return ZERO
    if z < ZERO:
        return subtract(ONE, normal_cdf(-z))
    t = divide(ONE, add(ONE, multiply(_AS_P, z)))
    if t is None:  # pragma: no cover - unreachable: z >= 0 and _AS_P > 0 make the denominator >= 1
        return ZERO
    polynomial = ZERO
    power = t
    for coefficient in _AS_B:
        polynomial = add(polynomial, multiply(coefficient, power))
        power = multiply(power, t)
    density = divide(DECIMAL_CONTEXT.exp(multiply(multiply(z, z), _MINUS_HALF)), _SQRT_TWO_PI)
    if density is None:  # pragma: no cover - _SQRT_TWO_PI is a positive constant
        return ZERO
    value = subtract(ONE, multiply(density, polynomial))
    if value < ZERO:
        return ZERO
    return ONE if value > ONE else value


@dataclass(frozen=True)
class Vertical:
    """A two-leg vertical: one short leg, one long leg, same type and expiry, same size."""

    short_leg: Leg
    long_leg: Leg
    contract_type: str
    expiry: str


@dataclass(frozen=True)
class EvArithmetic:
    """Every intermediate number, so the record shows the working and not just the answer."""

    width: Decimal
    credit: Decimal
    credit_to_width: Decimal
    max_loss: Decimal
    market_implied_pop: Decimal


def _vertical(proposal: TradeProposal) -> Vertical | None:
    """The proposal as a two-leg vertical, or ``None`` when it is some other shape.

    Deliberately narrow. Condors, butterflies and calendars have a defined max loss too, but not one
    this arithmetic computes, and approximating them here would put a number in the record that nobody
    could re-derive. ``structure_valid`` is what guarantees defined risk; this check prices the subset
    it can price honestly and says so for the rest (docs/EV-GATE.md section 7).
    """
    if len(proposal.legs) != 2:
        return None
    first, second = proposal.legs
    if not (first.is_option and second.is_option):
        return None
    if first.contract_type != second.contract_type or first.expiry != second.expiry:
        return None
    if dec(first.quantity) != dec(second.quantity):
        return None
    if first.side == second.side:
        return None
    if first.strike is None or second.strike is None or dec(first.strike) == dec(second.strike):
        return None
    short_leg, long_leg = (first, second) if first.side == "sell" else (second, first)
    return Vertical(
        short_leg=short_leg,
        long_leg=long_leg,
        contract_type=str(first.contract_type),
        expiry=str(first.expiry),
    )


def _no_probability(detail: str) -> CheckResult:
    """The probability estimate could not be formed. Blocking, whatever the policy says.

    ``GREEKS_MISSING`` is the nearest honest code the frozen catalogue carries: implied volatility
    comes from the same OPRA entitlement as the greeks and is refused with them on this data tier
    (``policies/options-defined-risk.yaml``). A precise ``EXPECTED_VALUE_INPUT_MISSING`` is requested
    in ``ledger/requests.md``; until it exists the detail below carries the specific reason.
    """
    return missing(CHECK_ID, ReasonCode.GREEKS_MISSING, detail)


def _arithmetic(short_mark: Decimal, long_mark: Decimal, width: Decimal) -> EvArithmetic | None:
    credit = subtract(short_mark, long_mark)
    ratio = divide(credit, width)
    if ratio is None:  # pragma: no cover - guarded by the zero-width check before this is called
        return None
    return EvArithmetic(
        width=width,
        credit=credit,
        credit_to_width=ratio,
        max_loss=subtract(width, credit),
        market_implied_pop=subtract(ONE, ratio),
    )


def expected_value(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    """Refuse a credit spread whose expected value does not clear the policy's floors.

    Returns a blocking failure with the arithmetic in ``detail``, a passing result carrying the same
    arithmetic, or a passing result stating why the proposal is outside this check's scope. Never a
    bare pass: every path here carries its evidence (INV-26).
    """
    limits = policy.ev
    if limits is None:
        return ok(CHECK_ID, policy, detail="no expected-value floors configured")
    if proposal.asset_class != "equity_option":
        return ok(CHECK_ID, policy, detail="not an options proposal; no spread expectancy to compute")
    if proposal.intent == "close":
        # A gate that blocks an exit strands the position it exists to protect. Never gate a close.
        return ok(CHECK_ID, policy, detail="closing a position has no forward expectancy to gate")

    vertical = _vertical(proposal)
    if vertical is None:
        return ok(
            CHECK_ID,
            policy,
            actual=Decimal(len(proposal.legs)),
            detail=(
                f"{proposal.strategy} with {len(proposal.legs)} leg(s) is not a two-leg vertical; the "
                "expected-value arithmetic is defined for verticals only (docs/EV-GATE.md section 7)"
            ),
        )

    market = context.market_snapshot
    short_quote = option_quote_for(market, vertical.short_leg.occ_symbol(proposal.symbol))
    long_quote = option_quote_for(market, vertical.long_leg.occ_symbol(proposal.symbol))
    if short_quote is None or long_quote is None:
        absent = "short" if short_quote is None else "long"
        return missing(
            CHECK_ID,
            ReasonCode.PRICE_MISSING,
            f"no option mark for the {absent} leg; a spread cannot be priced from one side",
        )

    width = abs(subtract(dec(vertical.short_leg.strike or "0"), dec(vertical.long_leg.strike or "0")))
    if width <= ZERO:  # pragma: no cover - _vertical rejects equal strikes before this is reached
        return fail(
            CHECK_ID,
            policy,
            ReasonCode.STRUCTURE_INVALID,
            detail="the legs share a strike, so the spread has no width and no max loss",
        )

    numbers = _arithmetic(dec(short_quote.mark), dec(long_quote.mark), width)
    if numbers is None:  # pragma: no cover - width > 0 is established immediately above
        return _no_probability("the spread width is zero; credit-to-width is undefined")

    source = market.source if market is not None else None
    snapshot_ts = market.as_of if market is not None else None
    if numbers.credit <= ZERO:
        return ok(
            CHECK_ID,
            policy,
            actual=numbers.credit,
            source=source,
            snapshot_ts=snapshot_ts,
            detail=(
                f"net {dstr(abs(numbers.credit))} DEBIT per share at the marks, not a credit spread; "
                "this check's floors are defined for credit received (docs/EV-GATE.md section 4.4)"
            ),
        )

    # --- Floor 1: credit-to-width. The only floor computable from the marks alone, so it is checked
    # first: it produces the refusal that carries the most arithmetic.
    min_ratio = dec(limits.min_credit_to_width)
    structure = (
        f"width {dstr(numbers.width)}, credit {dstr(numbers.credit)}/share, "
        f"credit-to-width {dstr(numbers.credit_to_width)}, max loss {dstr(numbers.max_loss)}/share; "
        f"the market's implied probability of loss is {dstr(numbers.credit_to_width)}, so this trade "
        f"needs POP above {dstr(numbers.market_implied_pop)} merely to break even"
    )
    if numbers.credit_to_width < min_ratio:
        return fail(
            CHECK_ID,
            policy,
            ReasonCode.REWARD_RISK_BELOW_MINIMUM,
            threshold=min_ratio,
            actual=numbers.credit_to_width,
            source=source,
            snapshot_ts=snapshot_ts,
            detail=f"credit-to-width below the floor {dstr(min_ratio)}: {structure}",
        )

    # --- The probability estimate. Everything from here needs a volatility, and E2 plus the identity
    # in this module's docstring both say absence blocks.
    underlying = quote_for(market, proposal.symbol)
    if underlying is None:
        return missing(
            CHECK_ID,
            ReasonCode.PRICE_MISSING,
            f"no quote for {proposal.symbol}; the distance to the short strike cannot be measured",
        )
    spot = dec(underlying.price)
    remaining_days = Decimal(days_to_expiry(context, vertical.expiry))
    if remaining_days <= ZERO:
        return _no_probability(
            f"the {vertical.expiry} expiry leaves {dstr(remaining_days)} days; there is no remaining "
            "time over which to estimate a probability, so no expected value can be formed"
        )
    if short_quote.iv is None:
        return _no_probability(
            "no implied volatility on the short leg, and RiskContext carries no realized-vol input "
            "(see docs/EV-GATE.md section 5). A credit spread priced at the market's own implied "
            f"probability has an expected value of exactly zero: at credit-to-width "
            f"{dstr(numbers.credit_to_width)} this trade breaks even only if the true probability of "
            f"profit exceeds {dstr(numbers.market_implied_pop)}, and nothing in this record establishes "
            f"that it does. {structure}"
        )
    volatility = dec(short_quote.iv)
    if volatility <= ZERO:
        return _no_probability(
            f"the short leg reports volatility {dstr(volatility)}; a non-positive volatility cannot "
            "produce a probability"
        )

    years = divide(remaining_days, DAYS_PER_YEAR)
    if years is None:  # pragma: no cover - DAYS_PER_YEAR is a positive constant
        return _no_probability("the year fraction could not be computed")
    sigma = multiply(multiply(spot, volatility), DECIMAL_CONTEXT.sqrt(years))
    strike = dec(vertical.short_leg.strike or "0")
    # Signed on purpose. A short put profits above its strike, a short call below it, so a short strike
    # that is already through the money gives a NEGATIVE z and a POP under one half - which is exactly
    # what it should give. An absolute distance would report a breached strike as a safe one.
    edge = subtract(spot, strike) if vertical.contract_type == "put" else subtract(strike, spot)
    sigmas = divide(edge, sigma)
    if sigmas is None:
        return _no_probability("the volatility implies a zero move; no probability can be formed")
    pop = normal_cdf(sigmas)

    # --- Floor 2: POP. Is this the kind of trade it claims to be?
    evidence = (
        f"{structure}. spot {dstr(spot)}, short strike {dstr(strike)}, {dstr(remaining_days)}d to "
        f"{vertical.expiry}, vol {dstr(volatility)} -> 1 sigma {dstr(sigma)}, distance "
        f"{dstr(sigmas)} sigma, POP {dstr(pop)} (normal approximation; upper bound, see "
        f"docs/EV-GATE.md section 2.2)"
    )
    min_pop = dec(limits.min_pop)
    if pop < min_pop:
        return fail(
            CHECK_ID,
            policy,
            ReasonCode.REWARD_RISK_BELOW_MINIMUM,
            threshold=min_pop,
            actual=pop,
            source=source,
            snapshot_ts=snapshot_ts,
            detail=f"probability of profit below the floor {dstr(min_pop)}: {evidence}",
        )

    # --- Floor 3: the edge itself. EV = POP x credit - (1 - POP) x (width - credit).
    ev_per_share = subtract(
        multiply(pop, numbers.credit), multiply(subtract(ONE, pop), numbers.max_loss)
    )
    ev_per_spread = multiply(ev_per_share, OPTION_CONTRACT_MULTIPLIER)
    ev_ratio = divide(ev_per_share, numbers.max_loss)
    if ev_ratio is None:  # pragma: no cover - max_loss > 0 whenever credit < width, checked above
        return _no_probability("max loss is zero; expected value cannot be normalised")
    arithmetic = (
        f"EV {dstr(ev_per_share)}/share = {dstr(ev_per_spread)} per spread "
        f"({dstr(pop)} x {dstr(numbers.credit)} - {dstr(subtract(ONE, pop))} x {dstr(numbers.max_loss)}), "
        f"which is {dstr(ev_ratio)} of the {dstr(numbers.max_loss)} at risk. {evidence}"
    )
    min_ev = dec(limits.min_ev_to_max_loss)
    if ev_ratio < min_ev:
        return fail(
            CHECK_ID,
            policy,
            ReasonCode.REWARD_RISK_BELOW_MINIMUM,
            threshold=min_ev,
            actual=ev_ratio,
            source=source,
            snapshot_ts=snapshot_ts,
            detail=f"expected value below the floor {dstr(min_ev)} of capital at risk: {arithmetic}",
        )

    return ok(
        CHECK_ID,
        policy,
        threshold=min_ev,
        actual=ev_ratio,
        source=source,
        snapshot_ts=snapshot_ts,
        detail=f"clears every expected-value floor. {arithmetic}",
    )
