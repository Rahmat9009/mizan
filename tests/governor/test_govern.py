"""Unit tests for ``mizan.governor.govern`` — the arbitration where Hard Rule E1 becomes code.

The invariant suite pins the headline property (the advisory can never increase a quantity or overturn a
rejection). These tests pin the rest of the behaviour that makes that property *safe*: which codes are
emitted, who is recorded as having cut what, how a multi-leg structure is apportioned, and the fact that
none of it moves when free text is injected anywhere in the inputs.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from mizan.contracts import (
    AdvisoryOpinion,
    ReasonCode,
    canonical_json,
    dec,
    dstr,
    format_ts,
    sorted_reason_codes,
)
from mizan.governor import govern
from tests.fixtures import (
    FIXED_NOW,
    OPTION_EXPIRY,
    TENANT_B,
    injection_reasoning,
    make_checks,
    make_context,
    make_evaluation,
    make_policy,
    make_proposal,
)

REDUCE_CODE = ReasonCode.CAPITAL_THRESHOLD_EXCEEDED
REJECT_CODE = ReasonCode.RESTRICTED_SYMBOL


# ----------------------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------------------


def codes(obj) -> set[str]:
    return {str(code.value) for code in obj.reason_codes}


def setup(*, verdict="PASS", recommended=None, proposal=None, policy=None, **evaluation_overrides):
    """A linked (proposal, policy, context, evaluation) chain built only from the shared fixtures."""
    policy = policy or make_policy()
    context = make_context(policy=policy)
    proposal = proposal or make_proposal()
    if recommended is None:
        recommended = {"PASS": dstr(proposal.total_quantity), "REJECT": "0"}[verdict]
    reason_codes = evaluation_overrides.pop("reason_codes", None)
    if reason_codes is None:
        reason_codes = {"PASS": [], "REDUCE": [REDUCE_CODE], "REJECT": [REJECT_CODE]}[verdict]
    evaluation = make_evaluation(
        proposal=proposal,
        context=context,
        policy_snapshot=policy,
        verdict=verdict,
        recommended_quantity=recommended,
        reason_codes=reason_codes,
        **evaluation_overrides,
    )
    return proposal, policy, context, evaluation


def opinion(recommendation, quantity=None, *, available=True, invoked=True, reasoning="") -> AdvisoryOpinion:
    return AdvisoryOpinion(
        profile="unit-test",
        invoked=invoked,
        available=available,
        recommendation=recommendation,
        recommended_quantity=quantity,
        reasoning=reasoning,
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def option_leg(index, side, strike, quantity, price):
    return {
        "leg_index": index,
        "side": side,
        "contract_type": "call",
        "strike": strike,
        "expiry": OPTION_EXPIRY,
        "quantity": quantity,
        "limit_price": price,
        "order_type": "limit",
    }


def spread_proposal(first="4", second="4"):
    """A two-leg bull call spread: the structure a broken apportionment would turn into a naked short."""
    return make_proposal(
        asset_class="equity_option",
        strategy="bull_call_spread",
        legs=[option_leg(0, "buy", "230", first, "1.85"), option_leg(1, "sell", "240", second, "0.85")],
    )


def ratio_proposal(first="4", second="2"):
    """A 2:1 equity structure, so that "preserve the ratio" means something other than "equal legs"."""
    return make_proposal(
        strategy="custom",
        legs=[
            {
                "leg_index": index,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": quantity,
                "limit_price": "228.50",
                "order_type": "limit",
            }
            for index, quantity in enumerate((first, second))
        ],
    )


# ----------------------------------------------------------------------------------------------------------
# A deterministic rejection is final
# ----------------------------------------------------------------------------------------------------------


def test_hard_rejection_is_upheld_whatever_the_advisory_said():
    proposal, policy, context, evaluation = setup(verdict="REJECT")
    for advisory in (
        None,
        opinion("CONCUR"),
        opinion("REDUCE", "1"),
        opinion("REJECT"),
        opinion(None, available=False),
        opinion("CONCUR", reasoning=injection_reasoning()),
    ):
        decision = govern(proposal, evaluation, policy, advisory, context=context)
        assert decision.verdict == "REJECT"
        assert decision.authorized.total_quantity == "0"
        assert decision.authorized.legs == []
        assert "HARD_REJECTION_UPHELD" in codes(decision)
        assert codes(evaluation) <= codes(decision)


def test_hard_rejection_records_who_cut_the_size_to_zero():
    proposal, policy, context, evaluation = setup(verdict="REJECT")
    decision = govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    (reduction,) = decision.authorized.reductions
    assert reduction.source == "deterministic"
    assert reduction.from_quantity == evaluation.original_quantity
    assert reduction.to_quantity == "0"
    assert reduction.reason_code == ReasonCode.HARD_REJECTION_UPHELD


def test_a_hard_rejection_keeps_the_advisory_opinion_for_the_record():
    proposal, policy, context, evaluation = setup(verdict="REJECT")
    advised = opinion("REDUCE", "3")
    decision = govern(proposal, evaluation, policy, advised, context=context)
    assert decision.llm_advisory == advised  # recorded, and completely without effect


# ----------------------------------------------------------------------------------------------------------
# Advisory arbitration
# ----------------------------------------------------------------------------------------------------------


def test_advisory_reject_rejects_an_otherwise_passing_proposal():
    proposal, policy, context, evaluation = setup()
    decision = govern(proposal, evaluation, policy, opinion("REJECT"), context=context)
    assert decision.verdict == "REJECT"
    assert decision.authorized.total_quantity == "0"
    assert decision.authorized.legs == []
    assert "ADVISORY_REJECT" in codes(decision)
    assert "HARD_REJECTION_UPHELD" not in codes(decision)
    (reduction,) = decision.authorized.reductions
    assert reduction.source == "advisory"
    assert reduction.reason_code == ReasonCode.ADVISORY_REJECT


def test_advisory_reduce_below_the_cap_is_applied_and_attributed():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="6")
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "3"), context=context)
    assert decision.verdict == "REDUCE"
    assert decision.authorized.total_quantity == "3"
    assert "ADVISORY_REDUCE" in codes(decision)
    assert "ADVISORY_CLAMPED" not in codes(decision)
    deterministic, advisory = decision.authorized.reductions
    assert (deterministic.source, deterministic.from_quantity, deterministic.to_quantity) == (
        "deterministic",
        "10",
        "6",
    )
    assert (advisory.source, advisory.from_quantity, advisory.to_quantity) == ("advisory", "6", "3")
    assert advisory.reason_code == ReasonCode.ADVISORY_REDUCE


def test_advisory_reduce_exactly_at_the_cap_cuts_nothing():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="6")
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "6"), context=context)
    assert decision.authorized.total_quantity == "6"
    assert "ADVISORY_REDUCE" not in codes(decision)
    assert "ADVISORY_CLAMPED" not in codes(decision)
    assert [reduction.source for reduction in decision.authorized.reductions] == ["deterministic"]


def test_advisory_reduce_above_the_cap_is_clamped_to_the_cap():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "9"), context=context)
    assert decision.authorized.total_quantity == "4"
    assert "ADVISORY_CLAMPED" in codes(decision)
    assert "ADVISORY_REDUCE" not in codes(decision)


def test_advisory_reduce_above_the_original_quantity_is_still_clamped():
    proposal, policy, context, evaluation = setup()
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "10000000"), context=context)
    assert decision.verdict == "APPROVE"
    assert dec(decision.authorized.total_quantity) == dec(evaluation.recommended_quantity)
    assert "ADVISORY_CLAMPED" in codes(decision)


def test_advisory_concur_authorizes_exactly_the_deterministic_cap():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    decision = govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    assert decision.authorized.total_quantity == "4"
    assert decision.verdict == "REDUCE"
    assert not (codes(decision) - codes(evaluation))


def test_a_malformed_reduce_is_treated_as_unavailable_not_as_authority():
    """An opinion that cannot be understood must not become more powerful than one that can."""
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    broken = opinion("CONCUR").model_copy(update={"recommendation": "REDUCE", "recommended_quantity": None})
    decision = govern(proposal, evaluation, policy, broken, context=context)
    assert decision.authorized.total_quantity == "4"
    assert not (codes(decision) - codes(evaluation))
    assert decision.llm_advisory is not None and decision.llm_advisory.available is False


def test_an_opinion_the_contract_cannot_carry_is_recorded_as_unavailable():
    """Forced past the contract's validators, an opinion still cannot make the decision less safe."""
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    forced = opinion("CONCUR").model_copy(update={"recommendation": "REDUCE", "recommended_quantity": "0"})
    decision = govern(proposal, evaluation, policy, forced, context=context)
    assert dec(decision.authorized.total_quantity) <= dec(evaluation.recommended_quantity)
    assert decision.llm_advisory is not None
    assert decision.llm_advisory.available is False
    assert decision.llm_advisory.recommendation is None


