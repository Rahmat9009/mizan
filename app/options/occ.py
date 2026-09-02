"""OCC option-symbol parsing.

Alpaca identifies an option contract by its OCC symbol, for example
``AAPL261016C00230000``. The symbol is self-describing: it carries the root,
the expiry, the contract type, and the strike. Parsing it lets the backend
cross-check every declared leg field against the symbol the broker will
actually be asked to trade, so a proposal cannot claim one strike and submit
another.

This module is pure: no I/O, no broker calls, no dependency on the rest of the
application. Broker-side confirmation of a contract happens later, at execution
time, against ``get_option_contract``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Root (1-6), expiry YYMMDD, C or P, strike in thousandths padded to 8 digits.
OCC_PATTERN = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9]{0,5})"
    r"(?P<expiry>\d{6})"
    r"(?P<kind>[CP])"
    r"(?P<strike>\d{8})$"
)

STRIKE_SCALE = Decimal(1000)


class OccSymbolError(ValueError):
    """An OCC option symbol was malformed or internally impossible."""


@dataclass(frozen=True)
class OccSymbol:
    """The decoded content of one OCC option symbol."""

    symbol: str
    root: str
    expiry: date
    option_type: str  # "CALL" or "PUT"
    strike: float


def parse_occ_symbol(raw: str) -> OccSymbol:
    """Decode an OCC symbol, or raise ``OccSymbolError``."""

    if not isinstance(raw, str):
        raise OccSymbolError("Option symbol must be a string.")
    symbol = raw.strip().upper()
    if not symbol:
        raise OccSymbolError("Option symbol must not be blank.")

    match = OCC_PATTERN.fullmatch(symbol)
    if match is None:
        raise OccSymbolError(
            f"Option symbol {symbol!r} is not a valid OCC symbol "
            "(expected ROOT + YYMMDD + C/P + 8-digit strike)."
        )

    digits = match.group("expiry")
    try:
        expiry = date(2000 + int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError as exc:
        raise OccSymbolError(f"Option symbol {symbol!r} encodes an impossible date.") from exc

    strike = Decimal(match.group("strike")) / STRIKE_SCALE
    if strike <= 0:
        raise OccSymbolError(f"Option symbol {symbol!r} encodes a non-positive strike.")

    return OccSymbol(
        symbol=symbol,
        root=match.group("root"),
        expiry=expiry,
        option_type="CALL" if match.group("kind") == "C" else "PUT",
        strike=float(strike),
    )


def strikes_equal(left: float, right: float) -> bool:
    """Compare strikes at the resolution OCC symbols can actually express."""

    return abs(left - right) < 0.0005
