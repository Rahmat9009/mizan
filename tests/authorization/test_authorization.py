"""Unit tests for ``mizan.authorization`` — minimum authority, shortest time, bound to the state.

``issue`` mints authority from a decision (never from a request), and ``validate`` is the only thing that
says an authorization may still be acted on. The tests below pin the three properties the execution gate
depends on: the window is exactly the policy's TTL and its upper boundary is exclusive, the scope is the
decision's, and an authorization that has drifted from the decision, the proposal or itself cannot pass.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from mizan.authorization import InMemoryAuthorizationRegistry, issue, validate
from mizan.contracts import (
    AdvisoryOpinion,
    ReasonCode,
    authorization_hash_for,
    dec,
    idempotency_key_for,
    object_hash,
    parse_ts,
)
from mizan.contracts.errors import AuthorizationError
from mizan.governor import govern
from tests.fixtures import (
    FIXED_NOW,
    OPTION_EXPIRY,
    TENANT_B,
    make_context,
    make_evaluation,
    make_institutional_context,
    make_institutional_policy,
    make_option_proposal,
    make_policy,
    make_proposal,
)


def codes(error: AuthorizationError) -> set[str]:
    return {str(code.value) for code in error.reason_codes}


def opinion(recommendation, quantity=None) -> AdvisoryOpinion:
    return AdvisoryOpinion(
        profile="unit-test",
        invoked=True,
        available=True,
        recommendation=recommendation,
        recommended_quantity=quantity,
        reasoning="",
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def chain(*, verdict="PASS", recommended=None, advisory=None, policy=None, context=None, proposal=None):
    """A real (proposal, policy, context, evaluation, decision) chain through the real governor."""
    policy = policy or make_policy()
    context = context or make_context(policy=policy)
    proposal = proposal or make_proposal()
    reason_codes = {
        "PASS": [],
        "REDUCE": [ReasonCode.CAPITAL_THRESHOLD_EXCEEDED],
        "REJECT": [ReasonCode.RESTRICTED_SYMBOL],
    }[verdict]
    if recommended is None:
        recommended = {"PASS": "10", "REJECT": "0"}[verdict]
    evaluation = make_evaluation(
        proposal=proposal,
        context=context,
        policy_snapshot=policy,
        verdict=verdict,
        recommended_quantity=recommended,
        reason_codes=reason_codes,
    )
    decision = govern(proposal, evaluation, policy, advisory, context=context)
    return proposal, policy, context, evaluation, decision


def issued(**kwargs):
    proposal, policy, context, _evaluation, decision = chain(**kwargs)
    auth = issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    return auth, decision, proposal, policy, context


# ----------------------------------------------------------------------------------------------------------
# issue
# ----------------------------------------------------------------------------------------------------------


def test_a_rejected_decision_can_never_be_authorized():
    proposal, policy, context, _evaluation, decision = chain(verdict="REJECT")
    assert decision.verdict == "REJECT"
    with pytest.raises(AuthorizationError) as error:
        issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


def test_an_advisory_rejection_cannot_be_authorized_either():
    proposal, policy, context, _evaluation, decision = chain(advisory=opinion("REJECT"))
    with pytest.raises(AuthorizationError):
        issue(decision, proposal, policy, now=FIXED_NOW, context=context)


def test_the_scope_is_built_from_the_decision_not_from_the_request():
    """The proposal asked for 10; the decision allowed 3; the authorization carries 3."""
    auth, decision, proposal, _policy, _context = issued(advisory=opinion("REDUCE", "3"))
    assert proposal.total_quantity == dec("10")
    assert decision.authorized.total_quantity == "3"
    assert auth.scope.total_quantity == "3"
    assert [leg.quantity for leg in auth.scope.legs] == ["3"]
    assert auth.scope.symbol == proposal.symbol
    assert auth.scope.intent == proposal.intent
    assert [leg.side for leg in auth.scope.legs] == [leg.side for leg in proposal.legs]


def test_the_scope_describes_the_proposal_structure():
    auth, _decision, proposal, _policy, _context = issued()
    leg = auth.scope.legs[0]
    source = proposal.legs[0]
    assert (leg.order_type, leg.limit_price) == (source.order_type, source.limit_price)
    assert leg.occ_symbol is None and leg.contract_type is None


def test_an_option_scope_carries_the_contract_that_will_be_traded():
    proposal = make_option_proposal()
    auth, _decision, _proposal, _policy, _context = issued(proposal=proposal, recommended="5")
    leg = auth.scope.legs[0]
    assert auth.scope.asset_class == "equity_option"
    assert leg.contract_type == "call"
    assert leg.expiry == OPTION_EXPIRY
    assert leg.occ_symbol == proposal.legs[0].occ_symbol(proposal.symbol)


def test_a_multi_leg_decision_round_trips_through_issue_and_validate():
    """What L3 will actually call: govern -> issue -> validate(decision, proposal), on a spread."""
    spread = make_proposal(
        asset_class="equity_option",
        strategy="bull_call_spread",
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": "call",
                "strike": "230",
                "expiry": OPTION_EXPIRY,
                "quantity": "4",
                "limit_price": "1.85",
                "order_type": "limit",
            },
            {
                "leg_index": 1,
                "side": "sell",
                "contract_type": "call",
                "strike": "240",
                "expiry": OPTION_EXPIRY,
                "quantity": "4",
                "limit_price": "0.85",
                "order_type": "limit",
            },
        ],
    )
    proposal, policy, context, _evaluation, decision = chain(
        proposal=spread, verdict="REDUCE", recommended="5", advisory=opinion("CONCUR")
    )
    assert [leg.quantity for leg in decision.authorized.legs] == ["2", "2"]
    auth = issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    assert [leg.quantity for leg in auth.scope.legs] == ["2", "2"]
    assert [leg.side for leg in auth.scope.legs] == ["buy", "sell"]
    assert auth.scope.total_quantity == "4"
    assert validate(auth, now=FIXED_NOW, decision=decision, proposal=proposal) is None


def test_the_ttl_comes_from_the_policy_and_defines_the_window():
    for ttl in (5, 15, 30):
        policy = make_policy(authorization={"ttl_seconds": ttl})
        auth, *_ = issued(policy=policy)
        assert auth.ttl_seconds == ttl
        assert parse_ts(auth.issued_at) == FIXED_NOW
        assert parse_ts(auth.expires_at) - parse_ts(auth.issued_at) == timedelta(seconds=ttl)


def test_the_authorization_is_paper_only_and_single_use():
    auth, *_ = issued()
    assert auth.environment == "paper"
    assert auth.single_use is True


def test_the_idempotency_key_is_derived_and_stable_across_issues():
    proposal, policy, context, _evaluation, decision = chain()
    first = issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    second = issue(decision, proposal, policy, now=FIXED_NOW + timedelta(seconds=1), context=context)
    assert first.idempotency_key == second.idempotency_key
    assert first.auth_id != second.auth_id
    assert first.idempotency_key == idempotency_key_for(
        decision.tenant_id, decision.proposal_id, first.scope.legs
    )
    assert first.idempotency_key.startswith("mz1-")


def test_a_different_size_is_a_different_idempotency_key():
    full, *_ = issued()
    reduced, *_ = issued(advisory=opinion("REDUCE", "3"))
    assert full.idempotency_key != reduced.idempotency_key


def test_the_bound_state_records_the_state_the_decision_was_made_under():
    auth, decision, _proposal, policy, context = issued()
    bound = auth.bound_state
    assert bound.policy_hash == policy.policy_hash == decision.policy.hash
    assert bound.portfolio_snapshot_id == context.portfolio_snapshot.snapshot_id
    assert bound.portfolio_state_hash == object_hash(context.portfolio_snapshot)
    assert bound.market_snapshot_id == context.market_snapshot.snapshot_id
    assert bound.response_level == context.response_level == 0
    assert bound.path_state_hash is None
    assert bound.aggregate_state_hash is None


def test_the_bound_state_carries_path_and_aggregate_hashes_when_the_context_has_them():
    policy = make_institutional_policy()
    context = make_institutional_context(policy=policy)
    # this policy fails closed on advisory unavailability, so the chain needs a real opinion
    auth, *_ = issued(policy=policy, context=context, advisory=opinion("CONCUR"))
    assert auth.bound_state.path_state_hash == object_hash(context.path_state)
    assert auth.bound_state.aggregate_state_hash == object_hash(context.aggregate_state)
    assert auth.bound_state.response_level == context.response_level


def test_a_changed_snapshot_changes_the_bound_state_hash():
    first, *_ = issued()
    context = make_context(policy=make_policy(), portfolio_snapshot=_shifted_portfolio())
    second, *_ = issued(context=context)
    assert first.bound_state.portfolio_state_hash != second.bound_state.portfolio_state_hash


def _shifted_portfolio():
    from tests.fixtures import make_portfolio_snapshot

    return make_portfolio_snapshot(equity="123456.78")


def test_issue_uses_the_supplied_clock_only():
    later = FIXED_NOW + timedelta(days=1)
    proposal, policy, context, _evaluation, decision = chain()
    auth = issue(decision, proposal, policy, now=later, context=context)
    assert parse_ts(auth.issued_at) == later


def test_issue_refuses_a_decision_from_another_proposal():
    proposal, policy, context, _evaluation, decision = chain()
    with pytest.raises(AuthorizationError) as error:
        issue(decision, make_proposal(symbol="MSFT"), policy, now=FIXED_NOW, context=context)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_issue_refuses_a_tenant_disagreement():
    proposal, policy, context, _evaluation, decision = chain()
    with pytest.raises(AuthorizationError) as error:
        issue(decision, proposal, make_policy(tenant_id=TENANT_B), now=FIXED_NOW, context=context)
    assert codes(error.value) & {"TENANT_MISMATCH", "STATE_BINDING_MISMATCH"}


def test_issue_refuses_a_policy_the_decision_was_not_made_under():
    proposal, policy, context, _evaluation, decision = chain()
    other = make_policy(order={"max_notional": "999999", "max_quantity": "500", "max_legs": 4})
    assert other.policy_hash != policy.policy_hash
    with pytest.raises(AuthorizationError) as error:
        issue(decision, proposal, other, now=FIXED_NOW, context=context)
    assert "STATE_BINDING_MISMATCH" in codes(error.value)


def test_issue_refuses_a_context_with_no_state_to_bind_to():
    proposal, policy, context, _evaluation, decision = chain()
    for field, code in (
        ("portfolio_snapshot", "PORTFOLIO_STATE_MISSING"),
        ("market_snapshot", "MARKET_DATA_MISSING"),
    ):
        stateless = context.model_copy(update={field: None})
        with pytest.raises(AuthorizationError) as error:
            issue(decision, proposal, policy, now=FIXED_NOW, context=stateless)
        assert code in codes(error.value)


# ----------------------------------------------------------------------------------------------------------
# validate: the window
# ----------------------------------------------------------------------------------------------------------


def test_a_fresh_authorization_validates_and_returns_none():
    auth, decision, proposal, _policy, _context = issued()
    assert validate(auth, now=FIXED_NOW, decision=decision, proposal=proposal) is None


def test_the_window_is_closed_at_the_top():
    auth, *_ = issued()
    expires = parse_ts(auth.expires_at)
    validate(auth, now=expires - timedelta(microseconds=1))
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=expires)
    assert "AUTHORIZATION_EXPIRED" in codes(error.value)


def test_an_expired_authorization_is_expired_by_a_whole_second_too():
    auth, *_ = issued()
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=parse_ts(auth.expires_at) + timedelta(seconds=1))
    assert "AUTHORIZATION_EXPIRED" in codes(error.value)


def test_an_authorization_from_the_future_is_not_yet_valid():
    auth, *_ = issued()
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=parse_ts(auth.issued_at) - timedelta(microseconds=1))
    assert "AUTHORIZATION_NOT_YET_VALID" in codes(error.value)


def test_validation_requires_an_absolute_time():
    auth, *_ = issued()
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW.replace(tzinfo=None))
    assert "AUTHORIZATION_INVALID" in codes(error.value)


# ----------------------------------------------------------------------------------------------------------
# validate: self-consistency
# ----------------------------------------------------------------------------------------------------------


def test_a_tampered_quantity_does_not_validate():
    auth, *_ = issued()
    bigger = auth.scope.model_copy(
        update={
            "total_quantity": "50",
            "legs": [auth.scope.legs[0].model_copy(update={"quantity": "50"})],
        }
    )
    with pytest.raises(AuthorizationError) as error:
        validate(auth.model_copy(update={"scope": bigger}), now=FIXED_NOW)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


def test_a_tampered_identity_does_not_validate():
    auth, *_ = issued()
    forged = auth.model_copy(update={"agent_id": "someone-else"})
    assert forged.authorization_hash != authorization_hash_for(forged)
    with pytest.raises(AuthorizationError) as error:
        validate(forged, now=FIXED_NOW)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


def test_a_forged_idempotency_key_does_not_validate():
    auth, *_ = issued()
    forged = auth.model_copy(update={"idempotency_key": "mz1-" + "0" * 40})
    with pytest.raises(AuthorizationError) as error:
        validate(forged, now=FIXED_NOW)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


def test_a_stretched_ttl_does_not_validate():
    auth, *_ = issued()
    stretched = auth.model_copy(
        update={"expires_at": "2026-09-02T18:40:00.000000Z", "ttl_seconds": 3600}
    )
    with pytest.raises(AuthorizationError) as error:
        validate(stretched, now=FIXED_NOW)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


def test_legs_that_do_not_sum_to_the_total_do_not_validate():
    auth, *_ = issued()
    broken_scope = auth.scope.model_copy(
        update={"legs": [auth.scope.legs[0].model_copy(update={"quantity": "9"})]}
    )
    broken = auth.model_copy(update={"scope": broken_scope})
    forged = broken.model_copy(
        update={
            "idempotency_key": idempotency_key_for(broken.tenant_id, broken.proposal_id, broken_scope.legs)
        }
    )
    with pytest.raises(AuthorizationError) as error:
        validate(forged, now=FIXED_NOW)
    assert "AUTHORIZATION_INVALID" in codes(error.value)


# ----------------------------------------------------------------------------------------------------------
# validate: scope
# ----------------------------------------------------------------------------------------------------------


def test_an_authorization_does_not_match_another_decision():
    auth, _decision, proposal, policy, context = issued()
    _p, _pol, _ctx, _ev, other_decision = chain(policy=policy, context=context, proposal=proposal)
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, decision=other_decision)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_authorization_does_not_match_a_resized_decision():
    auth, decision, *_ = issued()
    smaller = decision.model_copy(
        update={
            "authorized": decision.authorized.model_copy(
                update={
                    "total_quantity": "2",
                    "legs": [decision.authorized.legs[0].model_copy(update={"quantity": "2"})],
                }
            )
        }
    )
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, decision=smaller)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_authorization_never_matches_a_rejecting_decision():
    auth, decision, *_ = issued()
    rejected = decision.model_copy(update={"verdict": "REJECT"})
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, decision=rejected)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_authorization_does_not_match_another_proposal():
    auth, *_ = issued()
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, proposal=make_proposal(symbol="MSFT"))
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_authorization_does_not_match_a_flipped_side():
    auth, *_ = issued()
    flipped = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "sell",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ]
    )
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, proposal=flipped)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_authorization_for_more_than_the_proposal_asked_does_not_match():
    auth, *_ = issued()
    smaller_request = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "1",
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ]
    )
    with pytest.raises(AuthorizationError) as error:
        validate(auth, now=FIXED_NOW, proposal=smaller_request)
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(error.value)


def test_an_expired_authorization_is_expired_before_any_scope_question_is_asked():
    auth, decision, proposal, *_ = issued()
    with pytest.raises(AuthorizationError) as error:
        validate(
            auth,
            now=parse_ts(auth.expires_at),
            decision=decision,
            proposal=make_proposal(symbol="MSFT"),
        )
    assert "AUTHORIZATION_EXPIRED" in codes(error.value)


# ----------------------------------------------------------------------------------------------------------
# Single use
# ----------------------------------------------------------------------------------------------------------


def test_an_authorization_is_consumable_exactly_once():
    registry = InMemoryAuthorizationRegistry()
    auth, *_ = issued()
    assert registry.consume(auth.auth_id) is True
    assert registry.consume(auth.auth_id) is False
    assert registry.was_consumed(auth.auth_id) is True


def test_racing_threads_cannot_both_consume_the_same_authorization():
    registry = InMemoryAuthorizationRegistry()
    auth, *_ = issued()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def race():
        barrier.wait()
        results.append(registry.consume(auth.auth_id))

    workers = [threading.Thread(target=race) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert results.count(True) == 1
