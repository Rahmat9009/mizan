"""Realized-volatility signal - SHADOW ONLY.

What this package is
--------------------
A volatility reading computed from daily price bars: ``realized_vol_rank`` (a percentile in [0, 100]),
``atr`` (Wilder, 14) and a ``regime`` label (HIGH / MID / LOW). It exists so the seam between a market
signal and a governed decision is built, tested and auditable.

What this package is NOT
------------------------
It is not implied volatility. The data entitlement available to this project returns no greeks and no
implied volatility - the options-data request is refused outright - so an implied number cannot be
computed here. ``realized_vol_rank`` is named for what it measures, and calling it an "IV rank" would
misdescribe the input to anyone reading a stored decision.

It also has no authority. The reading cannot cause, size, approve, delay or block an order. It reaches
the record through ``VolSignalAdvisoryProvider``, which changes exactly one field of another
provider's opinion - ``reasoning`` - a field the deterministic path is forbidden to read (invariant 17)
and which is not an input to ``verdict_hash``. Default OFF; ``SIGNAL_SHADOW=1`` enables the text.

Usage::

    from mizan.signal import compute_vol_signal, fetch_daily_bars

    bars = fetch_daily_bars("SPY")          # network, once, at the edge
    reading = compute_vol_signal(bars, symbol="SPY")   # pure, Decimal-only, deterministic
"""

from __future__ import annotations

from mizan.signal.advisory import SHADOW_PROFILE, VolSignalAdvisoryProvider, annotate
from mizan.signal.bars import Bar, BarDataError, parse_bars, parse_bars_payload
from mizan.signal.shadow import SHADOW_ENV, shadow_enabled
from mizan.signal.source import (
    DEFAULT_SYMBOL,
    MARKET_DATA_HOST,
    MarketDataUnavailable,
    MissingCredentials,
    bars_url,
    fetch_daily_bars,
)
from mizan.signal.vol import (
    ANNUALIZATION_DAYS,
    ATR_PERIOD,
    HIGH_RANK_THRESHOLD,
    LOW_RANK_THRESHOLD,
    RANK_LOOKBACK,
    REALIZED_VOL_WINDOW,
    SIGNAL_METHOD,
    InsufficientBars,
    VolSignal,
    compute_vol_signal,
    minimum_bars,
)

__all__ = [
    "ANNUALIZATION_DAYS",
    "ATR_PERIOD",
    "DEFAULT_SYMBOL",
    "HIGH_RANK_THRESHOLD",
    "LOW_RANK_THRESHOLD",
    "MARKET_DATA_HOST",
    "RANK_LOOKBACK",
    "REALIZED_VOL_WINDOW",
    "SHADOW_ENV",
    "SHADOW_PROFILE",
    "SIGNAL_METHOD",
    "Bar",
    "BarDataError",
    "InsufficientBars",
    "MarketDataUnavailable",
    "MissingCredentials",
    "VolSignal",
    "VolSignalAdvisoryProvider",
    "annotate",
    "bars_url",
    "compute_vol_signal",
    "fetch_daily_bars",
    "minimum_bars",
    "parse_bars",
    "parse_bars_payload",
    "shadow_enabled",
]
