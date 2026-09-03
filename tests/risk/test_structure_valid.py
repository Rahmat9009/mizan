"""F-31 / Risk Canon R-OPT-3: the legs must FORM the strategy they declare, and every short must be covered.

`STRATEGY_LEG_COUNTS` constrains the NUMBER of legs and nothing else. Before this check, a
`bull_call_spread` of two SHORT calls was APPROVEd for 10 contracts, an `iron_condor` of four short
calls for 20, and a naked short dressed as `custom` for 5 - against the strictest policy this build
ships. The portfolio greek caps bounded the exposure at size but are not structure rules: they bind at
50 contracts, not at 5.

This is the check that makes "defined risk" mean something. Max loss is computable at construction
precisely because a long leg caps each short one; without that, "max loss" is a phrase, not a number.
"""
from __future__ import annotations

import pytest

from mizan import risk
from mizan.contracts import TradeProposal
from tests.fixtures import make_institutional_policy, make_option_proposal, make_proposal
from tests.invariants._support import path_and_aggregate_policy, unstressed_context


def _option(strategy: str, legs: list[dict]) -> TradeProposal:
    payload = make_option_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    base = payload["legs"][0]
    payload["strategy"] = strategy
    payload["legs"] = [{**base, "leg_index": i, **leg} for i, leg in enumerate(legs)]
    return TradeProposal.build(**payload)


def _structure(proposal: TradeProposal, policy=None):
    policy = policy or path_and_aggregate_policy()
    evaluation = risk.evaluate(proposal, unstressed_context(policy), policy)
    return evaluation, next(c for c in evaluation.checks if c.check_id == "structure_valid")


def _call(side: str, strike: str, **extra) -> dict:
    return {"side": side, "contract_type": "call", "strike": strike, **extra}


def _put(side: str, strike: str, **extra) -> dict:
    return {"side": side, "contract_type": "put", "strike": strike, **extra}


# --- F-31's three demonstrated attacks, each previously APPROVED -----------------------------------
def test_f31a_a_naked_short_call_dressed_as_custom_is_rejected():
    _, check = _structure(_option("custom", [_call("sell", "230")]))
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"


def test_f31b_a_bull_call_spread_made_of_two_shorts_is_rejected():
    """The legs are the right COUNT and the right TYPE. They are simply both short, which makes this
    an unhedged short position wearing a spread's name."""
    _, check = _structure(_option("bull_call_spread", [_call("sell", "230"), _call("sell", "235")]))
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"


def test_f31c_an_iron_condor_of_four_short_calls_is_rejected():
    _, check = _structure(
        _option("iron_condor", [_call("sell", s) for s in ("225", "230", "235", "240")])
    )
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"


def test_not_even_the_institutional_policy_used_to_stop_these():
    """The finding's sharpest point: this was a MISSING CONTROL, not a loose policy. Re-asserted
    against the strictest shipped policy so a future loosening of it cannot hide the regression."""
    policy = make_institutional_policy()
    for strategy, legs in (
        ("bull_call_spread", [_call("sell", "230"), _call("sell", "235")]),
        ("bear_call_spread", [_call("sell", "230"), _call("sell", "235")]),
        ("iron_condor", [_call("sell", s) for s in ("225", "230", "235", "240")]),
    ):
        evaluation, check = _structure(_option(strategy, legs), policy)
        assert check.passed is False, f"{strategy} of all-shorts must be refused"
        assert evaluation.verdict == "REJECT"


# --- coverage is the rule that makes max loss a number ---------------------------------------------
def test_a_short_covered_by_an_equal_long_of_the_same_type_and_expiry_passes():
    _, check = _structure(_option("bull_call_spread", [_call("buy", "230"), _call("sell", "235")]))
    assert check.passed is True
    assert check.detail, "a blocking pass must say what it checked (INV-26)"


def test_a_short_covered_only_partially_is_rejected():
    """Half a hedge is not a hedge: the uncovered contracts have unbounded loss."""
    _, check = _structure(
        _option("custom", [_call("buy", "230", quantity="1"), _call("sell", "235", quantity="3")])
    )
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"
    assert check.threshold == "1" and check.actual == "3"