def test_an_object_that_is_not_an_advisory_opinion_is_recorded_as_unavailable():
    class Rogue:
        profile = "rogue"
        invoked = True
        available = True
        recommendation = "APPROVE_MORE"
        recommended_quantity = "999"
        authority_ceiling = "unlimited"

    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    decision = govern(proposal, evaluation, policy, Rogue(), context=context)
    assert decision.authorized.total_quantity == "4"
    assert decision.llm_advisory is not None and decision.llm_advisory.available is False


# ----------------------------------------------------------------------------------------------------------
# Unavailability
# ----------------------------------------------------------------------------------------------------------


def test_an_unavailable_advisory_leaves_the_deterministic_verdict_standing():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    assert policy.fail_closed.on_advisory_unavailable is False
    for advisory in (None, opinion(None, available=False), opinion(None, available=False, invoked=False)):
        decision = govern(proposal, evaluation, policy, advisory, context=context)
        assert decision.verdict == "REDUCE"
        assert decision.authorized.total_quantity == "4"


def test_absent_unavailable_and_concurring_advisories_produce_the_same_verdict_hash():
    """Invariant 18 in miniature: switching the semantic layer off changes no deterministic outcome."""
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    hashes = {
        govern(proposal, evaluation, policy, advisory, context=context).verdict_hash
        for advisory in (None, opinion(None, available=False), opinion("CONCUR"))
    }
    assert len(hashes) == 1


