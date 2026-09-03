"""REQ-35: the account capability gate. Is this ACCOUNT permitted to place this order at all?

Every other check asks whether the ORDER is sound. This one asks a different question with a different
failure mode, and before REQ-35 the engine could not ask it: there was nowhere to put the answer and no
reason code to report it. Alpaca rejects a blocked account or an under-privileged options order at
submission, which means the decision record would say APPROVE for an order that was never placeable -
the exact gap between "we decided" and "it happened" this product exists to close.

Fails closed throughout (E2): a field the broker did not report is ACCOUNT_STATE_MISSING, never an
assumption of permission.
"""
from __future__ import annotations

import pytest

from mizan import risk
from tests.fixtures import make_account_state, make_option_proposal, make_proposal
from tests.invariants._support import codes, path_and_aggregate_policy, unstressed_context


def _check(context, policy=None, proposal=None):
    policy = policy or path_and_aggregate_policy()
    evaluation = risk.evaluate(proposal or make_proposal(), context, policy)
    return evaluation, next(c for c in evaluation.checks if c.check_id == "account_capability")


def _context(*, portfolio_snapshot=None, **account_overrides):
    policy = path_and_aggregate_policy()
    overrides = {"account_state": make_account_state(**account_overrides)}
    if portfolio_snapshot is not None:
        overrides["portfolio_snapshot"] = portfolio_snapshot
    # unstressed_context, not bare context_for: path_and_aggregate_policy() has both sections
    # enabled, so a bare context is missing state those OTHER checks need and blocks for reasons
    # that have nothing to do with account capability - masking exactly what these tests assert.
    return unstressed_context(policy, **overrides), policy


def _held_long(symbol, quantity):
    from tests.fixtures import make_portfolio_snapshot

    return make_portfolio_snapshot(
        positions=[
            {
                "symbol": symbol,
                "asset_class": "equity",
                "quantity": str(quantity),
                "market_value": str(quantity * 229),
                "sector": "Technology",
                "occ_symbol": None,
                "delta": None,
                "gamma": None,
                "vega": None,
            }
        ]
    )


# --- the happy path, and it carries evidence (INV-26) ----------------------------------------------
def test_a_healthy_account_passes_and_says_what_it_checked():
    context, policy = _context()
    _, check = _check(context, policy)
    assert check.passed is True
    assert check.snapshot_ts, "a blocking pass must carry evidence of what it looked at"
    assert "ACTIVE" in check.detail


# --- the blocking flags ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field", ["trading_blocked", "account_blocked", "trade_suspended_by_user"]
)
def test_every_blocking_flag_blocks(field):
    context, policy = _context(**{field: True})
    evaluation, check = _check(context, policy)
    assert check.passed is False
    assert str(check.reason_code) == "ACCOUNT_TRADING_BLOCKED"
    assert evaluation.verdict == "REJECT"
    assert "ACCOUNT_TRADING_BLOCKED" in codes(evaluation)


@pytest.mark.parametrize(
    "field", ["trading_blocked", "account_blocked", "trade_suspended_by_user", "shorting_enabled"]
)
def test_an_unreported_flag_blocks_rather_than_being_read_as_permission(field):
    """bool(None) is False, and False here means 'not blocked'. Absence must never become a grant."""
    context, policy = _context(**{field: None})
    proposal = make_proposal() if field != "shorting_enabled" else _short_proposal()
    _, check = _check(context, policy, proposal)
    assert check.passed is False
    assert str(check.reason_code) == "ACCOUNT_STATE_MISSING"


def test_a_missing_account_state_entirely_blocks():
    policy = path_and_aggregate_policy()
    _, check = _check(unstressed_context(policy, account_state=None), policy)
    assert check.passed is False
    assert str(check.reason_code) == "ACCOUNT_STATE_MISSING"


# --- status ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["ONBOARDING", "SUBMISSION_FAILED", "ACCOUNT_CLOSED", "INACTIVE"])
def test_any_status_other_than_active_blocks(status):
    context, policy = _context(status=status)
    _, check = _check(context, policy)
    assert check.passed is False
    assert str(check.reason_code) == "ACCOUNT_NOT_ACTIVE"


@pytest.mark.parametrize("status", ["ACTIVE", "active", " Active "])
def test_active_is_matched_case_and_whitespace_insensitively(status):
    """A broker that changes its casing must not halt trading; a broker that changes its MEANING must."""
    context, policy = _context(status=status)
    _, check = _check(context, policy)
    assert check.passed is True


# --- options level ---------------------------------------------------------------------------------
def test_an_options_level_below_the_requirement_blocks_an_options_order():
    context, policy = _context(options_trading_level=1)
    _, check = _check(context, policy, make_option_proposal())
    assert check.passed is False
    assert str(check.reason_code) == "OPTIONS_LEVEL_INSUFFICIENT"
    assert check.threshold == "2" and check.actual == "1"


def test_the_options_level_does_not_gate_an_equity_order():
    """The requirement is about options; applying it to equities would block orders it has no view on."""
    context, policy = _context(options_trading_level=0)
    _, check = _check(context, policy, make_proposal())
    assert check.passed is True


def test_a_sufficient_options_level_passes_and_records_the_comparison():
    context, policy = _context(options_trading_level=3)
    _, check = _check(context, policy, make_option_proposal())
    assert check.passed is True
    assert "options level 3" in check.detail