def test_a_long_of_the_WRONG_TYPE_does_not_cover_a_short():
    """A long put does not cap a short call. Coverage is per contract_type, not a headcount of legs."""
    _, check = _structure(_option("custom", [_put("buy", "230"), _call("sell", "235")]))
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"


def test_a_long_at_a_DIFFERENT_EXPIRY_does_not_cover_a_short():
    """A calendar is not a vertical: once the near leg expires the far short is naked, so coverage is
    established per expiry and a diagonal cannot masquerade as defined risk."""
    _, check = _structure(
        _option(
            "custom",
            [_call("buy", "230", expiry="2026-12-18"), _call("sell", "235", expiry="2026-09-25")],
        )
    )
    assert check.passed is False
    assert str(check.reason_code) == "NAKED_SHORT_NOT_PERMITTED"


# --- a named vertical must have the SHAPE its name claims ------------------------------------------
@pytest.mark.parametrize(
    ("strategy", "lower", "upper"),
    [
        ("bull_call_spread", "buy", "sell"),
        ("bear_call_spread", "sell", "buy"),
        ("bull_put_spread", "buy", "sell"),
        ("bear_put_spread", "sell", "buy"),
    ],
)
def test_each_named_vertical_accepts_its_own_shape(strategy, lower, upper):
    """A bull put spread is a CREDIT spread - short the higher strike, long the lower. Getting this
    backwards would refuse every legitimate put credit spread and accept its opposite."""
    leg = _call if "call" in strategy else _put
    _, check = _structure(_option(strategy, [leg(lower, "230"), leg(upper, "235")]))
    assert check.passed is True, f"{strategy} must accept {lower} lower / {upper} upper"


@pytest.mark.parametrize(
    ("strategy", "lower", "upper"),
    [
        ("bull_call_spread", "sell", "buy"),
        ("bear_call_spread", "buy", "sell"),
        ("bull_put_spread", "sell", "buy"),
        ("bear_put_spread", "buy", "sell"),
    ],
)
def test_each_named_vertical_rejects_its_inverse(strategy, lower, upper):
    """Inverting the sides is still covered - so it passes rule 1 - but it is the OPPOSITE position.
    A label that lies about direction is worse than an honest `custom`."""
    leg = _call if "call" in strategy else _put
    _, check = _structure(_option(strategy, [leg(lower, "230"), leg(upper, "235")]))
    assert check.passed is False
    assert str(check.reason_code) == "STRUCTURE_INVALID"


def test_a_vertical_of_mismatched_sizes_is_rejected():
    """A 1x3 is a ratio spread, not a vertical: the extra shorts are uncovered."""
    _, check = _structure(
        _option("bull_call_spread", [_call("buy", "230", quantity="1"), _call("sell", "235", quantity="3")])
    )
    assert check.passed is False


def test_a_vertical_spanning_two_expiries_is_rejected_as_a_diagonal():
    _, check = _structure(
        _option(
            "bull_call_spread",
            [_call("buy", "230", expiry="2026-09-25"), _call("sell", "235", expiry="2026-12-18")],
        )
    )
    assert check.passed is False
    assert str(check.reason_code) in {"STRUCTURE_INVALID", "NAKED_SHORT_NOT_PERMITTED"}


def test_a_vertical_with_one_strike_is_not_a_spread():
    _, check = _structure(_option("bull_call_spread", [_call("buy", "230"), _call("sell", "230")]))
    assert check.passed is False
    assert str(check.reason_code) == "STRUCTURE_INVALID"


# --- the check has no opinion on equities ----------------------------------------------------------
def test_an_equity_proposal_passes_without_comment():
    _, check = _structure(make_proposal())
    assert check.passed is True
    assert "not an options proposal" in check.detail


def test_long_only_option_structures_are_always_defined_risk():
    """A long option's max loss is its premium. Nothing to cover, nothing to refuse."""
    _, check = _structure(_option("long_call", [_call("buy", "230")]))
    assert check.passed is True
