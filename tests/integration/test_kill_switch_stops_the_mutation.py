"""The kill switch, asserted as an ORDERING fact and not merely as an outcome.

Hard Rule E4 says the switch is read immediately before the broker mutation, and the reason is a
window: check it at request entry and the whole TOCTOU re-validation, the consume and the freshness
re-check all happen afterwards, during which an operator can throw the switch and an order still goes
out. "It blocked" does not test that. What tests it is *where in the call sequence the read happens*,
which is why ``_world`` gives the kill switch and the broker one shared log.

The hardest case is the last one here: the switch is thrown from inside the gate's own final broker
read. An implementation that cached the switch at entry passes every other test in this file.
"""

from __future__ import annotations

from tests.integration._world import build_world, proposal


def test_a_switch_thrown_between_decision_and_execution_stops_the_order(tmp_path):
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    assert record.verdict == "APPROVE"

    world.mizan.kill_switch.activate()
    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert world.codes(result) == ["KILL_SWITCH_ACTIVE"]
    assert result.kill_switch_checked_at is not None
    assert world.broker.submitted == []
    assert "broker.submit_order" not in world.log


def test_the_switch_is_read_after_the_last_broker_read_and_immediately_before_the_mutation(tmp_path):
    """E4 as a position in the call sequence. This is the assertion the rule is actually about."""
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    del world.log[:]  # only the gate's calls, not the decision plane's

    assert world.mizan.execute(record.decision_id).status == "SUBMITTED"

    assert world.log == [
        "broker.find_order",           # 3 idempotency
        "broker.get_portfolio_snapshot",  # 4 TOCTOU re-validation
        "broker.get_market_snapshot",
        "kill_switch.is_active",       # 7 the last check
        "broker.submit_order",         # 8 the mutation
    ], world.log
    assert world.log.count("kill_switch.is_active") == 1, "read exactly once, not cached and not polled"


def test_a_switch_thrown_during_the_gates_own_work_still_stops_the_order(tmp_path):
    """The window E4 exists to close: valid at entry, thrown while the gate is mid-flight.

    The switch is activated from inside the broker's market-data read, which the gate performs during
    TOCTOU re-validation — after entry, after idempotency, before the consume. A gate that read the
    switch at entry, or cached it, would submit here.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    world.broker.on_market_read = world.mizan.kill_switch.activate

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert world.codes(result) == ["KILL_SWITCH_ACTIVE"]
    assert "broker.get_market_snapshot" in world.log, "the gate really did get that far"
    assert "broker.submit_order" not in world.log
    assert world.broker.submitted == []


def test_the_switch_does_not_stop_evaluation_only_execution(tmp_path):
    """Governance keeps working with the switch on: decisions are still made, recorded and replayable.

    A kill switch that also silenced the decision plane would destroy the audit trail exactly when it
    is most wanted — during an incident.
    """
    world = build_world(ledger_dir=tmp_path, kill_switch_active=True)

    record = world.mizan.evaluate(proposal("10"))

    assert record.verdict == "APPROVE"
    assert record.authorization is not None
    assert world.mizan.verify_chain().ok is True
    assert world.mizan.replay(record.decision_id).identical is True
    assert world.mizan.execute(record.decision_id).status == "BLOCKED"
    assert world.broker.submitted == []


def test_the_switch_burns_the_authorization_it_stopped(tmp_path):
    """Documented consequence of CHECK_ORDER: the consume (5) precedes the switch read (7).

    Recorded as a test rather than left implicit, because it is a real operational fact — after a
    kill-switch block the authorization is spent and de-escalating does NOT let the held order
    through. The agent must come back through the whole decision plane. That is the safe direction,
    and it is pinned here so a future reordering of CHECK_ORDER cannot flip it silently.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    world.mizan.kill_switch.activate()
    assert world.mizan.execute(record.decision_id).status == "BLOCKED"

    world.mizan.kill_switch.deactivate()
    retried = world.mizan.execute(record.decision_id)

    assert retried.status == "BLOCKED", (retried.status, world.codes(retried))
    assert world.codes(retried) == ["AUTHORIZATION_ALREADY_USED"]
    assert world.broker.submitted == []
