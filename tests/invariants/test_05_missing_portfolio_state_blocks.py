"""Invariant 05 - Hard Rule E2: unknown risk != safe; missing portfolio state BLOCKS.

Pass criterion: mizan.risk.evaluate on a context with portfolio_snapshot=None returns REJECT with
PORTFOLIO_STATE_MISSING, recommended_quantity "0" and a failed *blocking* portfolio_state_presence check, while the
same proposal with complete data does not carry that code. The always-on presence check cannot be disabled and
FailClosed.on_missing_portfolio_state cannot be switched off by the contract. Addendum 1: with policy.path and
policy.aggregate enabled and path_state / aggregate_state = None, the engine REJECTs with PATH_STATE_MISSING and
AGGREGATE_STATE_MISSING.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mizan import risk
from mizan.contracts import FailClosed
from mizan.contracts.errors import MizanError

from tests.fixtures import make_institutional_policy, make_policy, make_proposal
from tests.invariants._support import codes, context_for


def test_missing_portfolio_state_blocks():
    policy = make_policy()
    proposal = make_proposal()

    # control: complete data does not carry the code (the test is not vacuous)
    baseline = risk.evaluate(proposal, context_for(policy), policy)
    assert "PORTFOLIO_STATE_MISSING" not in codes(baseline), codes(baseline)
    assert baseline.data_complete is True

    context = context_for(policy, portfolio_snapshot=None)
    assert context.portfolio_snapshot is None
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict == "REJECT"
    assert "PORTFOLIO_STATE_MISSING" in codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"
    assert evaluation.data_complete is False
    assert any(
        c.check_id == "portfolio_state_presence" and not c.passed and c.severity == "blocking"
        for c in evaluation.checks
    ), [(c.check_id, c.passed, c.severity) for c in evaluation.checks]


def test_portfolio_state_presence_check_cannot_be_disabled_or_fail_open():
    with pytest.raises((ValidationError, MizanError)):
        make_policy(checks={"portfolio_state_presence": {"enabled": False}})
    with pytest.raises(ValidationError):
        FailClosed(on_missing_portfolio_state=False)
    with pytest.raises(ValidationError):
        FailClosed(on_engine_degraded=False)


def test_missing_path_and_aggregate_state_block_when_policy_enables_them():
    policy = make_institutional_policy()
    assert policy.path is not None and policy.aggregate is not None, (
        "make_institutional_policy() must enable the path and aggregate sections"
    )
    context = context_for(policy, path_state=None, aggregate_state=None)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert {"PATH_STATE_MISSING", "AGGREGATE_STATE_MISSING"} <= codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"
    assert evaluation.data_complete is False
