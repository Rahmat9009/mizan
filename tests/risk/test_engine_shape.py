"""The shape of every evaluation: one result per check, in order, with a verdict that follows the caps.

These are the structural guarantees the governor, the ledger and the console all rely on. They hold for
every proposal and every policy, so they are asserted here once rather than in each check's test.
"""

from __future__ import annotations

import pytest

from mizan import risk
from mizan.contracts import ALWAYS_ON_CHECKS, CHECK_IDS, dec
from mizan.risk import DEFERRED_CHECKS, IMPLEMENTED_CHECKS
from tests.fixtures import (
    TENANT_B,
    killer_demo_approve_proposal,
    killer_demo_context,
    killer_demo_policy,
    killer_demo_reject_proposal,
    make_context,
    make_institutional_policy,
    make_option_proposal,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)


def test_every_check_id_is_recorded_exactly_once_in_order(context_for):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert [check.check_id for check in evaluation.checks] == list(CHECK_IDS)


def test_deferred_checks_record_information_not_protection(context_for, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert DEFERRED_CHECKS, "the deferred set must stay visible while checks are outstanding"
    for check_id in sorted(DEFERRED_CHECKS):
        check = check_of(evaluation, check_id)
        assert check.passed is True
        assert check.severity == "info"
        assert "not implemented" in check.detail


def test_disabled_checks_record_info_and_say_so(context_for, check_of):
    policy = make_policy(checks={"concentration_limit": {"enabled": False, "severity": "warning"}})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "concentration_limit")
    assert (check.passed, check.severity, check.detail) == (True, "info", "disabled by policy")


def test_implemented_and_deferred_partition_the_catalogue():
    assert IMPLEMENTED_CHECKS | DEFERRED_CHECKS == frozenset(CHECK_IDS)
    assert not IMPLEMENTED_CHECKS & DEFERRED_CHECKS
    assert frozenset(ALWAYS_ON_CHECKS) <= IMPLEMENTED_CHECKS


def test_a_complete_context_and_a_normal_order_passes(context_for, codes):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.recommended_quantity == evaluation.original_quantity == "10"
    assert evaluation.recommended_notional == evaluation.original_notional == "2285"
    assert codes(evaluation) == set()
    assert evaluation.data_complete is True


def test_evaluation_identifies_its_proposal_context_and_policy(context_for):
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.proposal_id == proposal.proposal_id
    assert evaluation.context_id == context.context_id
    assert evaluation.tenant_id == context.tenant_id
    assert evaluation.policy == policy.ref
    assert evaluation.evaluated_at == context.evaluated_at


def test_tenant_mismatch_rejects_before_any_check_runs(codes):
    policy = make_policy(tenant_id=TENANT_B)
    context = make_context(tenant_id="tenant-a", policy=policy.ref)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert codes(evaluation) == {"TENANT_MISMATCH"}
    assert evaluation.recommended_quantity == "0"
    assert all(check.severity == "info" for check in evaluation.checks)
    assert all("not evaluated" in check.detail for check in evaluation.checks)


def test_policy_hash_mismatch_rejects(codes):
    policy = make_policy()
    other = make_policy(policy_version="9.9.9")
    assert other.policy_hash != policy.policy_hash
    context = make_context(tenant_id=policy.tenant_id, policy=other.ref)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert codes(evaluation) == {"POLICY_HASH_MISMATCH"}


