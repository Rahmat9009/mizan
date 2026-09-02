"""Invariant 18 - API-SURFACE Addendum 1 section D (Verdict section 5): the semantic layer is downward-only and optional.

Pass criterion: (a) the RiskEvaluation is byte-identical whether or not an advisory opinion was produced - the
opinion is not an input to mizan.risk.evaluate and the RiskEvaluation type has no field that could carry one;
(b) for one RiskEvaluation, govern(advisory=None) never authorizes LESS than govern(advisory=X) for X in {CONCUR,
REDUCE below the cap, REDUCE above the cap, REJECT, unavailable}, and equals the deterministic cap; (c)
replay(record, advisory=None) reproduces the recorded evaluation_id exactly and reproduces the recorded governor
verdict (and verdict_hash) whenever the recorded advisory was CONCUR, unavailable, or absent, and never authorizes
less than the recorded decision when the recorded advisory had reduced it.
"""
from __future__ import annotations

from mizan import advisory, governor, replay, risk
from mizan.advisory import OfflineAdvisoryProvider
from mizan.audit import InMemoryLedger
from mizan.contracts import RiskEvaluation
from mizan.contracts.canonical import canonical_json

from tests.fixtures import FIXED_NOW, TENANT_A, make_policy, make_proposal
from tests.invariants._support import (
    ScriptedAdvisoryProvider,
    context_for,
    linked_evaluation,
    opinion,
    quantity_of,
)

SEMANTIC_WORDS = ("advisory", "opinion", "llm", "semantic", "reasoning")


def _advisory_variants(cap: str):
    below = str(max(int(cap) - 1, 0)) if cap.isdigit() else "1"
    above = str(int(cap) + 1) if cap.isdigit() else "1000000"
    return {
        "CONCUR": opinion("CONCUR"),
        "REDUCE-below-cap": opinion("REDUCE", below),
        "REDUCE-above-cap": opinion("REDUCE", above),
        "REJECT": opinion("REJECT"),
        "unavailable": opinion(None, available=False),
    }


def test_semantic_layer_disabled_produces_identical_verdict():
    policy = make_policy()
    assert policy.fail_closed.on_advisory_unavailable is False, "default policy must not fail closed on advisory"
    context = context_for(policy)
    proposal = make_proposal()

    # (a) the evaluation is not a function of the semantic layer
    assert not [f for f in RiskEvaluation.model_fields if any(w in f.lower() for w in SEMANTIC_WORDS)]
    before = risk.evaluate(proposal, context, policy)
    produced = advisory.get_advisory(OfflineAdvisoryProvider(), proposal, before, context, policy)
    assert produced.invoked is True
    rogue = advisory.get_advisory(
        ScriptedAdvisoryProvider(opinion("REJECT")), proposal, before, context, policy
    )
    assert rogue.invoked is True
    after = risk.evaluate(proposal, context, policy)
    assert canonical_json(before) == canonical_json(after)
    assert before.evaluation_id == after.evaluation_id

    # (b) disabling the semantic layer never makes the deterministic part looser or stricter
    for verdict, recommended in (("REDUCE", "4"), ("PASS", None)):
        evaluation = linked_evaluation(
            proposal, context, policy, verdict=verdict, recommended_quantity=recommended
        )
        cap = quantity_of(type("Q", (), {"total_quantity": evaluation.recommended_quantity})())
        without = governor.govern(proposal, evaluation, policy, None, context=context)
        assert quantity_of(without.authorized) == cap
        for label, variant in _advisory_variants(evaluation.recommended_quantity).items():
            with_variant = governor.govern(proposal, evaluation, policy, variant, context=context)
            assert quantity_of(with_variant.authorized) <= quantity_of(without.authorized), (
                label, with_variant.authorized.total_quantity, without.authorized.total_quantity
            )
            assert quantity_of(with_variant.authorized) <= cap

    # (c) replay without the semantic layer reproduces the deterministic record
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    for recorded in (opinion("CONCUR"), opinion(None, available=False), None):
        evaluation = risk.evaluate(proposal, context, policy)
        decision = governor.govern(proposal, evaluation, policy, recorded, context=context)
        record = tenant_ledger.append(
            proposal=proposal,
            risk_context=context,
            risk_evaluation=evaluation,
            governor_decision=decision,
            policy_snapshot=policy,
            recorded_at=FIXED_NOW,
        )
        result = replay.replay(record, advisory=None)
        assert result.decision_id == record.decision_id
        assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
        assert canonical_json(result.replayed_evaluation) == canonical_json(record.risk_evaluation)
        assert result.replayed_verdict == record.verdict
        assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
        assert result.replayed_decision.authorized.total_quantity == record.authorized.total_quantity


def test_replay_without_advisory_never_authorizes_less_than_the_recorded_decision():
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", "the default fixture proposal must be authorizable"
    reduced_by_advisory = governor.govern(
        proposal, evaluation, policy, opinion("REDUCE", "1"), context=context
    )
    record = InMemoryLedger().for_tenant(TENANT_A).append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=reduced_by_advisory,
        policy_snapshot=policy,
        recorded_at=FIXED_NOW,
    )
    result = replay.replay(record, advisory=None)
    assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert quantity_of(result.replayed_decision.authorized) >= quantity_of(record.authorized)
    assert result.replayed_decision.authorized.total_quantity == evaluation.recommended_quantity