def test_fail_closed_on_advisory_unavailable_rejects():
    policy = make_policy(
        fail_closed={
            "on_missing_market_data": True,
            "on_missing_portfolio_state": True,
            "on_engine_degraded": True,
            "on_advisory_unavailable": True,
        }
    )
    proposal, policy, context, evaluation = setup(policy=policy)
    for advisory in (None, opinion(None, available=False)):
        decision = govern(proposal, evaluation, policy, advisory, context=context)
        assert decision.verdict == "REJECT"
        assert decision.authorized.total_quantity == "0"
        assert "ADVISORY_UNAVAILABLE" in codes(decision)
        assert "HARD_REJECTION_UPHELD" not in codes(decision)


def test_fail_closed_does_not_fire_when_the_policy_disables_the_advisory_layer():
    """A tenant that switched the advisory off is not a tenant whose advisory is failing."""
    policy = make_policy(
        advisory={"enabled": False, "profile": "standard_advisory", "authority_ceiling": "reduce_or_reject"},
        fail_closed={
            "on_missing_market_data": True,
            "on_missing_portfolio_state": True,
            "on_engine_degraded": True,
            "on_advisory_unavailable": True,
        },
    )
    proposal, policy, context, evaluation = setup(policy=policy)
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert decision.verdict == "APPROVE"


def test_an_advisory_supplied_while_disabled_is_still_honoured_downward():
    policy = make_policy(
        advisory={"enabled": False, "profile": "standard_advisory", "authority_ceiling": "reduce_or_reject"}
    )
    proposal, policy, context, evaluation = setup(policy=policy)
    decision = govern(proposal, evaluation, policy, opinion("REJECT"), context=context)
    assert decision.verdict == "REJECT"


# ----------------------------------------------------------------------------------------------------------
# Apportionment (R-OPT-3: a spread that loses a leg is a naked short)
# ----------------------------------------------------------------------------------------------------------


def test_single_leg_apportionment_takes_the_whole_authorized_quantity():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert [(leg.leg_index, leg.quantity) for leg in decision.authorized.legs] == [(0, "4")]


def test_a_spread_is_authorized_in_whole_ratio_blocks():
    proposal = spread_proposal("4", "4")
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="5", proposal=proposal)
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert [(leg.leg_index, leg.quantity) for leg in decision.authorized.legs] == [(0, "2"), (1, "2")]
    assert decision.authorized.total_quantity == "4"  # 5 would have unbalanced the structure
    assert "SIZE_REDUCED_TO_POLICY_CAP" in codes(decision)
    structural = decision.authorized.reductions[-1]
    assert (structural.source, structural.from_quantity, structural.to_quantity) == (
        "deterministic",
        "5",
        "4",
    )


def test_a_spread_that_cannot_keep_its_structure_is_rejected_not_broken():
    proposal = spread_proposal("4", "4")
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="1", proposal=proposal)
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert decision.verdict == "REJECT"
    assert decision.authorized.legs == []
    assert "STRUCTURE_INVALID" in codes(decision)


def test_an_unequal_leg_ratio_is_preserved_exactly():
    proposal = ratio_proposal("4", "2")
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="5", proposal=proposal)
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert [(leg.leg_index, leg.quantity) for leg in decision.authorized.legs] == [(0, "2"), (1, "1")]
    assert decision.authorized.total_quantity == "3"


