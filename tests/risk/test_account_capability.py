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
from tests.invariants._support import codes, context_for, path_and_aggregate_policy


def _check(context, policy=None, proposal=None):
    policy = policy or path_and_aggregate_policy()
    evaluation = risk.evaluate(proposal or make_proposal(), context, policy)
    return evaluation, next(c for c in evaluation.checks if c.check_id == "account_capability")


def _context(**account_overrides):
    policy = path_and_aggregate_policy()
    return context_for(policy, account_state=make_account_state(**account_overrides)), policy


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
    _, check = _check(context_for(policy, account_state=None), policy)
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


def test_a_closing_sell_is_not_a_short_and_is_not_blocked():
    """Blocking a CLOSE would strand a position on an account whose shorting permission was withdrawn -
    a control causing the exact harm it exists to prevent."""
    payload = _short_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    payload["intent"] = "close"
    from mizan.contracts import TradeProposal

    context, policy = _context(shorting_enabled=False)
    _, check = _check(context, policy, TradeProposal.build(**payload))
    assert check.passed is True, "a closing sell must remain possible"


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
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    result = next(c for c in evaluation.checks if c.check_id == "account_capability")
    assert result.severity != "blocking" or result.passed
