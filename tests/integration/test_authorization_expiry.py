"""An authorization that ran out of time, from both sides of the gate.

E6: an authorization is short-lived and is re-validated immediately before the mutation. Two windows
have to be closed and they are not the same window:

* the TTL expires **before** the gate is entered — refused at check 2;
* the TTL expires **during** the gate's own work — checks 3 to 5 take time, and the second freshness
  check at step 6 is the only thing standing between a stale permission and a live order. That one is
  driven here by advancing the clock from inside a broker call, which is the real shape of the race.
"""

from __future__ import annotations

import pytest

from mizan.contracts.errors import AuthorizationError
from tests.integration._world import Clock, build_world, proposal


def test_an_authorization_that_expired_before_the_gate_is_refused(tmp_path):
    clock = Clock()
    world = build_world(ledger_dir=tmp_path, clock=clock, ttl_seconds=10)
    record = world.mizan.evaluate(proposal("10"))
    assert record.authorization is not None
    assert record.authorization.ttl_seconds == 10

    clock.advance(11)

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert world.codes(result) == ["AUTHORIZATION_EXPIRED"], world.codes(result)
    assert result.revalidation.performed is False, "an expired authorization is refused before the re-check"
    assert world.broker.submitted == []
    assert "broker.submit_order" not in world.log
    assert "broker.find_order" not in world.log, "check 2 refuses before the idempotency read"


def test_an_authorization_that_expires_inside_the_gate_is_still_refused(tmp_path):
    """The window E6 exists for: valid on entry, stale by the time the mutation is due.

    The clock is advanced from the broker's own ``find_order`` call — step 3 of CHECK_ORDER — so the
    TTL runs out after the entry validation has already passed. Only the second freshness check
    (step 6, immediately before the kill-switch read) can catch this.
    """
    clock = Clock()
    world = build_world(
        ledger_dir=tmp_path,
        clock=clock,
        ttl_seconds=10,
        hooks={"on_find_order": lambda: clock.advance(30)},
    )
    record = world.mizan.evaluate(proposal("10"))

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert world.codes(result) == ["AUTHORIZATION_EXPIRED"], world.codes(result)
    assert result.revalidation.performed is True, "the gate had already got past entry validation"
    assert "broker.find_order" in world.log, "the race was entered"
    assert "kill_switch.is_active" not in world.log, "it never reached the mutation boundary"
    assert world.broker.submitted == []


def test_an_expired_authorization_is_dead_for_good_not_merely_this_attempt(tmp_path):
    """Time does not run backwards for anyone but a test, and the refusal must not be re-triable."""
    clock = Clock()
    world = build_world(ledger_dir=tmp_path, clock=clock, ttl_seconds=10)
    record = world.mizan.evaluate(proposal("10"))
    clock.advance(11)

    assert world.mizan.execute(record.decision_id).status == "BLOCKED"
    assert world.mizan.execute(record.decision_id).status == "BLOCKED"
    assert world.broker.submitted == []

    # and the mint itself agrees the permission is gone
    with pytest.raises(AuthorizationError):
        from mizan import authorization as auth_module

        auth_module.validate(record.authorization, now=clock())


def test_a_fresh_authorization_for_the_same_proposal_executes_normally(tmp_path):
    """The remedy for an expiry is a new decision, and it works: the refusal is not a dead end."""
    clock = Clock()
    world = build_world(ledger_dir=tmp_path, clock=clock, ttl_seconds=10)
    stale = world.mizan.evaluate(proposal("10"))
    clock.advance(11)
    assert world.mizan.execute(stale.decision_id).status == "BLOCKED"

    fresh = world.mizan.evaluate(proposal("10"))
    result = world.mizan.execute(fresh.decision_id)

    assert result.status == "SUBMITTED", (result.status, world.codes(result))
    assert len(world.broker.submitted) == 1
    assert fresh.sequence == 2 and stale.sequence == 1
    assert world.mizan.verify_chain().ok is True
