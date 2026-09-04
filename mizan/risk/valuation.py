"""Pure decimal helpers shared by the checks: prices, exposure, greeks and caps.

**Valuation comes from the context snapshots, never from the proposal** (security findings F-1/F-2).
``leg.limit_price`` is an execution bound the agent chose; using it to decide how much capital is at
risk is exactly the bypass that let 1,000 shares at a claimed price of one cent through the legacy
gate. Every notional, exposure, concentration and buying-power figure below is computed from
``market_snapshot.quotes[symbol].price`` and ``market_snapshot.option_quotes[occ].mark``. The limit
price is read in one place only -- the ``erroneous_order`` check, whose whole job is to compare what
the agent asked for against what the market says.

Missing prices return ``None`` and a reason code; they never fall back to zero (Hard Rule E2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from mizan.contracts import (
    DECIMAL_CONTEXT,
    OPTION_CONTRACT_MULTIPLIER,
    Leg,
    MarketSnapshot,
    OptionQuote,
    PortfolioSnapshot,
    Quote,
    ReasonCode,
    RiskContext,
    TradeProposal,
    dec,
    parse_ts,
)

ZERO = Decimal(0)
ONE = Decimal(1)
BASIS_POINTS = Decimal(10000)
HALF = Decimal("0.5")

__all__ = [
    "BASIS_POINTS",
    "HALF",
    "ONE",
    "ZERO",
    "Exposure",
    "add",
    "apply_multiplier",
    "cap_from_budget",
    "days_to_expiry",
    "divide",
    "equity_of",
    "evaluated_datetime",
    "exposure_of",
    "floor_units",
    "greek_change",
    "gross_exposure_of",
    "leg_reference_price",
    "minutes_of_day",
    "minutes_of_hhmm",
    "multiply",
    "option_quote_for",
    "portfolio_greek",
    "quote_for",
    "sector_exposure",
    "sector_of",
    "signed_quantity",
    "subtract",
    "symbol_exposure",
]


# ----------------------------------------------------------------------------------------------------
# Deterministic arithmetic
# ----------------------------------------------------------------------------------------------------
def add(left: Decimal, right: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.add(left, right)


def subtract(left: Decimal, right: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.subtract(left, right)


def multiply(left: Decimal, right: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.multiply(left, right)


def divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Exact division, or ``None`` when the denominator is zero (never a trapped exception)."""
    if denominator == ZERO:
        return None
    return DECIMAL_CONTEXT.divide(numerator, denominator)


def floor_units(value: Decimal) -> Decimal:
    """Whole units, rounding down; a negative value floors to zero (a cap is never negative)."""
    if value <= ZERO:
        return ZERO
    return value.to_integral_value(rounding=ROUND_FLOOR)


def apply_multiplier(quantity: Decimal, multiplier: Decimal) -> Decimal:
    """``floor(quantity x multiplier)`` -- the reduction form of Addendum 1 section C."""
    return floor_units(multiply(quantity, multiplier))


def cap_from_budget(budget: Decimal, unit_cost: Decimal) -> Decimal:
    """Whole units affordable within ``budget`` at ``unit_cost`` per unit."""
    if unit_cost <= ZERO:
        return ZERO
    quotient = divide(budget, unit_cost)
    return ZERO if quotient is None else floor_units(quotient)


# ----------------------------------------------------------------------------------------------------
# Market data lookups (the only source of valuation)
# ----------------------------------------------------------------------------------------------------
def quote_for(market: MarketSnapshot | None, symbol: str) -> Quote | None:
    if market is None:
        return None
    return market.quotes.get(symbol)


def option_quote_for(market: MarketSnapshot | None, occ_symbol: str) -> OptionQuote | None:
    if market is None:
        return None
    return market.option_quotes.get(occ_symbol)


def sector_of(market: MarketSnapshot | None, symbol: str) -> str | None:
    if market is None:
        return None
    return market.sectors.get(symbol)


def signed_quantity(leg: Leg) -> Decimal:
    """``+quantity`` for a buy, ``-quantity`` for a sell."""
    quantity = dec(leg.quantity)
    return quantity if leg.side == "buy" else -quantity


