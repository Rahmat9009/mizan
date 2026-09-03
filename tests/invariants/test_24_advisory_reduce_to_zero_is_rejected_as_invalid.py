"""Invariant 24 - ESC-1 made permanent: a REDUCE to zero is not a REDUCE, it is a REJECT, and it is not constructible.

Pass criterion, at all four layers where the rule could be broken:
  (a) CONTRACT - AdvisoryOpinion refuses recommendation=REDUCE with a zero, negative or absent quantity.
      This is the authority; everything below is defence in depth.
  (b) ADVISORY BOUNDARY - a provider that returns such a thing (or anything shaped like it) yields an
      UNAVAILABLE opinion, never a zeroing one. A compromised provider must not be able to flatten an
      order by advising "reduce to nothing".
  (c) GOVERNOR - no advisory can drive the authorized quantity to zero. Only the deterministic engine
      REJECTs. This is E1: the semantic layer is downward-only, and "down" stops at the cap, not at zero.
  (d) TEST GUARD - the shared ``opinion`` builder refuses the combination and says why, so a generator
      bound bug is reported as one rather than as a mystery ValidationError.

Why this is its own invariant rather than a note in ESC-1: the escalation was closed once by narrowing
one hypothesis strategy's lower bound, which fixed the instance and left the class open. An invariant is
the only artefact that cannot be quietly un-fixed.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mizan import advisory, governor, risk
from mizan.contracts import AdvisoryOpinion
from mizan.contracts.types import dec

from tests.fixtures import make_policy, make_proposal
from tests.invariants._support import ScriptedAdvisoryProvider, codes, context_for, opinion, quantity_of

NON_POSITIVE = ["0", "0.0", "-1", "-0.0001", None]


def _opinion_fields(**overrides):
    base = dict(
        profile="invariant-test",
        invoked=True,
        available=True,
        recommendation="REDUCE",
        recommended_quantity="0",
        reasoning="",
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )
    base.update(overrides)
    return base


# --- (a) the contract is the authority -------------------------------------------------------------
@pytest.mark.parametrize("quantity", NON_POSITIVE)
def test_the_contract_refuses_reduce_to_a_non_positive_quantity(quantity):
    with pytest.raises(ValidationError):
        AdvisoryOpinion(**_opinion_fields(recommended_quantity=quantity))


def test_reject_is_the_way_to_express_it_and_is_accepted():
    """The rule is not "you cannot say no" - REJECT exists precisely for this, and needs no quantity."""
    rejected = AdvisoryOpinion(**_opinion_fields(recommendation="REJECT", recommended_quantity=None))
    assert rejected.recommendation == "REJECT"


# --- (b) the advisory boundary ---------------------------------------------------------------------
class _ZeroingProvider:
    """A provider that tries to return REDUCE-to-zero, bypassing contract construction entirely."""

    def __init__(self, quantity):
        self._quantity = quantity

    def advise(self, *args, **kwargs):
        return type(
            "RawOpinion",
            (),
            {**_opinion_fields(recommended_quantity=self._quantity), "__slots__": ()},
        )()


@pytest.mark.parametrize("quantity", NON_POSITIVE)
def test_a_provider_that_advises_reduce_to_zero_yields_an_unavailable_opinion(quantity):
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)

    produced = advisory.get_advisory(_ZeroingProvider(quantity), proposal, evaluation, context, policy)

    assert produced.available is False, (
        "a provider advising REDUCE-to-nothing must produce an UNAVAILABLE opinion, never a zeroing one; "
        f"got available={produced.available} recommendation={produced.recommendation!r} "
        f"quantity={produced.recommended_quantity!r}"
    )
    assert produced.recommended_quantity in (None, ""), "an unavailable opinion carries no quantity"


# --- (c) the governor ------------------------------------------------------------------------------
def test_no_advisory_can_drive_the_authorized_quantity_to_zero():
    """A REJECT advisory may block, but nothing may produce a zero-size APPROVE."""
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", "the baseline must be authorizable"

    for label, advised in (("REJECT", opinion("REJECT")), ("unavailable", opinion(None, available=False))):
        decision = governor.govern(proposal, evaluation, policy, advised, context=context)
        authorized = quantity_of(decision.authorized)
        if decision.verdict == "REJECT":
            assert authorized == 0, f"{label}: a REJECT authorizes nothing"
        else:
            assert authorized > 0, (
                f"{label}: a non-REJECT verdict must authorize a positive quantity, not zero; "
                f"got {authorized} with {sorted(codes(decision))}"
            )


def test_a_reduce_advisory_at_the_floor_still_authorizes_something_or_rejects_outright():
    """There is no third state: an order is reduced to a positive size, or it is refused. Never zero-approved."""
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, opinion("REDUCE", "1"), context=context)
    authorized = quantity_of(decision.authorized)
    assert (decision.verdict == "REJECT" and authorized == 0) or authorized > 0
    assert authorized <= dec(evaluation.recommended_quantity), "E1: never above the deterministic cap"


# --- (d) the ESC-1 test guard ----------------------------------------------------------------------
@pytest.mark.parametrize("quantity", NON_POSITIVE)
def test_the_shared_builder_refuses_the_combination_and_names_the_rule(quantity):
    with pytest.raises(AssertionError, match="not constructible"):
        opinion("REDUCE", quantity)


def test_the_guard_does_not_block_legitimate_opinions():
    """Control: the guard must catch only the invalid combination, or it would hide real coverage."""
    assert opinion("REDUCE", "1").recommended_quantity == "1"
    assert opinion("CONCUR").recommendation == "CONCUR"
    assert opinion("REJECT").recommendation == "REJECT"
    assert ScriptedAdvisoryProvider(opinion("REDUCE", "3")) is not None
