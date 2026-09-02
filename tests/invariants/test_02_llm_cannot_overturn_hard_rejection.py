"""Invariant 02 - Hard Rule E1: the LLM cannot overturn a deterministic hard rejection.

Pass criterion: when the deterministic RiskEvaluation verdict is REJECT, mizan.governor.govern returns REJECT with
authorized total quantity "0" and no authorized legs and carries HARD_REJECTION_UPHELD - for advisory CONCUR, for
advisory None, and for an advisory that tries to "reduce" the rejected order back to a positive size. A REJECT
decision can never be turned into an ExecutionAuthorization (mizan.authorization.issue raises AuthorizationError).
"""
from __future__ import annotations

import pytest

from mizan import authorization, governor
from mizan.contracts.errors import AuthorizationError
from mizan.contracts.types import dec

from tests.fixtures import FIXED_NOW, make_policy, make_proposal
from tests.invariants._support import codes, context_for, linked_evaluation, opinion, quantity_of


def _rejected_setup():
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = linked_evaluation(proposal, context, policy, verdict="REJECT")
    assert evaluation.recommended_quantity == "0"
    return policy, context, proposal, evaluation


def _assert_hard_reject(decision, evaluation):
    assert decision.verdict == "REJECT"
    assert decision.authorized.total_quantity == "0"
    assert decision.authorized.legs == []
    assert quantity_of(decision.authorized) == dec("0")
    assert "HARD_REJECTION_UPHELD" in codes(decision), codes(decision)
    # every deterministic reason survives into the decision (A4)
    assert codes(evaluation) <= codes(decision), (codes(evaluation), codes(decision))


def test_llm_cannot_overturn_hard_rejection():
    policy, context, proposal, evaluation = _rejected_setup()

    concur = governor.govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    _assert_hard_reject(concur, evaluation)

    none = governor.govern(proposal, evaluation, policy, None, context=context)
    _assert_hard_reject(none, evaluation)

    # an advisory that "reduces" a rejected order to a positive quantity is still a rejection
    lifted = governor.govern(
        proposal, evaluation, policy, opinion("REDUCE", "1"), context=context
    )
    _assert_hard_reject(lifted, evaluation)


def test_hard_rejection_survives_an_unavailable_advisory():
    policy, context, proposal, evaluation = _rejected_setup()
    unavailable = opinion(None, available=False)
    decision = governor.govern(proposal, evaluation, policy, unavailable, context=context)
    _assert_hard_reject(decision, evaluation)


def test_rejected_decision_cannot_be_authorized():
    policy, context, proposal, evaluation = _rejected_setup()
    decision = governor.govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    with pytest.raises(AuthorizationError):
        authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
