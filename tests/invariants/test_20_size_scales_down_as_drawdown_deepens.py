"""Invariant 20 - Dispatch Addendum section 2 / Risk Canon R-ERG-2: size scales DOWN as drawdown deepens, never up.

Pass criterion: (a) the recommended quantity is monotonically NON-INCREASING as current_drawdown_pct deepens,
with every other input held fixed; (b) it strictly decreases somewhere, so an engine that ignored path state
could not pass vacuously; (c) the contract refuses a policy whose ladder scales UP or is not ascending, so
"size up into a drawdown" is not expressible in configuration.

The same order is a different risk after four losses than it is at the peak. That is the whole reason the
engine takes path state as an input rather than being a pure per-proposal function of the proposal.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from mizan import risk
from mizan.contracts import Policy
from mizan.contracts.types import dec

from tests.fixtures import make_path_state, make_proposal
from tests.invariants._support import path_and_aggregate_policy, unstressed_context

#: Deepening drawdowns, shallow to deep. Every other input is held constant.
LADDER = ("0", "0.02", "0.05", "0.08", "0.10", "0.12", "0.15", "0.25", "0.40")


def _quantity_at(policy, drawdown: str) -> Decimal:
    context = unstressed_context(
        policy, path_state=make_path_state(current_drawdown_pct=drawdown, consecutive_losses=0, days_under_water=0)
    )
    return dec(risk.evaluate(make_proposal(), context, policy).recommended_quantity)


def test_size_scales_down_as_drawdown_deepens():
    policy = path_and_aggregate_policy()
    assert policy.path is not None and policy.path.size_scaling_by_drawdown, "needs a drawdown ladder"

    sizes = [(pct, _quantity_at(policy, pct)) for pct in LADDER]

    # Index walk rather than zip(): a pairwise walk has two sequences of different length, and
    # zip(strict=True) would raise on that while zip(strict=False) reads as an oversight.
    for i in range(len(sizes) - 1):
        (shallow_pct, shallow), (deep_pct, deeper) = sizes[i], sizes[i + 1]
        assert deeper <= shallow, (
            "size must never increase as drawdown deepens: at drawdown "
            f"{shallow_pct} the engine recommended {shallow}, but at the DEEPER drawdown {deep_pct} "
            f"it recommended {deeper}"
        )

    assert sizes[-1][1] < sizes[0][1], (
        f"the deepest drawdown must recommend strictly less than none at all; got {sizes[-1][1]} at "
        f"{sizes[-1][0]} versus {sizes[0][1]} at {sizes[0][0]}. An engine ignoring path state would "
        "satisfy monotonicity vacuously."
    )


def test_the_contract_refuses_a_ladder_that_scales_up():
    payload = path_and_aggregate_policy().model_dump(mode="json")
    payload.pop("policy_hash", None)
    payload["path"]["size_scaling_by_drawdown"] = [
        {"drawdown_pct": "0.05", "size_multiplier": "0.5"},
        {"drawdown_pct": "0.10", "size_multiplier": "0.9"},  # deeper drawdown, LARGER size
    ]
    with pytest.raises(ValidationError, match="non-increasing"):
        Policy.model_validate(payload)


def test_the_contract_refuses_a_ladder_that_is_not_ascending_in_drawdown():
    payload = path_and_aggregate_policy().model_dump(mode="json")
    payload.pop("policy_hash", None)
    payload["path"]["size_scaling_by_drawdown"] = [
        {"drawdown_pct": "0.10", "size_multiplier": "0.9"},
        {"drawdown_pct": "0.05", "size_multiplier": "0.5"},
    ]
    with pytest.raises(ValidationError, match="ascending"):
        Policy.model_validate(payload)
