"""The signal itself: a pure, Decimal-only function from daily bars to a volatility reading.

Three deliberate constraints, each one a correctness requirement rather than a style choice.

**It is named for what it measures.** ``realized_vol_rank`` is the percentile rank of *realized*
close-to-close volatility. It is NOT an IV rank. The data tier this project is entitled to returns no
greeks and no implied volatility at all, so an implied number cannot be computed here and must not be
implied by a name. A reader who sees "IV rank" and gets realized vol has been told something false
about the input, and that error would survive every test in this repository.

**It is pure.** ``compute_vol_signal`` takes bars and returns a reading. No clock, no environment, no
socket: fetching is a separate step in ``mizan.signal.source``. Same bars in, byte-identical reading
out, forever.

**It is Decimal-only.** Every number is a ``Decimal`` evaluated in ``DECIMAL_CONTEXT`` (precision 28,
trapping InvalidOperation / DivisionByZero / Overflow) and leaves as a normalised DecimalStr, in line
with Hard Rule A6. ``Decimal.ln`` and ``Context.sqrt`` are correctly-rounded decimal operations; there
is no ``math`` import and no binary float anywhere in this package.

The reading has ZERO authority. Nothing here approves, sizes, blocks or reduces an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mizan.contracts.canonical import DECIMAL_CONTEXT, canonical_json, sha256_hex
from mizan.contracts.types import dec, dstr
from mizan.signal.bars import Bar

__all__ = [
    "ANNUALIZATION_DAYS",
    "ATR_PERIOD",
    "HIGH_RANK_THRESHOLD",
    "LOW_RANK_THRESHOLD",
    "MIN_RANK_OBSERVATIONS",
    "RANK_LOOKBACK",
    "REALIZED_VOL_WINDOW",
    "SIGNAL_METHOD",
    "InsufficientBars",
    "VolSignal",
    "compute_vol_signal",
]

#: Trading days per year, the conventional annualisation factor for daily returns.
ANNUALIZATION_DAYS = 252
#: Length in trading days of one realized-volatility observation (about one month).
REALIZED_VOL_WINDOW = 21
#: How many realized-volatility observations the percentile rank is taken over (about one year).
RANK_LOOKBACK = 252
#: Below this many observations a percentile rank is not meaningful and the signal refuses to produce one.
MIN_RANK_OBSERVATIONS = 60
#: Wilder's ATR period.
ATR_PERIOD = 14

#: Regime bands on ``realized_vol_rank``. HIGH is the premium-selling-favourable half.
HIGH_RANK_THRESHOLD = Decimal(50)
LOW_RANK_THRESHOLD = Decimal(25)

#: Identifies exactly which computation produced a reading, so a stored reading is interpretable later.
SIGNAL_METHOD = "rv21-close-to-close-annualized-252/pctrank-252/atr14-wilder"

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
_RANK_EXP = Decimal("0.01")
_ATR_EXP = Decimal("0.0001")
_VOL_EXP = Decimal("0.000001")

REGIME_HIGH = "HIGH"
REGIME_MID = "MID"
REGIME_LOW = "LOW"


class InsufficientBars(ValueError):
    """Too few bars to compute the reading. The signal refuses rather than extrapolating."""


@dataclass(frozen=True, slots=True)
class VolSignal:
    """A volatility reading over a fixed bar series. Advisory metadata only - it authorises nothing.

    ``realized_vol_rank`` is a DecimalStr percentile in [0, 100]; ``atr`` is in the instrument's own
    price units; ``regime`` is HIGH / MID / LOW.
    """

    symbol: str
    as_of: str
    realized_vol_rank: str
    realized_vol: str
    atr: str
    regime: str
    bars_used: int
    method: str

    def as_dict(self) -> dict[str, Any]:
        """The reading as a plain, canonically serialisable mapping (keys are sorted by canonical_json)."""
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "realized_vol_rank": self.realized_vol_rank,
            "realized_vol": self.realized_vol,
            "atr": self.atr,
            "regime": self.regime,
            "bars_used": self.bars_used,
            "method": self.method,
        }

    def canonical(self) -> str:
        """Canonical JSON of the reading - the byte string determinism is asserted on."""
        return canonical_json(self.as_dict())

    def digest(self) -> str:
        """SHA-256 of the canonical form, so two readings can be compared by one value."""
        return sha256_hex(self.canonical())

    def summary(self) -> str:
        """One line of advisory prose. This text is audit-only; no check ever reads it (INV-17)."""
        return (
            f"vol-signal-shadow[{self.symbol} {self.as_of}]: "
            f"realized_vol_rank={self.realized_vol_rank} "
            f"realized_vol={self.realized_vol} atr={self.atr} regime={self.regime} "
            f"bars={self.bars_used} method={self.method}. "
            "Realized-volatility proxy from price bars, not implied volatility. "
            "Advisory metadata with no authority: it did not size, approve or block this order."
        )


# ----------------------------------------------------------------------------------------------------------
# Decimal-only arithmetic helpers. Every one evaluates in DECIMAL_CONTEXT so an overflow or a division
# by zero raises here rather than producing a quiet NaN that would look like a number downstream.
# ----------------------------------------------------------------------------------------------------------


def _total(values: list[Decimal]) -> Decimal:
    running = _ZERO
    for value in values:
        running = DECIMAL_CONTEXT.add(running, value)
    return running


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise InsufficientBars("cannot take the mean of an empty series")
    return DECIMAL_CONTEXT.divide(_total(values), Decimal(len(values)))


def _sample_stdev(values: list[Decimal]) -> Decimal:
    """Sample standard deviation (n-1). Bessel's correction, because a window is a sample of a regime."""
    count = len(values)
    if count < 2:
        raise InsufficientBars("standard deviation needs at least two observations")
    mean = _mean(values)
    deviations = [DECIMAL_CONTEXT.subtract(v, mean) for v in values]
    squares = [DECIMAL_CONTEXT.multiply(d, d) for d in deviations]
    variance = DECIMAL_CONTEXT.divide(_total(squares), Decimal(count - 1))
    if variance < _ZERO:
        variance = _ZERO
    return DECIMAL_CONTEXT.sqrt(variance)


