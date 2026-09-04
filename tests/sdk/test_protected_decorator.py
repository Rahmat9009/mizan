"""``@mizan.protected`` — the ten-line integration, and the four ways it must refuse.

The decorator's promise is narrow and total: the wrapped function's body runs only behind a decision
that was approved, authorized, unexpired, unconsumed and not stopped by the kill switch, and it runs
with the **authorized** quantities rather than the proposed ones. Everything below is one of those
words.
"""

from __future__ import annotations

import pytest

from mizan.contracts.errors import MizanError
from mizan.execution import ExecutionConfig
from tests.fixtures import (
    FIXED_NOW,
    killer_demo_policy,
    killer_demo_reject_proposal,
    make_proposal,
)
from tests.sdk.conftest import FakeGate, StepClock, reducing_policy


def codes(error: MizanError) -> set[str]:
    return {str(code.value) for code in error.reason_codes}


def test_the_wrapped_function_runs_and_keeps_its_own_identity(pipeline, proposal):
    seen = []

    @pipeline.protected
    def submit_trade(order, note="n/a"):
        """Send it."""
        seen.append((order, note))
        return "submitted"

    assert submit_trade(proposal, note="hello") == "submitted"
    assert submit_trade.__name__ == "submit_trade"
    assert submit_trade.__doc__ == "Send it."
    assert len(seen) == 1
    assert seen[0][1] == "hello"


def test_the_function_receives_the_authorized_quantities_not_the_proposed_ones(build_pipeline):
    """A reduction that is only reported is not a reduction.

    The agent asked for 10; the policy allows 4. What reaches the caller's submit function must be an
    order for 4, because the most likely thing that function does is hand it straight to a broker.
    """
    proposal = make_proposal()
    pipeline = build_pipeline(policy=reducing_policy(max_quantity="4"))
    delivered = []

    @pipeline.protected
    def submit_trade(order):
        delivered.append(order)

    submit_trade(proposal)

    assert [leg.quantity for leg in proposal.legs] == ["10"]
    assert [leg.quantity for leg in delivered[0].legs] == ["4"]
    assert delivered[0].total_quantity < proposal.total_quantity
    record = pipeline.list_decisions()[0]
    assert record.verdict == "REDUCE"
    assert record.authorized.total_quantity == "4"


def test_an_approved_order_is_handed_through_unchanged(pipeline, proposal):
    delivered = []

    @pipeline.protected
    def submit_trade(order):
        delivered.append(order)

    submit_trade(proposal)

    assert pipeline.list_decisions()[0].verdict == "APPROVE"
    assert delivered[0].proposal_id == proposal.proposal_id


def test_a_rejected_proposal_never_reaches_the_function(build_pipeline):
    """The one thing a decorator must never do is let a blocked order fall through as a quiet None."""
    proposal = killer_demo_reject_proposal()
    pipeline = build_pipeline(policy=killer_demo_policy(), agent=proposal.agent)
    ran = []

    @pipeline.protected
    def submit_trade(order):
        ran.append(order)

    with pytest.raises(MizanError) as blocked:
        submit_trade(proposal)

    assert ran == []
    assert blocked.value.reason_codes, "a refusal must say why"
    assert pipeline.list_decisions()[0].verdict == "REJECT"


def test_the_kill_switch_stops_the_function(build_pipeline, proposal):
    from mizan.execution import InMemoryKillSwitch

    switch = InMemoryKillSwitch()
    pipeline = build_pipeline(kill_switch=switch)
    ran = []

    @pipeline.protected
    def submit_trade(order):
        ran.append(order)

    switch.activate()
    with pytest.raises(MizanError) as blocked:
        submit_trade(proposal)

    assert ran == []
    assert "KILL_SWITCH_ACTIVE" in codes(blocked.value)


def test_an_expired_authorization_stops_the_function(build_pipeline, proposal, much_later):
    """E6: the decision was sound when it was made and is not sound now. Time is the only input moved."""
    pipeline = build_pipeline(clock=StepClock(FIXED_NOW, much_later))
    ran = []

    @pipeline.protected
    def submit_trade(order):
        ran.append(order)

    with pytest.raises(MizanError) as blocked:
        submit_trade(proposal)

    assert ran == []
    assert "AUTHORIZATION_EXPIRED" in codes(blocked.value)


