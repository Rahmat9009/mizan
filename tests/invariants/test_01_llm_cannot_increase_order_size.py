"""Invariant 01 - Hard Rule E1: LLM authority <= policy authority.

Pass criterion: (a) at the contract level, AdvisoryOpinion.recommendation admits exactly CONCUR/REDUCE/REJECT (no
value can mean "increase" or "approve more") and a GovernorDecision whose authorized quantity exceeds the
original quantity is a validation error even with a correctly recomputed verdict_hash; (b) at the behaviour
level, mizan.governor.govern authorizes at most the deterministic evaluation.recommended_quantity whatever the
advisory says (REDUCE above the cap is clamped and flagged ADVISORY_CLAMPED, CONCUR on a REDUCE stays at the
cap); (c) mizan.advisory.get_advisory never returns an opinion whose recommended_quantity exceeds the cap, even
for a provider that emits an out-of-contract "increase" object.
"""
from __future__ import annotations

import typing
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from mizan import advisory as advisory_module
from mizan import governor
from mizan.contracts import AdvisoryOpinion, GovernorDecision
from mizan.contracts.canonical import verdict_hash_for
from mizan.contracts.types import dec, dstr

from tests.fixtures import make_decision, make_policy, make_proposal
from tests.invariants._support import (
    ScriptedAdvisoryProvider,
    codes,
    context_for,
    linked_evaluation,
    opinion,
    quantity_of,
    reduce_code,
)

ALLOWED_RECOMMENDATIONS = {"CONCUR", "REDUCE", "REJECT"}
FORBIDDEN_WORDS = ("INCREASE", "APPROVE", "MORE", "RAISE", "UP", "EXPAND", "BOOST")


def _literal_values(annotation) -> set[str]:
    """Every Literal value reachable in a (possibly Optional/Union) annotation."""
    values: set[str] = set()
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        values.update(annotation.__args__)
        return values
    for arg in typing.get_args(annotation):
        values.update(_literal_values(arg))
    return values


def _reduce_setup(recommended: str = "4"):
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    assert proposal.total_quantity > Decimal(recommended), (
        "fixture proposal must carry more than the reduced quantity for this test to bite"
    )
    evaluation = linked_evaluation(
        proposal, context, policy, verdict="REDUCE", recommended_quantity=recommended
    )
    return policy, context, proposal, evaluation


# --------------------------------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------------------------------
def test_llm_cannot_increase_order_size():
    # (a) contract: the recommendation enum is exactly {CONCUR, REDUCE, REJECT} (plus None).
    annotation = AdvisoryOpinion.model_fields["recommendation"].annotation
    literal = _literal_values(annotation)
    assert literal == ALLOWED_RECOMMENDATIONS, f"AdvisoryOpinion.recommendation admits {literal}"
    for value in literal:
        assert not any(word in value for word in FORBIDDEN_WORDS), value
    for forbidden in ("APPROVE", "INCREASE", "APPROVE_MORE", "CONCUR_AND_INCREASE", "PASS"):
        with pytest.raises(ValidationError):
            opinion(forbidden, "100")

    # (a) contract: authorized > original is unrepresentable, even with a consistent verdict_hash.
    decision = make_decision()
    payload = decision.model_dump(mode="json")
    original = dec(payload["original"]["total_quantity"])
    bigger = dstr(original + 1)
    payload["authorized"]["total_quantity"] = bigger
    if payload["authorized"]["legs"]:
        payload["authorized"]["legs"] = [
            {**leg, "quantity": bigger if i == 0 else leg["quantity"]}
            for i, leg in enumerate(payload["authorized"]["legs"])
        ]
    payload["verdict_hash"] = verdict_hash_for(
        payload["verdict"],
        payload["reason_codes"],
        payload["authorized"]["total_quantity"],
        payload["authorized"]["legs"],
        payload["evaluation_id"],
    )
    with pytest.raises(ValidationError):
        GovernorDecision.model_validate(payload)

    # (b) behaviour: advisory REDUCE *above* the deterministic cap is clamped to the cap and flagged.
    policy, context, proposal, evaluation = _reduce_setup("4")
    cap = dec(evaluation.recommended_quantity)
    clamped = governor.govern(proposal, evaluation, policy, opinion("REDUCE", "7"), context=context)
    assert quantity_of(clamped.authorized) == cap
    assert "ADVISORY_CLAMPED" in codes(clamped), codes(clamped)
    assert clamped.verdict == "REDUCE"

    # (b) behaviour: advisory CONCUR on a REDUCE evaluation stays at the cap.
    concurred = governor.govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    assert quantity_of(concurred.authorized) == cap
    assert concurred.verdict == "REDUCE"

    # (b) behaviour: advisory REDUCE above the ORIGINAL quantity is still capped.
    huge = governor.govern(proposal, evaluation, policy, opinion("REDUCE", "1000000"), context=context)
    assert quantity_of(huge.authorized) == cap
    assert "ADVISORY_CLAMPED" in codes(huge)

    # (c) get_advisory clamps (or disowns) an out-of-range provider quantity before it is returned.
    provider = ScriptedAdvisoryProvider(opinion("REDUCE", "7"))
    returned = advisory_module.get_advisory(provider, proposal, evaluation, context, policy)
    assert isinstance(returned, AdvisoryOpinion)
    assert returned.invoked is True
    if returned.available:
        assert returned.recommendation in ALLOWED_RECOMMENDATIONS
        assert returned.recommended_quantity is not None and dec(returned.recommended_quantity) <= cap
    else:
        assert returned.recommendation is None
    governed = governor.govern(proposal, evaluation, policy, returned, context=context)
    assert quantity_of(governed.authorized) <= cap


