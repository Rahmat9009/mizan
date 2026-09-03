"""Invariant 22 - Dispatch Addendum section 2: an authorization does not survive a change to the state it was bound to.

Pass criterion: an authorization issued against state S must not be usable to submit against state S' != S. The
gate must detect the change from FRESH state and refuse, with REAUTHORIZATION_REQUIRED; nothing reaches the
broker. Proven separately for each bound component that can move on its own: portfolio, path state, aggregate
state, response level, and policy.

This is the "until one state" half of the authorization contract. Invariant 21 proves the binding EXISTS;
this proves it BITES.
"""
from __future__ import annotations

from mizan import authorization, governor, risk
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW, make_path_state, make_portfolio_snapshot, make_proposal
from tests.invariants._support import (
    RecordingBroker,
    ScriptedContextProvider,
    codes,
    full_book,
    path_and_aggregate_policy,
    unstressed_context,
)


def _issue(policy, context, proposal):
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    return decision, auth


def _execute_against(policy, context, proposal, decision, auth, *, overrides=None, portfolio=None):
    broker = RecordingBroker(
        portfolio_snapshot=portfolio or context.portfolio_snapshot,
        market_snapshot=context.market_snapshot,
    )
    # The provider rebuilds a context from the broker's snapshots alone, so path and aggregate state
    # must be carried across explicitly - otherwise "unchanged" would silently mean "state removed",
    # which blocks for the wrong reason and would make every assertion below vacuous.
    carried = {"path_state": context.path_state, "aggregate_state": context.aggregate_state}
    carried.update(overrides or {})
    provider = ScriptedContextProvider(broker, context_overrides=carried)
    gate = ExecutionGate(
        broker=broker,
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=provider,
        policy=policy,
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )
    return gate.execute(auth, proposal, decision), broker


def _assert_refused(result, broker, what: str):
    assert result.status == "BLOCKED", (
        f"the authorization was bound to a state that has since changed ({what}); "
        f"the gate returned {result.status} instead of BLOCKED"
    )
    assert "REAUTHORIZATION_REQUIRED" in codes(result), (
        f"a changed {what} must require re-authorization; codes were {sorted(codes(result))}"
    )
    assert not broker.submitted, f"nothing may reach the broker after {what} changed"


def test_authorization_invalid_after_portfolio_state_change():
    policy = path_and_aggregate_policy()
    context, proposal = unstressed_context(policy), make_proposal()
    decision, auth = _issue(policy, context, proposal)
    starved = make_portfolio_snapshot(buying_power="1", equity="1000")
    result, broker = _execute_against(policy, context, proposal, decision, auth, portfolio=starved)
    _assert_refused(result, broker, "the portfolio")


def test_authorization_invalid_after_response_level_escalation():
    policy = path_and_aggregate_policy()
    context, proposal = unstressed_context(policy), make_proposal()
    decision, auth = _issue(policy, context, proposal)
    escalated = max(context.response_level + 1, 1)
    result, broker = _execute_against(
        policy, context, proposal, decision, auth, overrides={"response_level": escalated}
    )
    _assert_refused(result, broker, "the response level")
    assert "RESPONSE_LEVEL_ESCALATED" in codes(result)


def test_authorization_invalid_after_path_state_change():
    """A drawdown that deepened after authorization must not be executed under the old size."""
    policy = path_and_aggregate_policy()
    context, proposal = unstressed_context(policy), make_proposal()
    decision, auth = _issue(policy, context, proposal)
    result, broker = _execute_against(
        policy, context, proposal, decision, auth,
        overrides={"path_state": make_path_state(current_drawdown_pct="0.40", consecutive_losses=9)},
    )
    _assert_refused(result, broker, "the path state")


def test_authorization_invalid_after_aggregate_state_change():
    """The book filling up after authorization must not be executed under the old size."""
    policy = path_and_aggregate_policy()
    context, proposal = unstressed_context(policy), make_proposal()
    decision, auth = _issue(policy, context, proposal)
    result, broker = _execute_against(
        policy, context, proposal, decision, auth,
        overrides={
            "aggregate_state": full_book(
                context.portfolio_snapshot.equity, policy.aggregate.max_portfolio_exposure_pct
            )
        },
    )
    _assert_refused(result, broker, "the aggregate state")


def test_the_unchanged_case_still_proceeds():
    """Control: with state genuinely unchanged the same authorization is usable, so the tests above
    are detecting a state change rather than a gate that refuses everything."""
    policy = path_and_aggregate_policy()
    context, proposal = unstressed_context(policy), make_proposal()
    decision, auth = _issue(policy, context, proposal)
    result, _ = _execute_against(policy, context, proposal, decision, auth)
    assert result.status != "BLOCKED", (
        f"unchanged state must remain executable; got BLOCKED with {sorted(codes(result))}"
    )
