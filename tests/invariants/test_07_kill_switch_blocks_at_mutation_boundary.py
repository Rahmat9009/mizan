"""Invariant 07 - Hard Rule E4: the kill switch is checked immediately before the mutation, not at request entry.

Pass criterion: with the kill switch inactive through every early check and flipped active by a hook that runs
after the last broker read of the TOCTOU re-validation (inside ContextProvider.build) and before the mutation,
ExecutionGate.execute returns BLOCKED with KILL_SWITCH_ACTIVE, revalidation.performed True,
kill_switch_checked_at stamped, and the broker records no submission - while the identical wiring without the
flip reaches SUBMITTED (so it was the switch that blocked). Ordering is pinned with an event log: the switch's
last consultation happens after the last broker/context read and immediately before submit_order, and a switch
that is already active at entry is still consulted at that boundary (revalidation still performed).
"""
from __future__ import annotations

from mizan import authorization
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW
from tests.invariants._support import (
    READ_EVENTS,
    EventKillSwitch,
    RecordingBroker,
    ScriptedContextProvider,
    codes,
    fixture_chain,
)


def _issued():
    proposal, policy, context, _evaluation, decision = fixture_chain()
    assert decision.verdict != "REJECT", "fixture decision must be authorizable"
    auth = authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    return policy, proposal, decision, context, auth


def _gate(policy, broker, provider, kill_switch, *, dry_run=False):
    return ExecutionGate(
        broker=broker,
        kill_switch=kill_switch,
        registry=InMemoryAuthorizationRegistry(),
        context_provider=provider,
        policy=policy,
        config=ExecutionConfig(enabled=True, dry_run=dry_run),
        clock=lambda: FIXED_NOW,
    )


def test_kill_switch_blocks_at_mutation_boundary():
    policy, proposal, decision, context, auth = _issued()
    switch = InMemoryKillSwitch()
    assert switch.is_active() is False
    log: list[str] = []
    broker = RecordingBroker.from_context(context, log=log)

    def flip_after_last_read(_fresh_context):
        switch.activate()

    provider = ScriptedContextProvider(broker, log=log, on_build=flip_after_last_read)
    result = _gate(policy, broker, provider, switch).execute(auth, proposal, decision)

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result), codes(result)
    assert broker.submitted == []
    assert "broker.submit_order" not in log
    assert result.broker_order_id is None
    assert result.submitted_at is None
    assert result.revalidation.performed is True
    assert result.kill_switch_checked_at is not None
    assert provider.built, "the TOCTOU re-validation must have built a fresh context before the switch was read"

    # control: identical wiring, switch never flipped -> the mutation happens
    policy2, proposal2, decision2, context2, auth2 = _issued()
    switch2 = InMemoryKillSwitch()
    broker2 = RecordingBroker.from_context(context2)
    result2 = _gate(policy2, broker2, ScriptedContextProvider(broker2), switch2).execute(
        auth2, proposal2, decision2
    )
    assert result2.status == "SUBMITTED", (result2.status, codes(result2), result2.message)
    assert len(broker2.submitted) == 1
    assert broker2.submitted[0].client_order_id == auth2.idempotency_key
    assert result2.broker_order_id is not None
    assert result2.kill_switch_checked_at is not None


def test_kill_switch_is_consulted_after_the_last_broker_read_and_right_before_submit():
    policy, proposal, decision, context, auth = _issued()
    log: list[str] = []
    switch = EventKillSwitch(active=False, log=log)
    broker = RecordingBroker.from_context(context, log=log)
    result = _gate(policy, broker, ScriptedContextProvider(broker, log=log), switch).execute(
        auth, proposal, decision
    )
    assert result.status == "SUBMITTED", (result.status, codes(result), result.message)
    assert switch.calls >= 1
    reads = [i for i, event in enumerate(log) if event in READ_EVENTS]
    checks = [i for i, event in enumerate(log) if event == "kill_switch"]
    assert reads and checks, log
    assert checks[-1] > reads[-1], f"kill switch must be read after the last broker read: {log}"
    assert log[-1] == "broker.submit_order" and log[-2] == "kill_switch", log


def test_kill_switch_active_at_entry_is_still_checked_at_the_boundary():
    policy, proposal, decision, context, auth = _issued()
    log: list[str] = []
    switch = EventKillSwitch(active=True, log=log)
    broker = RecordingBroker.from_context(context, log=log)
    provider = ScriptedContextProvider(broker, log=log)
    result = _gate(policy, broker, provider, switch).execute(auth, proposal, decision)
    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result), codes(result)
    assert broker.submitted == []
    assert result.broker_order_id is None
    assert result.kill_switch_checked_at is not None
    # E4: not a request-entry check - the TOCTOU re-validation still ran, and the switch was read after it
    assert result.revalidation.performed is True
    reads = [i for i, event in enumerate(log) if event in READ_EVENTS]
    checks = [i for i, event in enumerate(log) if event == "kill_switch"]
    assert reads and checks and checks[-1] > reads[-1], log


def test_kill_switch_blocks_dry_run_as_well():
    policy, proposal, decision, context, auth = _issued()
    switch = InMemoryKillSwitch()
    switch.activate()
    assert switch.is_active() is True
    broker = RecordingBroker.from_context(context)
    result = _gate(policy, broker, ScriptedContextProvider(broker), switch, dry_run=True).execute(
        auth, proposal, decision
    )
    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result)
    assert broker.submitted == []