def test_a_reduction_floors_to_whole_units(context_for, codes):
    """max_notional 1000 at 228.5 affords 4.37 shares; the engine authorizes 4, never 4.37."""
    policy = make_policy(
        order={"max_notional": "1000", "max_quantity": "20", "max_legs": 4},
        checks={"capital_threshold": {"enabled": True, "severity": "warning"}},
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REDUCE"
    assert evaluation.recommended_quantity == "4"
    assert "CAPITAL_THRESHOLD_EXCEEDED" in codes(evaluation)
    assert evaluation.recommended_notional == "914"


def test_a_reduction_that_reaches_zero_is_a_rejection(context_for, codes):
    policy = make_policy(
        order={"max_notional": "10", "max_quantity": "20", "max_legs": 4},
        checks={"capital_threshold": {"enabled": True, "severity": "warning"}},
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"
    assert evaluation.recommended_notional == "0"
    assert "CAPITAL_THRESHOLD_EXCEEDED" in codes(evaluation)


def test_the_binding_cap_is_the_minimum_across_reducing_checks(context_for):
    policy = make_policy(
        order={"max_notional": "1000", "max_quantity": "6", "max_legs": 4},
        checks={
            "capital_threshold": {"enabled": True, "severity": "warning"},
            "position_limit": {"enabled": True, "severity": "warning"},
        },
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REDUCE"
    assert evaluation.recommended_quantity == "4"  # 4 from notional, 6 from quantity: the minimum binds


def test_a_blocking_failure_rejects_even_when_a_smaller_order_would_fit(context_for, check_of):
    policy = make_policy(order={"max_notional": "1000", "max_quantity": "20", "max_legs": 4})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"
    check = check_of(evaluation, "capital_threshold")
    assert (check.passed, check.severity, check.recommended_quantity) == (False, "blocking", "0")


def test_notionals_are_absent_when_the_order_cannot_be_valued(context_for):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy, market_snapshot=None), policy)
    assert evaluation.original_notional is None
    assert evaluation.recommended_notional is None
    assert evaluation.data_complete is False


def test_option_notional_carries_the_contract_multiplier(context_for):
    policy = killer_demo_policy()
    evaluation = risk.evaluate(killer_demo_approve_proposal(), killer_demo_context(policy=policy), policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.original_notional == "3700"  # 20 contracts x 1.85 x 100


def test_the_killer_demo_rejects_fifty_contracts_on_the_delta_limit(codes, check_of):
    policy = killer_demo_policy()
    evaluation = risk.evaluate(killer_demo_reject_proposal(), killer_demo_context(policy=policy), policy)
    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"
    assert "OPTIONS_DELTA_LIMIT_EXCEEDED" in codes(evaluation)
    delta = check_of(evaluation, "options_delta_limit")
    assert (delta.threshold, delta.actual) == ("500", "890")
    assert delta.severity == "blocking" and delta.passed is False


def test_the_killer_demo_approves_twenty_contracts(codes, check_of):
    policy = killer_demo_policy()
    evaluation = risk.evaluate(killer_demo_approve_proposal(), killer_demo_context(policy=policy), policy)
    assert evaluation.verdict == "PASS"
    assert codes(evaluation) == set()
    delta = check_of(evaluation, "options_delta_limit")
    assert (delta.threshold, delta.actual) == ("500", "386")


def test_data_complete_is_false_only_when_something_was_unknown(context_for):
    policy = make_policy()
    complete = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert complete.data_complete is True
    without_buying_power = risk.evaluate(
        make_proposal(),
        context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power=None)),
        policy,
    )
    assert without_buying_power.data_complete is False


def test_a_zero_value_is_never_treated_as_missing(context_for):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="0"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.data_complete is True
    assert evaluation.verdict == "REJECT"  # zero buying power is a known, binding value


@pytest.mark.parametrize(
    "proposal_builder", [make_proposal, make_option_proposal, killer_demo_reject_proposal]
)
def test_every_failed_blocking_check_code_reaches_the_evaluation(proposal_builder, codes, context_for):
    policy = make_institutional_policy()
    context = context_for(policy)
    evaluation = risk.evaluate(proposal_builder(), context, policy)
    for check in evaluation.checks:
        if not check.passed and check.severity == "blocking":
            assert str(check.reason_code.value) in codes(evaluation)


def test_reductions_never_exceed_the_original_quantity(context_for):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert dec(evaluation.recommended_quantity) <= dec(evaluation.original_quantity)