def test_an_untouched_multi_leg_proposal_is_approved_leg_for_leg():
    proposal = spread_proposal("4", "4")
    proposal, policy, context, evaluation = setup(proposal=proposal)
    decision = govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    assert decision.verdict == "APPROVE"
    assert [(leg.leg_index, leg.quantity) for leg in decision.authorized.legs] == [(0, "4"), (1, "4")]
    assert decision.authorized.reductions == []


def test_the_advisory_cannot_break_a_structure_either():
    proposal = spread_proposal("4", "4")
    proposal, policy, context, evaluation = setup(proposal=proposal)
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "3"), context=context)
    assert [leg.quantity for leg in decision.authorized.legs] == ["1", "1"]
    assert decision.authorized.total_quantity == "2"


# ----------------------------------------------------------------------------------------------------------
# Quantities, notionals and identity
# ----------------------------------------------------------------------------------------------------------


def test_the_authorized_notional_tracks_the_authorized_quantity():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    at_cap = govern(proposal, evaluation, policy, None, context=context)
    assert at_cap.authorized.total_notional == evaluation.recommended_notional
    below_cap = govern(proposal, evaluation, policy, opinion("REDUCE", "2"), context=context)
    assert below_cap.authorized.total_notional == "457"  # 2285 x 2/10, in decimal, not binary


def test_an_approve_keeps_the_original_notional():
    proposal, policy, context, evaluation = setup()
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert decision.verdict == "APPROVE"
    assert decision.authorized.total_notional == evaluation.original_notional
    assert decision.original.total_quantity == evaluation.original_quantity


def test_the_deterministic_reduction_names_the_check_that_bound_the_size():
    proposal = make_proposal()
    policy = make_policy()
    context = make_context(policy=policy)
    checks = make_checks(
        policy,
        position_limit={
            "passed": False,
            "severity": "warning",
            "reason_code": "POSITION_LIMIT_EXCEEDED",
            "recommended_quantity": "4",
        },
    )
    evaluation = make_evaluation(
        proposal=proposal,
        context=context,
        policy_snapshot=policy,
        verdict="REDUCE",
        recommended_quantity="4",
        reason_codes=[ReasonCode.POSITION_LIMIT_EXCEEDED],
        checks=checks,
    )
    decision = govern(proposal, evaluation, policy, None, context=context)
    (reduction,) = decision.authorized.reductions
    assert reduction.reason_code == ReasonCode.POSITION_LIMIT_EXCEEDED


def test_the_decision_timestamp_is_the_evaluation_time_not_a_wall_clock():
    policy = make_policy()
    long_ago = format_ts(FIXED_NOW - timedelta(days=90))
    context = make_context(policy=policy, evaluated_at=long_ago)
    proposal = make_proposal()
    evaluation = make_evaluation(proposal=proposal, context=context, policy_snapshot=policy)
    decision = govern(proposal, evaluation, policy, None, context=context)
    assert decision.decision_timestamp == long_ago


def test_decisions_carry_unique_uuid7_identifiers():
    proposal, policy, context, evaluation = setup()
    first = govern(proposal, evaluation, policy, None, context=context)
    second = govern(proposal, evaluation, policy, None, context=context)
    assert first.decision_id != second.decision_id
    assert first.decision_id[14] == "7"
    assert first.verdict_hash == second.verdict_hash


def test_reason_codes_are_sorted_and_de_duplicated():
    proposal, policy, context, evaluation = setup(
        verdict="REDUCE",
        recommended="4",
        reason_codes=sorted_reason_codes([ReasonCode.POSITION_LIMIT_EXCEEDED, REDUCE_CODE, REDUCE_CODE]),
    )
    decision = govern(proposal, evaluation, policy, opinion("REDUCE", "999"), context=context)
    values = [str(code.value) for code in decision.reason_codes]
    assert values == sorted(set(values))
    assert codes(evaluation) <= codes(decision)


def test_a_proposal_that_does_not_belong_to_the_evaluation_is_rejected():
    proposal, policy, context, evaluation = setup()
    other = make_proposal(symbol="MSFT")
    decision = govern(other, evaluation, policy, None, context=context)
    assert decision.verdict == "REJECT"
    assert "SCHEMA_INVALID" in codes(decision)


def test_a_tenant_disagreement_is_rejected():
    proposal, policy, context, evaluation = setup()
    other_tenant = make_policy(tenant_id=TENANT_B)
    decision = govern(proposal, evaluation, other_tenant, None, context=context)
    assert decision.verdict == "REJECT"
    assert "TENANT_MISMATCH" in codes(decision)