def leg_multiplier(proposal: TradeProposal) -> Decimal:
    return OPTION_CONTRACT_MULTIPLIER if proposal.asset_class == "equity_option" else ONE


def leg_reference_price(proposal: TradeProposal, leg: Leg, market: MarketSnapshot | None) -> Decimal | None:
    """The market's price for one unit of this leg. Never the agent's limit price (F-1)."""
    if leg.is_option:
        option_quote = option_quote_for(market, leg.occ_symbol(proposal.symbol))
        return None if option_quote is None else dec(option_quote.mark)
    quote = quote_for(market, proposal.symbol)
    return None if quote is None else dec(quote.price)


@dataclass(frozen=True)
class Exposure:
    """What the order is worth at market, and which way it moves the book.

    ``gross`` is the absolute market value of every leg (what the order costs to put on), ``change``
    is signed (positive when the order increases exposure), and ``unit_gross`` is the gross value of
    one unit of ``proposal.total_quantity`` -- the divisor that turns a money cap into a unit cap.
    """

    gross: Decimal
    change: Decimal
    unit_gross: Decimal
    missing_code: ReasonCode | None = None
    opening: Decimal = ZERO
    """Market value of the part of this order that INCREASES a position, net of what it offsets.

    ``change`` is a signed cash flow and is the wrong question to ask about capital. Selling to open
    receives cash, so ``change`` is negative, and every check that asked "is change positive?" as a
    proxy for "does this consume capital?" concluded that a naked short consumes none - which switched
    off buying-power sufficiency, buying-power utilisation, concentration and sector concentration all
    at once, on the single riskiest shape a proposal can have (F-30).

    A short receives cash AND consumes margin; the two are not alternatives. So capital is measured by
    what the order OPENS: quantity that increases the absolute size of the position it touches, with
    quantity that offsets an existing holding excluded because closing genuinely frees capital. A
    proposal that flips through flat counts only the part that comes out the other side.
    """

    @property
    def priced(self) -> bool:
        return self.missing_code is None

    @property
    def increases_risk(self) -> bool:
        """Whether the order adds exposure - by magnitude, not by direction of cash."""
        return self.opening > ZERO


def held_quantities(portfolio: PortfolioSnapshot | None) -> dict[str, Decimal]:
    """Signed quantity per instrument, keyed the way a leg identifies itself."""
    quantities: dict[str, Decimal] = {}
    if portfolio is None:
        return quantities
    for position in portfolio.positions:
        key = position.occ_symbol or position.symbol
        quantities[key] = add(quantities.get(key, ZERO), dec(position.quantity))
    return quantities


def opening_quantity(held: Decimal, proposed: Decimal) -> Decimal:
    """How much of ``proposed`` increases ``|held|`` rather than reducing it. Both signed."""
    if proposed == ZERO:
        return ZERO
    if held == ZERO or (held > ZERO) == (proposed > ZERO):
        return abs(proposed)
    # Opposite directions: the overlap closes, and anything beyond it opens on the other side.
    return max(ZERO, abs(proposed) - abs(held))


