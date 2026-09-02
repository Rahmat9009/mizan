"""Invariant 03 - Hard Rule E2: unknown risk != safe; a missing price BLOCKS.

Pass criterion: mizan.risk.evaluate on a context whose market snapshot has no quote for the proposal symbol, and
separately on a context with market_snapshot=None, returns REJECT with MARKET_DATA_MISSING or PRICE_MISSING,
recommended_quantity "0" and a failed *blocking* market_data_presence check - while the same proposal with complete
data does not carry those codes. The contract cannot express a zero price, cannot disable the always-on presence
check and cannot switch fail-closed off. Addendum 1: with policy.path enabled and path_state=None the engine
REJECTs with PATH_STATE_MISSING.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mizan import risk
from mizan.contracts import FailClosed, Quote
from mizan.contracts.errors import MizanError

from tests.fixtures import (
    FIXED_NOW_STR,
    make_institutional_policy,
    make_market_snapshot,
    make_option_proposal,
    make_policy,
    make_proposal,
)
from tests.invariants._support import codes, context_for

MISSING_MARKET = {"MARKET_DATA_MISSING", "PRICE_MISSING"}


def _assert_blocked_for_missing_market_data(evaluation, *, expected_codes=MISSING_MARKET):
    assert evaluation.verdict == "REJECT"
    assert codes(evaluation) & expected_codes, codes(evaluation)
    assert evaluation.recommended_quantity == "0"
    assert evaluation.data_complete is False
    failed = [c for c in evaluation.checks if not c.passed]
    assert any(
        c.check_id == "market_data_presence" and c.severity == "blocking" for c in failed
    ), [(c.check_id, c.passed, c.severity) for c in evaluation.checks]


def test_missing_price_blocks():
    policy = make_policy()
    proposal = make_proposal()

    # control: complete data does not carry the missing-data codes (the test is not vacuous)
    baseline = risk.evaluate(proposal, context_for(policy), policy)
    assert not (codes(baseline) & MISSING_MARKET), codes(baseline)
    assert baseline.data_complete is True

    # no quote for the proposal's symbol
    no_quote = context_for(policy, market_snapshot=make_market_snapshot(quotes={}))
    assert proposal.symbol not in no_quote.market_snapshot.quotes
    _assert_blocked_for_missing_market_data(risk.evaluate(proposal, no_quote, policy))

    # no market snapshot at all
    no_snapshot = context_for(policy, market_snapshot=None)
    evaluation = risk.evaluate(proposal, no_snapshot, policy)
    _assert_blocked_for_missing_market_data(evaluation)
    assert "MARKET_DATA_MISSING" in codes(evaluation)


def test_unquoted_symbol_blocks_even_when_other_symbols_are_quoted():
    policy = make_policy()
    context = context_for(policy)
    unquoted = "ZQZQ"
    assert unquoted not in context.market_snapshot.quotes
    proposal = make_proposal(symbol=unquoted)
    _assert_blocked_for_missing_market_data(risk.evaluate(proposal, context, policy))


def test_option_leg_without_option_quote_blocks():
    policy = make_policy()
    proposal = make_option_proposal()
    context = context_for(policy, market_snapshot=make_market_snapshot(option_quotes={}))
    _assert_blocked_for_missing_market_data(risk.evaluate(proposal, context, policy))


def test_contract_cannot_express_a_zero_price():
    with pytest.raises(ValidationError):
        Quote(symbol="SPY", price="0", bid=None, ask=None, as_of=FIXED_NOW_STR, source="test")
    with pytest.raises(ValidationError):
        Quote(symbol="SPY", price="-1", bid=None, ask=None, as_of=FIXED_NOW_STR, source="test")


def test_market_data_presence_check_cannot_be_disabled_or_fail_open():
    with pytest.raises((ValidationError, MizanError)):
        make_policy(checks={"market_data_presence": {"enabled": False}})
    with pytest.raises(ValidationError):
        FailClosed(on_missing_market_data=False)


def test_missing_path_state_blocks_when_policy_enables_path_checks():
    policy = make_institutional_policy()
    assert policy.path is not None, "make_institutional_policy() must enable the path section"
    context = context_for(policy, path_state=None)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "PATH_STATE_MISSING" in codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"