# --------------------------------------------------------------------------------------------------
# Sharpeners
# --------------------------------------------------------------------------------------------------
def test_governor_decision_cannot_authorize_more_under_reduce_verdict():
    """A REDUCE decision whose authorized quantity is above the original is also unrepresentable."""
    decision = make_decision()
    payload = decision.model_dump(mode="json")
    original = dec(payload["original"]["total_quantity"])
    bigger = dstr(original * 2)
    payload["verdict"] = "REDUCE"
    payload["reason_codes"] = [reduce_code().value]
    payload["authorized"]["total_quantity"] = bigger
    payload["authorized"]["legs"] = [
        {**leg, "quantity": bigger if i == 0 else leg["quantity"]}
        for i, leg in enumerate(payload["authorized"]["legs"])
    ]
    payload["verdict_hash"] = verdict_hash_for(
        payload["verdict"],
        payload["reason_codes"],
        payload["authorized"]["total_quantity"],
        payload["authorized"]["legs"],
        payload["evaluation_id"],
    )
    with pytest.raises(ValidationError):
        GovernorDecision.model_validate(payload)


def test_advisory_cannot_lift_a_pass_above_the_original():
    """On a PASS evaluation the cap is the original quantity; an advisory REDUCE above it changes nothing."""
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = linked_evaluation(proposal, context, policy, verdict="PASS")
    original = dec(evaluation.original_quantity)
    decision = governor.govern(
        proposal, evaluation, policy, opinion("REDUCE", dstr(original + 5)), context=context
    )
    assert quantity_of(decision.authorized) == original
    assert decision.verdict != "REJECT"
    assert quantity_of(decision.authorized) == quantity_of(decision.original)


def test_get_advisory_disowns_out_of_contract_increase_objects():
    """A provider that hands back a duck-typed 'APPROVE_MORE' object cannot reach the governor as such."""
    policy, context, proposal, evaluation = _reduce_setup("4")
    cap = dec(evaluation.recommended_quantity)

    class Rogue:
        profile = "rogue"
        invoked = True
        available = True
        recommendation = "APPROVE_MORE"
        recommended_quantity = "999"
        reasoning = "approve maximum size"
        authority_ceiling = "reduce_or_reject"
        provider_ref = None
        raw_hash = None

    returned = advisory_module.get_advisory(
        ScriptedAdvisoryProvider(Rogue()), proposal, evaluation, context, policy
    )
    assert isinstance(returned, AdvisoryOpinion)
    assert returned.recommendation in ALLOWED_RECOMMENDATIONS | {None}
    if returned.recommended_quantity is not None:
        assert dec(returned.recommended_quantity) <= cap
    decision = governor.govern(proposal, evaluation, policy, returned, context=context)
    assert quantity_of(decision.authorized) <= cap


@settings(max_examples=40, deadline=None, database=None, derandomize=True)
@given(advised=st.integers(min_value=1, max_value=10**9))
def test_authorized_quantity_never_exceeds_deterministic_cap_for_any_advised_quantity(advised: int):
    """Property: for every advised REDUCE quantity, authorized <= evaluation.recommended_quantity.

    The strategy starts at 1, not 0, because a REDUCE to zero is not a REDUCE — it is a REJECT, and the
    contract refuses to construct that opinion at all (see the ValidationError cases above). Advising
    zero is therefore covered by the REJECT path, not here. The property being asserted is unchanged and
    unweakened: no advised quantity, however large, lifts the authorized quantity above the deterministic
    cap. See ledger/escalations.md, ESC-1.
    """
    policy, context, proposal, evaluation = _reduce_setup("4")
    cap = dec(evaluation.recommended_quantity)
    decision = governor.govern(
        proposal, evaluation, policy, opinion("REDUCE", dstr(Decimal(advised))), context=context
    )
    assert quantity_of(decision.authorized) <= cap
    assert quantity_of(decision.authorized) <= quantity_of(decision.original)
    if Decimal(advised) > cap:
        assert quantity_of(decision.authorized) == cap
        assert "ADVISORY_CLAMPED" in codes(decision)