# --- shorting --------------------------------------------------------------------------------------
def _sell_proposal(quantity: int):
    payload = make_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    leg = payload["legs"][0]
    payload["strategy"] = "short_equity"
    payload["intent"] = "open"
    payload["legs"] = [{**leg, "side": "sell", "quantity": str(quantity)}]
    from mizan.contracts import TradeProposal

    return TradeProposal.build(**payload)


def _short_proposal():
    payload = make_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    payload["strategy"] = "short_equity"
    payload["intent"] = "open"
    payload["legs"] = [{**payload["legs"][0], "side": "sell"}]
    from mizan.contracts import TradeProposal

    return TradeProposal.build(**payload)


def test_an_opening_short_on_an_account_that_may_not_short_blocks():
    context, policy = _context(shorting_enabled=False)
    _, check = _check(context, policy, _short_proposal())
    assert check.passed is False
    assert str(check.reason_code) == "SHORTING_NOT_PERMITTED"


def test_a_sell_fully_covered_by_a_held_long_is_not_a_short_and_is_not_blocked():
    """Selling no more than what is held closes a position; it needs no shorting permission at all -
    this is now derived from the PORTFOLIO, not from the caller's `intent` label. Blocking it would
    strand a position on an account whose shorting permission was withdrawn, which is the exact harm
    this check exists to prevent."""
    context, policy = _context(shorting_enabled=False, portfolio_snapshot=_held_long("AAPL", 10))
    _, check = _check(context, policy, _short_proposal())  # sells 10, exactly what is held
    assert check.passed is True, "a fully-covered close must remain possible"


def test_intent_close_with_nothing_held_is_not_exempted():
    """`intent` is a label the caller chose; it is no longer trusted on its own. A "close" with no
    matching position held is indistinguishable from a short and must be treated as one."""
    payload = _short_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    payload["intent"] = "close"
    from mizan.contracts import TradeProposal

    context, policy = _context(shorting_enabled=False)  # no held position at all
    _, check = _check(context, policy, TradeProposal.build(**payload))
    assert check.passed is False
    assert str(check.reason_code) == "SHORTING_NOT_PERMITTED"


def test_selling_more_than_held_reduces_to_the_closing_quantity_not_a_silent_resize():
    """The brief's exact scenario: long 10, sell 15, shorting withdrawn. 10 closes the position and
    needs no permission; the excess 5 is a short the account may not open. The result must be a REDUCE
    to 10, carrying SHORTING_NOT_PERMITTED - never a silent cut, and never a REJECT of the whole order
    when part of it is perfectly fine."""
    context, policy = _context(shorting_enabled=False, portfolio_snapshot=_held_long("AAPL", 10))
    proposal = _sell_proposal(15)
    evaluation = risk.evaluate(proposal, context, policy)
    check = next(c for c in evaluation.checks if c.check_id == "account_capability")

    assert check.passed is False
    assert str(check.reason_code) == "SHORTING_NOT_PERMITTED"
    assert check.recommended_quantity == "10", "the cap must be exactly the closing quantity"
    assert check.actual == "5", "the excess (the actual short) is what gets reported, not the whole 15"

    assert evaluation.verdict == "REDUCE", "part of the order is fine; REJECTing all of it is wrong"
    assert evaluation.recommended_quantity == "10"
    assert "SHORTING_NOT_PERMITTED" in codes(evaluation)


def test_selling_more_than_held_with_nothing_at_all_held_rejects_outright():
    """The degenerate case of the same scenario: long 0, sell 15. The closing quantity is 0, a REDUCE
    to zero is semantically a REJECT (ESC-1's rule, applied here at the deterministic-check level), and
    the aggregate verdict must actually be REJECT, not a REDUCE that authorizes nothing while claiming to."""
    context, policy = _context(shorting_enabled=False)  # nothing held
    evaluation = risk.evaluate(_sell_proposal(15), context, policy)
    check = next(c for c in evaluation.checks if c.check_id == "account_capability")
    assert check.passed is False
    assert check.recommended_quantity == "0"
    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"


def test_a_short_that_stays_within_a_larger_long_never_reaches_the_permission_question():
    """Selling LESS than what is held asks nothing of the shorting permission - the state is not even
    consulted, so a broker that never reported shorting_enabled must not block a partial close."""
    context, policy = _context(shorting_enabled=None, portfolio_snapshot=_held_long("AAPL", 10))
    _, check = _check(context, policy, _sell_proposal(4))
    assert check.passed is True


def test_a_long_order_is_unaffected_by_shorting_permission():
    context, policy = _context(shorting_enabled=False)
    _, check = _check(context, policy, make_proposal())
    assert check.passed is True


# --- the check is off when the policy has no account section ---------------------------------------
def test_without_an_account_section_the_check_is_not_evaluated():
    """A tenant that has not asked for this gate does not get a blocking check it never configured."""
    from tests.fixtures import make_policy

    policy = make_policy()
    assert policy.account is None
    assert not policy.is_check_enabled("account_capability")
    evaluation = risk.evaluate(make_proposal(), unstressed_context(policy), policy)
    result = next(c for c in evaluation.checks if c.check_id == "account_capability")
    assert result.severity != "blocking" or result.passed
