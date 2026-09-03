"""Invariant 21 - Dispatch Addendum section 2 / L0 scope: an authorization is bound to the STATE it was issued against.

Pass criterion: (a) every issued authorization carries a bound_state whose hashes are the real derived hashes of
the context it was issued against - not placeholders, not nulls where the state existed; (b) those hashes cover
policy, portfolio, market, response level, path state and aggregate state; (c) an authorization whose bound_state
has been tampered with is refused; (d) the gate re-derives the binding from FRESH state rather than trusting the
authorization's own copy.

"An authorization is valid for one action, one policy, one account, one amount, until one timestamp, against one
state." Without (a) the last clause is decorative.
"""
from __future__ import annotations

import pytest

from mizan import authorization, governor, risk
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts.canonical import object_hash
from mizan.contracts.errors import AuthorizationError
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW, make_proposal
from tests.invariants._support import (
    RecordingBroker,
    ScriptedContextProvider,
    path_and_aggregate_policy,
    unstressed_context,
)


def _issue(policy, context, proposal):
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    return decision, authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)


def test_authorization_bound_to_state_hash():
    policy = path_and_aggregate_policy()
    context = unstressed_context(policy)
    assert context.path_state is not None and context.aggregate_state is not None, (
        "this invariant needs a context carrying path and aggregate state"
    )
    _, auth = _issue(policy, context, make_proposal())
    bound = auth.bound_state

    # (a) + (b) every binding is the REAL derived hash of what it claims to bind
    assert bound.policy_hash == policy.policy_hash
    assert bound.portfolio_snapshot_id == context.portfolio_snapshot.snapshot_id
    assert bound.portfolio_state_hash == object_hash(context.portfolio_snapshot)
    assert bound.market_snapshot_id == context.market_snapshot.snapshot_id
    assert bound.response_level == context.response_level
    assert bound.path_state_hash == object_hash(context.path_state), (
        "path state was present but is not bound; a deepening drawdown could then be ignored at execution"
    )
    assert bound.aggregate_state_hash == object_hash(context.aggregate_state), (
        "aggregate state was present but is not bound; the book could fill up between decision and execution"
    )


def test_a_tampered_binding_is_refused():
    """(c) Editing the binding must not produce a usable authorization."""
    policy = path_and_aggregate_policy()
    context = unstressed_context(policy)
    decision, auth = _issue(policy, context, make_proposal())
    forged = auth.model_copy(
        update={"bound_state": auth.bound_state.model_copy(update={"portfolio_state_hash": "0" * 64})},
        deep=True,
    )
    with pytest.raises(AuthorizationError):
        authorization.validate(forged, now=FIXED_NOW, decision=decision, proposal=make_proposal())


def test_the_gate_rederives_the_binding_from_fresh_state():
    """(d) The gate must compare against state it fetched itself, never the authorization's own copy."""
    policy = path_and_aggregate_policy()
    context = unstressed_context(policy)
    proposal = make_proposal()
    decision, auth = _issue(policy, context, proposal)

    broker = RecordingBroker(
        portfolio_snapshot=context.portfolio_snapshot, market_snapshot=context.market_snapshot
    )
    provider = ScriptedContextProvider(
        broker,
        context_overrides={"path_state": context.path_state, "aggregate_state": context.aggregate_state},
    )
    gate = ExecutionGate(
        broker=broker,
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=provider,
        policy=policy,
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )
    result = gate.execute(auth, proposal, decision)
    assert result.revalidation.performed is True, (
        "the gate must re-derive state on every execution; a gate trusting auth.bound_state would have "
        f"nothing to re-derive and TOCTOU would be undefended. Result was {result.status}: {result.message}"
    )
    assert provider.built, "the gate did not fetch fresh state at all"