def exposure_of(proposal: TradeProposal, context: RiskContext) -> Exposure:
    """Market value of the proposed order, from the context's snapshots only."""
    market = context.market_snapshot
    if market is None:
        return Exposure(ZERO, ZERO, ZERO, ReasonCode.MARKET_DATA_MISSING)
    multiplier = leg_multiplier(proposal)
    held = held_quantities(context.portfolio_snapshot)
    # Addendum 1 C: a close never increases risk, and the engine already says so in
    # `is_risk_increasing`. Measuring a close against the portfolio instead would make one that the
    # snapshot cannot corroborate - a partial snapshot, a position held elsewhere - read as an opening
    # short and get scaled to zero, which strands the exit. A close MISLABELLED to smuggle a short in
    # is caught by shape rather than by capital: `structure_valid` and the naked-short check refuse
    # the structure whatever the intent field claims.
    closing_out = proposal.intent == "close"
    gross = ZERO
    change = ZERO
    opening = ZERO
    for leg in proposal.legs:
        price = leg_reference_price(proposal, leg, market)
        if price is None:
            return Exposure(ZERO, ZERO, ZERO, ReasonCode.PRICE_MISSING)
        value = multiply(multiply(dec(leg.quantity), price), multiplier)
        gross = add(gross, value)
        signed = signed_quantity(leg)
        change = add(change, multiply(multiply(signed, price), multiplier))
        # An option leg is identified by its contract; an equity leg by its symbol. Asking a leg for
        # an OCC symbol it cannot have raises, so the asset class decides, not a try.
        key = (
            leg.occ_symbol(proposal.symbol)
            if proposal.asset_class == "equity_option"
            else proposal.symbol
        )
        current = held.get(key, ZERO)
        added = ZERO if closing_out else opening_quantity(current, signed)
        opening = add(opening, multiply(multiply(added, price), multiplier))
        # Later legs see what earlier legs did, so a spread that closes then reopens is measured once.
        held[key] = add(current, signed)
    unit = divide(gross, proposal.total_quantity)
    return Exposure(gross, change, ZERO if unit is None else unit, opening=opening)


# ----------------------------------------------------------------------------------------------------
# Portfolio views
# ----------------------------------------------------------------------------------------------------
def equity_of(portfolio: PortfolioSnapshot | None) -> Decimal | None:
    return None if portfolio is None else dec(portfolio.equity)


def gross_exposure_of(portfolio: PortfolioSnapshot | None) -> Decimal | None:
    """The snapshot's gross exposure, or the sum of absolute position values when it is absent."""
    if portfolio is None:
        return None
    if portfolio.gross_exposure is not None:
        return dec(portfolio.gross_exposure)
    total = ZERO
    for position in portfolio.positions:
        total = add(total, abs(dec(position.market_value)))
    return total


def symbol_exposure(portfolio: PortfolioSnapshot, symbol: str) -> Decimal:
    """Absolute market value already held in one symbol (its options included)."""
    total = ZERO
    for position in portfolio.positions:
        if position.symbol == symbol:
            total = add(total, abs(dec(position.market_value)))
    return total


def sector_exposure(portfolio: PortfolioSnapshot, market: MarketSnapshot | None, sector: str) -> Decimal:
    """Absolute market value held in one sector, using the position's sector then the snapshot's map."""
    total = ZERO
    for position in portfolio.positions:
        position_sector = position.sector or sector_of(market, position.symbol)
        if position_sector == sector:
            total = add(total, abs(dec(position.market_value)))
    return total


def portfolio_greek(portfolio: PortfolioSnapshot | None, name: str) -> Decimal | None:
    if portfolio is None or portfolio.greeks is None:
        return None
    value = getattr(portfolio.greeks, name)
    return None if value is None else dec(value)


def greek_change(proposal: TradeProposal, context: RiskContext, name: str) -> Decimal | None:
    """Signed change in one portfolio greek from this order, or ``None`` when a leg greek is absent."""
    market = context.market_snapshot
    if market is None:
        return None
    total = ZERO
    for leg in proposal.legs:
        if not leg.is_option:
            return None
        option_quote = option_quote_for(market, leg.occ_symbol(proposal.symbol))
        if option_quote is None:
            return None
        value = getattr(option_quote, name)
        if value is None:
            return None
        contribution = multiply(multiply(signed_quantity(leg), dec(value)), OPTION_CONTRACT_MULTIPLIER)
        total = add(total, contribution)
    return total


# ----------------------------------------------------------------------------------------------------
# Calendar
# ----------------------------------------------------------------------------------------------------
def evaluated_datetime(context: RiskContext) -> datetime:
    return parse_ts(context.evaluated_at)


def days_to_expiry(context: RiskContext, expiry: str) -> int:
    return (date.fromisoformat(expiry) - evaluated_datetime(context).date()).days


def minutes_of_day(context: RiskContext) -> int:
    moment = evaluated_datetime(context)
    return moment.hour * 60 + moment.minute


def minutes_of_hhmm(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)
