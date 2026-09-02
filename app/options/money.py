"""Decimal money arithmetic and the declared-value tolerance policy.

Option economics are built from strikes and premiums that arrive as floats.
Binary floats cannot represent most cent values exactly, so every calculation
that decides how much capital is at risk converts to ``Decimal`` first, via
``str`` so ``1.35`` becomes ``Decimal("1.35")`` rather than
``Decimal("1.350000000000000088817841970012523233890533447265625")``.

Nothing here imports the rest of the application. It is arithmetic.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

# Money is reported to the cent.
CENT = Decimal("0.01")
# Percentages keep enough resolution to compare against a policy threshold
# without the comparison itself introducing rounding error.
RATIO_EXPONENT = Decimal("0.000001")

# Declared-value tolerance. A declared figure matches the recomputed one when it
# is within a cent, or within a tenth of a percent for larger sums where a cent
# is unreasonably tight. Both are policy, and both are documented here so there
# is one place to change them.
ABSOLUTE_MONEY_TOLERANCE = Decimal("0.01")
RELATIVE_MONEY_TOLERANCE = Decimal("0.001")


class MoneyError(ValueError):
    """A value that had to be money was not usable as money."""


def to_decimal(value: Any) -> Decimal:
    """Convert to Decimal through str, preserving the decimal literal."""

    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MoneyError(f"{value!r} is not a usable numeric value.") from exc
    if not decimal_value.is_finite():
        raise MoneyError("Money values must be finite.")
    return decimal_value


def to_money(value: Any) -> Decimal:
    """Round to the cent, half away from zero, the way money is quoted."""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_ratio(value: Any) -> Decimal:
    """Round a proportion to six places, well below any policy threshold."""

    return to_decimal(value).quantize(RATIO_EXPONENT, rounding=ROUND_HALF_UP)


def money_equal(
    left: Any,
    right: Any,
    *,
    absolute: Decimal = ABSOLUTE_MONEY_TOLERANCE,
    relative: Decimal = RELATIVE_MONEY_TOLERANCE,
) -> bool:
    """Whether two money values agree under the declared-value tolerance.

    Equal when the absolute difference is within ``absolute``, or when the
    relative difference against the larger magnitude is within ``relative``.
    The relative branch exists so a five-figure position is not rejected over a
    rounding difference a cent-only rule would catch.
    """

    left_value = to_decimal(left)
    right_value = to_decimal(right)
    difference = abs(left_value - right_value)
    if difference <= absolute:
        return True
    scale = max(abs(left_value), abs(right_value))
    if scale == 0:
        return False
    return (difference / scale) <= relative
