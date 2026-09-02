"""Invariant 14 - Hard Rules E9 (TOCTOU defence) and E5 (no silent resizing).

Pass criterion: an authorization issued against a context with ample buying power, executed while the broker's
fresh portfolio supports fewer units, is BLOCKED with REAUTHORIZATION_REQUIRED (+ TOCTOU_STATE_CHANGED),
revalidation.performed True, revalidation.supported False, fresh_recommended_quantity < auth.scope.total_quantity,
a fresh evaluation id, and nothing reaches the broker - the gate never submits a smaller order (E5). The happy
path (state unchanged) reaches WOULD_SUBMIT in dry-run with revalidation.performed True and supported True,
proving the re-validation runs on every execution. Addendum 1: a fresh context whose response_level exceeds
auth.bound_state.response_level is BLOCKED with REAUTHORIZATION_REQUIRED and RESPONSE_LEVEL_ESCALATED.
"""
from __future__ import annotations

from decimal import Decimal

from mizan import authorization, governor, risk
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts.types import dec, dstr
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW, make_policy, make_portfolio_snapshot, make_proposal
from tests.invariants._support import (
    RecordingBroker,
    ScriptedContextProvider,
    as_decimal,
    codes,
    context_for,
    quantity_of,
)


def _issue_chain(policy, context, proposal):
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", (
        "the default fixture proposal must be authorizable under the default policy: " + str(codes(evaluation))
    )
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    assert quantity_of(auth.scope) == quantity_of(decision.authorized) > Decimal(0)
    return evaluation, decision, auth


def _gate(policy, broker, provider, *, dry_run):
    return ExecutionGate(
        broker=broker,
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=provider,
        policy=policy,
        config=ExecutionConfig(enabled=True, dry_run=dry_run),
        clock=lambda: FIXED_NOW,
    )


def _starved_portfolio(context, proposal, scope_quantity: Decimal):
    """A portfolio whose buying power affords strictly fewer units than the authorized scope."""
    price = dec(context.market_snapshot.quotes[proposal.symbol].price)
    affordable_units = max(scope_quantity - 1, Decimal(0))
    return make_portfolio_snapshot(buying_power=dstr(price * affordable_units))


def test_toctou_revalidation_occurs():
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation, decision, auth = _issue_chain(policy, context, proposal)
    scope_quantity = quantity_of(auth.scope)

    # state degrades between authorization and execution
    broker = RecordingBroker(
        portfolio_snapshot=_starved_portfolio(context, proposal, scope_quantity),
        market_snapshot=context.market_snapshot,
    )
    provider = ScriptedContextProvider(broker)
    result = _gate(policy, broker, provider, dry_run=False).execute(auth, proposal, decision)

    assert result.status == "BLOCKED", (result.status, codes(result), result.message)
    assert "REAUTHORIZATION_REQUIRED" in codes(result), codes(result)
    assert "TOCTOU_STATE_CHANGED" in codes(result), codes(result)
    assert broker.submitted == []
    assert "broker.submit_order" not in broker.log
    assert result.broker_order_id is None
    assert result.fills == []
    assert result.revalidation.performed is True
    assert result.revalidation.supported is False
    assert result.revalidation.state_changed is True
    assert len(provider.built) == 1
    assert result.revalidation.fresh_context_id == provider.built[0].context_id
    assert result.revalidation.fresh_evaluation_id is not None
    assert result.revalidation.fresh_evaluation_id != evaluation.evaluation_id
    assert as_decimal(result.revalidation.fresh_recommended_quantity) < scope_quantity
    # E5: the authorization is not quietly shrunk to what fresh state supports
    assert quantity_of(auth.scope) == scope_quantity
    assert result.status not in {"SUBMITTED", "WOULD_SUBMIT", "RECONCILED_EXISTING"}

    # happy path: unchanged state -> re-validation still runs, and supports the scope
    evaluation2, decision2, auth2 = _issue_chain(policy, context, proposal)
    broker2 = RecordingBroker.from_context(context)
    provider2 = ScriptedContextProvider(broker2)
    result2 = _gate(policy, broker2, provider2, dry_run=True).execute(auth2, proposal, decision2)
    assert result2.status == "WOULD_SUBMIT", (result2.status, codes(result2), result2.message)
    assert result2.revalidation.performed is True
    assert result2.revalidation.supported is True
    assert len(provider2.built) == 1
    assert result2.revalidation.fresh_context_id == provider2.built[0].context_id
    assert result2.revalidation.fresh_evaluation_id is not None
    assert as_decimal(result2.revalidation.fresh_recommended_quantity) >= quantity_of(auth2.scope)
    assert broker2.submitted == []  # dry run never mutates
    assert result2.broker_order_id is None


def test_fresh_rejection_requires_reauthorization_not_a_smaller_order():
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    _evaluation, decision, auth = _issue_chain(policy, context, proposal)
    broker = RecordingBroker(
        portfolio_snapshot=make_portfolio_snapshot(buying_power="0"), market_snapshot=context.market_snapshot
    )
    result = _gate(policy, broker, ScriptedContextProvider(broker), dry_run=False).execute(
        auth, proposal, decision
    )
    assert result.status == "BLOCKED"
    assert "REAUTHORIZATION_REQUIRED" in codes(result), codes(result)
    assert result.revalidation.performed is True and result.revalidation.supported is False
    assert as_decimal(result.revalidation.fresh_recommended_quantity) < quantity_of(auth.scope)
    assert broker.submitted == []


def test_response_level_escalation_requires_reauthorization():
    policy = make_policy()
    context = context_for(policy)
    assert context.response_level == 0
    proposal = make_proposal()
    _evaluation, decision, auth = _issue_chain(policy, context, proposal)
    assert auth.bound_state.response_level == context.response_level
    assert auth.bound_state.policy_hash == policy.policy_hash

    broker = RecordingBroker.from_context(context)  # portfolio unchanged - only the level moved
    provider = ScriptedContextProvider(broker, context_overrides={"response_level": 2})
    result = _gate(policy, broker, provider, dry_run=False).execute(auth, proposal, decision)
    assert provider.built and provider.built[0].response_level == 2
    assert result.status == "BLOCKED", (result.status, codes(result), result.message)
    assert "REAUTHORIZATION_REQUIRED" in codes(result), codes(result)
    assert "RESPONSE_LEVEL_ESCALATED" in codes(result), codes(result)
    assert result.revalidation.performed is True
    assert result.revalidation.response_level_at_execution == 2
    assert broker.submitted == []
    assert result.broker_order_id is None