# ----------------------------------------------------------------------------------------------------------
# Free text never reaches the verdict
# ----------------------------------------------------------------------------------------------------------


def _without_decision_id(decision) -> str:
    payload = decision.model_dump(mode="json")
    payload.pop("decision_id")
    return canonical_json(payload)


def test_injected_text_in_the_proposal_changes_nothing():
    policy = make_policy()
    context = make_context(policy=policy)
    clean = make_proposal(reasoning="")
    poisoned = make_proposal(reasoning=injection_reasoning())
    evaluation = make_evaluation(proposal=clean, context=context, policy_snapshot=policy)
    clean_decision = govern(clean, evaluation, policy, None, context=context)
    poisoned_decision = govern(poisoned, evaluation, policy, None, context=context)
    assert _without_decision_id(clean_decision) == _without_decision_id(poisoned_decision)
    assert injection_reasoning() not in canonical_json(poisoned_decision)


def test_injected_text_in_the_advisory_changes_nothing():
    proposal, policy, context, evaluation = setup(verdict="REDUCE", recommended="4")
    plain = govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    injected = govern(
        proposal, evaluation, policy, opinion("CONCUR", reasoning=injection_reasoning()), context=context
    )
    assert plain.verdict_hash == injected.verdict_hash
    assert plain.authorized.total_quantity == injected.authorized.total_quantity
    assert plain.reason_codes == injected.reason_codes


def test_injected_text_in_every_free_text_field_at_once_changes_nothing():
    """Poison the proposal's reasoning and the advisory's reasoning at the same time: same bytes out."""
    policy = make_policy()
    context = make_context(policy=policy)
    poison = injection_reasoning()
    clean = make_proposal(reasoning="")
    poisoned = make_proposal(reasoning=poison)
    assert clean.proposal_id == poisoned.proposal_id  # free text is excluded from identity
    evaluation = make_evaluation(proposal=clean, context=context, policy_snapshot=policy)
    clean_decision = govern(clean, evaluation, policy, opinion("CONCUR"), context=context)
    poisoned_decision = govern(
        poisoned, evaluation, policy, opinion("CONCUR", reasoning=poison), context=context
    )
    assert clean_decision.verdict == poisoned_decision.verdict
    assert clean_decision.verdict_hash == poisoned_decision.verdict_hash
    assert clean_decision.authorized.total_quantity == poisoned_decision.authorized.total_quantity


def test_a_proposal_whose_hashed_fields_changed_no_longer_matches_its_evaluation():
    """``signal_sources`` is data, not free text: changing it changes identity, and identity is checked."""
    policy = make_policy()
    context = make_context(policy=policy)
    clean = make_proposal()
    relabelled = make_proposal(signal_sources=["vendor:polygon"])
    assert clean.proposal_id != relabelled.proposal_id
    evaluation = make_evaluation(proposal=clean, context=context, policy_snapshot=policy)
    decision = govern(relabelled, evaluation, policy, None, context=context)
    assert decision.verdict == "REJECT"
    assert "SCHEMA_INVALID" in codes(decision)


# ----------------------------------------------------------------------------------------------------------
# The property, over every opinion the contract can express
# ----------------------------------------------------------------------------------------------------------


ADVISED = st.one_of(
    st.none(),
    st.just(("CONCUR", None)),
    st.just(("REJECT", None)),
    st.integers(min_value=1, max_value=10**9).map(lambda value: ("REDUCE", str(value))),
    st.just(("UNAVAILABLE", None)),
)


@settings(max_examples=60, deadline=None, database=None, derandomize=True)
@given(advised=ADVISED, cap=st.integers(min_value=0, max_value=10))
def test_authorized_never_exceeds_the_deterministic_cap_for_any_opinion(advised, cap):
    """For ANY advisory opinion whatsoever: authorized <= evaluation.recommended_quantity."""
    verdict = "PASS" if cap == 10 else ("REJECT" if cap == 0 else "REDUCE")
    proposal, policy, context, evaluation = setup(verdict=verdict, recommended=str(cap))
    if advised is None:
        advisory = None
    elif advised[0] == "UNAVAILABLE":
        advisory = opinion(None, available=False)
    else:
        advisory = opinion(advised[0], advised[1])
    decision = govern(proposal, evaluation, policy, advisory, context=context)
    authorized = dec(decision.authorized.total_quantity)
    assert authorized <= Decimal(cap)
    assert authorized <= dec(decision.original.total_quantity)
    assert authorized <= proposal.total_quantity
    leg_total = sum((dec(leg.quantity) for leg in decision.authorized.legs), start=Decimal(0))
    assert leg_total == authorized
