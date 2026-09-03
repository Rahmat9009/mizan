"""Shared bar series and decision batteries for the vol-signal lane.

The bar series here is synthetic but *shaped like the venue's*: JSON numbers under ``o/h/l/c/v`` with
an RFC 3339 ``t``, exactly as the historical daily-bars endpoint returns them. Nothing in this file
touches a socket, so the whole lane's test suite runs offline and deterministically.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from mizan.contracts import TradeProposal
from tests.fixtures import (
    killer_demo_context,
    killer_demo_policy,
    killer_demo_reject_proposal,
    make_context,
    make_option_proposal,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

#: A fixed, arbitrary walk. Integers only, so the series is exact and identical on every machine.
_STEPS_BP = (
    31, -18, 7, 42, -55, 12, 3, -27, 61, -9, 14, -33, 22, 48, -12, 5, -41, 19, 26, -7,
    -63, 38, 11, -24, 52, -16, 8, 29, -45, 13, 21, -3, 36, -58, 17, 6, -22, 44, -11, 25,
    -37, 9, 53, -19, 2, 30, -47, 15, 23, -5, 41, -28, 10, 34, -60, 18, 4, -21, 49, -14,
    27, -8, 39, -51, 16, 1, -30, 46, -13, 24, -35, 12, 57, -20, 6, 32, -43, 20, 28, -2,
)


def bar_rows(count: int, *, start_price: str = "400.00", first_day: date = date(2024, 1, 2)) -> list[dict]:
    """``count`` venue-shaped daily bar objects, oldest first. Prices are JSON numbers, as the venue sends."""
    rows: list[dict] = []
    price = Decimal(start_price)
    day = first_day
    for index in range(count):
        step = Decimal(_STEPS_BP[index % len(_STEPS_BP)]) / Decimal(10000)
        price = (price * (Decimal(1) + step)).quantize(Decimal("0.0001"))
        high = (price * Decimal("1.0075")).quantize(Decimal("0.0001"))
        low = (price * Decimal("0.9925")).quantize(Decimal("0.0001"))
        rows.append(
            {
                "t": f"{day.isoformat()}T05:00:00Z",
                "o": Decimal(str(price)),
                "h": Decimal(str(high)),
                "l": Decimal(str(low)),
                "c": Decimal(str(price)),
                "v": 1_000_000 + index,
                "n": 1000,
                "vw": Decimal(str(price)),
            }
        )
        day += timedelta(days=1)
    return rows


@pytest.fixture
def rows() -> list[dict]:
    return bar_rows(400)


@pytest.fixture
def bars(rows):
    from mizan.signal import parse_bars

    return parse_bars(rows)


@pytest.fixture
def signal(bars):
    from mizan.signal import compute_vol_signal

    return compute_vol_signal(bars, symbol="SPY")


def _thin_portfolio_context(policy):
    """A context whose buying power cannot support the default proposal - so the engine cuts or refuses."""
    return make_context(
        policy=policy.ref,
        tenant_id=policy.tenant_id,
        portfolio_snapshot=make_portfolio_snapshot(equity="12000", cash="500", buying_power="900"),
    )


def decision_battery() -> list[tuple[TradeProposal, object, object]]:
    """``(proposal, context, policy)`` triples chosen to produce a MIX of verdicts, not one repeated.

    A shadow proof over a single APPROVE would prove almost nothing: the interesting question is whether
    the signal can move a verdict that was *close to* moving. So the battery includes a hard rejection,
    a size-constrained case and an option structure alongside the clean approvals.
    """
    default_policy = make_policy()
    demo_policy = killer_demo_policy()
    triples: list[tuple[TradeProposal, object, object]] = [
        (make_proposal(), make_context(policy=default_policy.ref), default_policy),
        (make_option_proposal(), make_context(policy=default_policy.ref), default_policy),
        (make_proposal(), _thin_portfolio_context(default_policy), default_policy),
        (make_option_proposal(), _thin_portfolio_context(default_policy), default_policy),
        (make_proposal(), make_context(policy=default_policy.ref, market_snapshot=None), default_policy),
        (killer_demo_reject_proposal(), killer_demo_context(), demo_policy),
    ]
    return triples
