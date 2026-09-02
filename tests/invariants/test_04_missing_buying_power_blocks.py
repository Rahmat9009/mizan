"""Invariant 04 - Hard Rule E2: unknown risk != safe; missing buying power BLOCKS.

Pass criterion: mizan.risk.evaluate on a context whose portfolio snapshot has buying_power=None returns REJECT with
BUYING_POWER_MISSING and recommended_quantity "0", while the same proposal with complete data does not carry that
code; a *zero* buying power is not "missing" (it is a known value and never reported as BUYING_POWER_MISSING).
Addendum 1: with policy.aggregate enabled and aggregate_state=None the engine REJECTs with AGGREGATE_STATE_MISSING.
"""
from __future__ import annotations

from mizan import risk

from tests.fixtures import make_institutional_policy, make_policy, make_portfolio_snapshot, make_proposal
from tests.invariants._support import codes, context_for


def test_missing_buying_power_blocks():
    policy = make_policy()
    proposal = make_proposal()

    # control: complete data does not carry the code
    baseline = risk.evaluate(proposal, context_for(policy), policy)
    assert "BUYING_POWER_MISSING" not in codes(baseline), codes(baseline)

    snapshot = make_portfolio_snapshot(buying_power=None)
    assert snapshot.buying_power is None
    context = context_for(policy, portfolio_snapshot=snapshot)
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict == "REJECT"
    assert "BUYING_POWER_MISSING" in codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"
    assert evaluation.data_complete is False
    failed_blocking = [c for c in evaluation.checks if not c.passed and c.severity == "blocking"]
    assert failed_blocking, [(c.check_id, c.passed, c.severity) for c in evaluation.checks]


def test_zero_buying_power_is_a_value_not_missing_data():
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="0"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "BUYING_POWER_MISSING" not in codes(evaluation), codes(evaluation)
    assert evaluation.data_complete is True


def test_missing_aggregate_state_blocks_when_policy_enables_aggregate_checks():
    policy = make_institutional_policy()
    assert policy.aggregate is not None, "make_institutional_policy() must enable the aggregate section"
    context = context_for(policy, aggregate_state=None)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "AGGREGATE_STATE_MISSING" in codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"
