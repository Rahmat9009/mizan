"""Daily price bars as text, never as binary floating point.

The venue's market-data endpoint returns OHLCV as JSON *numbers*, which is the one place a float can
enter this lane without anybody writing one. ``parse_bars`` therefore decodes the payload with
``json.loads(..., parse_float=Decimal, parse_int=Decimal)`` so a quoted price becomes a ``Decimal``
built from the digits the venue printed, and stores every number as a normalised DecimalStr. Nothing
downstream ever sees a binary float, and the same payload always yields the same bars.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mizan.contracts.types import dec, dstr

__all__ = ["Bar", "BarDataError", "parse_bars", "parse_bars_payload"]


class BarDataError(ValueError):
    """The payload is not a usable series of daily bars. Never a partial or guessed series."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily bar. Every price is a normalised DecimalStr; ``day`` is the bar's UTC calendar date."""

    day: str
    open: str
    high: str
    low: str
    close: str
    volume: str

    def as_dict(self) -> dict[str, str]:
        return {
            "day": self.day,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    @property
    def high_d(self) -> Decimal:
        return dec(self.high)

    @property
    def low_d(self) -> Decimal:
        return dec(self.low)

    @property
    def close_d(self) -> Decimal:
        return dec(self.close)


def _number(raw: Any, field: str, where: str) -> str:
    """Normalise one venue number to a DecimalStr without ever constructing a binary float.

    ``parse_bars_payload`` decodes with ``parse_float=Decimal``, so a well-formed payload arrives here
    as ``Decimal`` already. A string is accepted too (some venues quote numbers as text). Anything
    else - including a genuine ``bool`` or ``None`` - is a data error, not something to coerce.
    """
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            raise BarDataError(f"{where}: {field} is not a finite number")
        return dstr(raw)
    if isinstance(raw, str):
        try:
            return dstr(dec(raw.strip()))
        except (TypeError, ValueError) as exc:
            raise BarDataError(f"{where}: {field} is not a decimal: {raw!r}") from exc
    if isinstance(raw, int) and not isinstance(raw, bool):
        return dstr(Decimal(raw))
    raise BarDataError(f"{where}: {field} has unusable type {type(raw).__name__}")


def _day(raw: Any, where: str) -> str:
    """The bar's UTC calendar date, taken from the RFC 3339 timestamp the venue stamps on it."""
    if not isinstance(raw, str) or len(raw) < 10:
        raise BarDataError(f"{where}: bar timestamp is missing or unusable")
    day = raw[:10]
    parts = day.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise BarDataError(f"{where}: bar timestamp is not a calendar date: {raw!r}")
    return day


def parse_bars(rows: Any) -> tuple[Bar, ...]:
    """Turn a decoded list of venue bar objects into ``Bar`` values, oldest first.

    Duplicate calendar days are collapsed to the last occurrence, and the result is sorted by day, so
    the same set of bars in any page order produces one identical series (Determinism, Master Plan A1).
    """
    if not isinstance(rows, list):
        raise BarDataError(f"expected a list of bars, got {type(rows).__name__}")
    by_day: dict[str, Bar] = {}
    for index, row in enumerate(rows):
        where = f"bar[{index}]"
        if not isinstance(row, dict):
            raise BarDataError(f"{where}: expected an object, got {type(row).__name__}")
        day = _day(row.get("t"), where)
        bar = Bar(
            day=day,
            open=_number(row.get("o"), "o", where),
            high=_number(row.get("h"), "h", where),
            low=_number(row.get("l"), "l", where),
            close=_number(row.get("c"), "c", where),
            volume=_number(row.get("v", Decimal(0)), "v", where),
        )
        if dec(bar.close) <= 0 or dec(bar.high) < dec(bar.low):
            raise BarDataError(f"{where}: incoherent bar (close<=0 or high<low)")
        by_day[day] = bar
    return tuple(by_day[day] for day in sorted(by_day))


def parse_bars_payload(body: str) -> tuple[tuple[Bar, ...], str | None]:
    """Decode one page of the venue's bars response. Returns ``(bars, next_page_token)``.

    ``parse_float=Decimal`` is the whole point of this function: it is the only line standing between
    a JSON number and a binary float in this lane.
    """
    try:
        payload = json.loads(body, parse_float=Decimal, parse_int=Decimal)
    except ValueError as exc:
        raise BarDataError(f"market-data response is not JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise BarDataError(f"expected a JSON object, got {type(payload).__name__}")
    token = payload.get("next_page_token")
    if token is not None and not isinstance(token, str):
        token = None
    return parse_bars(payload.get("bars") or []), token