def _log_returns(closes: list[Decimal]) -> list[Decimal]:
    """Close-to-close log returns. A non-positive close is a data error, not a return of minus infinity."""
    returns: list[Decimal] = []
    for previous, current in zip(closes, closes[1:], strict=False):
        if previous <= _ZERO or current <= _ZERO:
            raise InsufficientBars("a non-positive close cannot produce a log return")
        returns.append(DECIMAL_CONTEXT.ln(DECIMAL_CONTEXT.divide(current, previous)))
    return returns


def _annualized(stdev: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.multiply(stdev, DECIMAL_CONTEXT.sqrt(Decimal(ANNUALIZATION_DAYS)))


def _realized_vol_series(closes: list[Decimal]) -> list[Decimal]:
    """Annualised realized volatility for every rolling window that fits, oldest first."""
    returns = _log_returns(closes)
    if len(returns) < REALIZED_VOL_WINDOW:
        raise InsufficientBars(
            f"need at least {REALIZED_VOL_WINDOW + 1} bars for one realized-volatility observation, "
            f"got {len(closes)}"
        )
    series: list[Decimal] = []
    for end in range(REALIZED_VOL_WINDOW, len(returns) + 1):
        window = returns[end - REALIZED_VOL_WINDOW : end]
        series.append(_annualized(_sample_stdev(window)))
    return series


def _percentile_rank(series: list[Decimal], current: Decimal) -> Decimal:
    """Share of the lookback at or below ``current``, as a percentage.

    ``current`` is itself the last member of ``series``, so the rank is in (0, 100]: a reading can be
    the highest volatility of the year (100) but is never below its own observation.
    """
    at_or_below = Decimal(sum(1 for value in series if value <= current))
    return DECIMAL_CONTEXT.divide(
        DECIMAL_CONTEXT.multiply(at_or_below, _HUNDRED), Decimal(len(series))
    )


def _true_ranges(bars: tuple[Bar, ...]) -> list[Decimal]:
    """Wilder's true range for every bar after the first."""
    ranges: list[Decimal] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        previous_close = previous.close_d
        high_low = DECIMAL_CONTEXT.subtract(current.high_d, current.low_d)
        high_close = DECIMAL_CONTEXT.abs(DECIMAL_CONTEXT.subtract(current.high_d, previous_close))
        low_close = DECIMAL_CONTEXT.abs(DECIMAL_CONTEXT.subtract(current.low_d, previous_close))
        ranges.append(max(high_low, high_close, low_close))
    return ranges


def _wilder_atr(bars: tuple[Bar, ...]) -> Decimal:
    """ATR(14) by Wilder's smoothing: a simple mean to seed it, then ((n-1)*prev + tr) / n."""
    ranges = _true_ranges(bars)
    if len(ranges) < ATR_PERIOD:
        raise InsufficientBars(f"need at least {ATR_PERIOD + 1} bars for ATR({ATR_PERIOD})")
    period = Decimal(ATR_PERIOD)
    atr = _mean(ranges[:ATR_PERIOD])
    for true_range in ranges[ATR_PERIOD:]:
        carried = DECIMAL_CONTEXT.multiply(atr, DECIMAL_CONTEXT.subtract(period, _ONE))
        atr = DECIMAL_CONTEXT.divide(DECIMAL_CONTEXT.add(carried, true_range), period)
    return atr


def _regime(rank: Decimal) -> str:
    if rank >= HIGH_RANK_THRESHOLD:
        return REGIME_HIGH
    if rank >= LOW_RANK_THRESHOLD:
        return REGIME_MID
    return REGIME_LOW


def _quantized(value: Decimal, exponent: Decimal) -> str:
    return dstr(DECIMAL_CONTEXT.quantize(value, exponent))


# ----------------------------------------------------------------------------------------------------------
# The signal
# ----------------------------------------------------------------------------------------------------------


def minimum_bars() -> int:
    """The smallest bar count ``compute_vol_signal`` accepts, given the configured windows."""
    return REALIZED_VOL_WINDOW + MIN_RANK_OBSERVATIONS


def compute_vol_signal(bars: tuple[Bar, ...] | list[Bar], *, symbol: str) -> VolSignal:
    """``(bars) -> {realized_vol_rank, atr, regime}``: pure, deterministic, Decimal-only, no network.

    ``bars`` are daily bars, oldest first, as produced by ``mizan.signal.bars.parse_bars``. The reading
    is taken as of the last bar. Too few bars raises ``InsufficientBars``; the signal never guesses.
    """
    series = tuple(bars)
    if len(series) < minimum_bars():
        raise InsufficientBars(
            f"need at least {minimum_bars()} daily bars for a {RANK_LOOKBACK}-day percentile rank "
            f"over {REALIZED_VOL_WINDOW}-day realized volatility, got {len(series)}"
        )
    days = [bar.day for bar in series]
    if days != sorted(days) or len(set(days)) != len(days):
        raise InsufficientBars("bars must be strictly ordered by day, oldest first, with no duplicates")

    closes = [bar.close_d for bar in series]
    realized = _realized_vol_series(closes)
    lookback = realized[-RANK_LOOKBACK:]
    current = lookback[-1]
    rank = _percentile_rank(lookback, current)
    atr = _wilder_atr(series)

    rank_text = _quantized(rank, _RANK_EXP)
    return VolSignal(
        symbol=symbol,
        as_of=series[-1].day,
        realized_vol_rank=rank_text,
        realized_vol=_quantized(current, _VOL_EXP),
        atr=_quantized(atr, _ATR_EXP),
        regime=_regime(dec(rank_text)),
        bars_used=len(series),
        method=SIGNAL_METHOD,
    )