def test_an_authorization_is_single_use(build_pipeline, proposal):
    """The same decision cannot be replayed into a second order."""
    pipeline = build_pipeline()
    record = pipeline.evaluate(proposal)

    first = pipeline.execute(record.decision_id)
    second = pipeline.execute(record.decision_id)

    assert first.status == "WOULD_SUBMIT"
    assert second.status == "BLOCKED"
    assert "AUTHORIZATION_ALREADY_USED" in {str(code.value) for code in second.reason_codes}


def test_execution_disabled_stops_the_function(build_pipeline, proposal):
    pipeline = build_pipeline(config=ExecutionConfig(enabled=False, dry_run=True))
    ran = []

    @pipeline.protected
    def submit_trade(order):
        ran.append(order)

    with pytest.raises(MizanError) as blocked:
        submit_trade(proposal)

    assert ran == []
    assert "EXECUTION_DISABLED" in codes(blocked.value)


def test_protected_refuses_a_configuration_that_would_double_submit(build_pipeline, proposal):
    """``dry_run=False`` means the gate submits through Mizan's broker.

    Calling the caller's submit function as well would place a second, unauthorized order for the
    same decision. The combination is refused rather than documented away - and refused at DECORATION,
    before a proposal exists, because it is decidable from configuration alone. It used to be refused
    after the gate had already placed the first of the two orders (F-33).
    """
    pipeline = build_pipeline(config=ExecutionConfig(enabled=True, dry_run=False))
    ran = []

    with pytest.raises(MizanError) as refused:

        @pipeline.protected
        def submit_trade(order):  # pragma: no cover - decoration raises first
            ran.append(order)

    assert ran == []
    assert pipeline.broker.submitted == [], (
        "the refusal is decidable from configuration alone, so nothing reaches the broker"
    )
    assert refused.value.http_status == 500
    assert "double" in refused.value.message.lower() or "dry" in refused.value.message.lower()


def test_on_decision_sees_every_verdict(build_pipeline):
    """The record id reaches the caller's logs without changing the caller's function signature."""
    proposal = killer_demo_reject_proposal()
    pipeline = build_pipeline(policy=killer_demo_policy(), agent=proposal.agent)
    seen = []

    @pipeline.protected(on_decision=seen.append)
    def submit_trade(order):  # pragma: no cover - the rejection prevents this from running
        raise AssertionError("must not run")

    with pytest.raises(MizanError):
        submit_trade(proposal)

    assert [record.verdict for record in seen] == ["REJECT"]
    assert seen[0].decision_id == pipeline.list_decisions()[0].decision_id


def test_the_sdk_hands_the_gate_exactly_the_recorded_decision(build_pipeline, proposal, monkeypatch):
    """What ``Mizan.execute`` delegates, asserted against a double rather than assumed.

    The gate is substituted here because the real one belongs to another lane and was being written
    while this suite was: a test that waits on a neighbour is a test that does not run. What is pinned
    is the SDK's half of the contract — which collaborators it wires in, and that the three arguments
    come from the *ledger record*, never from the caller.
    """
    import mizan.sdk as sdk_module

    built: dict[str, object] = {}
    gate = FakeGate(status="WOULD_SUBMIT")

    def fake_gate(**kwargs):
        built.update(kwargs)
        return gate

    monkeypatch.setattr(sdk_module, "ExecutionGate", fake_gate)
    pipeline = build_pipeline()
    record = pipeline.evaluate(proposal)

    result = pipeline.execute(record.decision_id)

    assert result.status == "WOULD_SUBMIT"
    assert built["broker"] is pipeline.broker
    assert built["kill_switch"] is pipeline.kill_switch
    assert built["registry"] is pipeline.registry
    assert built["config"] is pipeline.config
    assert built["policy"].policy_hash == record.policy_snapshot.policy_hash
    auth, sent_proposal, decision = gate.calls[0]
    assert auth.auth_id == record.authorization.auth_id
    assert sent_proposal.proposal_id == record.proposal_id
    assert decision.decision_id == record.decision_id


def test_execute_refuses_a_decision_that_was_never_authorized(build_pipeline, monkeypatch):
    """A REJECT carries no authorization, and nothing may manufacture one for it."""
    import mizan.sdk as sdk_module

    def refuse(**_kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("the gate must not be built for an unauthorized decision")

    proposal = killer_demo_reject_proposal()
    pipeline = build_pipeline(policy=killer_demo_policy(), agent=proposal.agent)
    record = pipeline.evaluate(proposal)
    monkeypatch.setattr(sdk_module, "ExecutionGate", refuse)

    with pytest.raises(MizanError) as refused:
        pipeline.execute(record.decision_id)

    assert refused.value.http_status >= 400
