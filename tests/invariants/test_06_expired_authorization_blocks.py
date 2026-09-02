"""Invariant 06 - Hard Rule E6: authorization expires (5-30 s) and is re-validated immediately before submission.

Pass criterion: an ExecutionAuthorization issued at FIXED_NOW is rejected by mizan.authorization.validate at
FIXED_NOW + ttl + 1 s with AUTHORIZATION_EXPIRED, and an ExecutionGate whose clock reads that time returns BLOCKED
with AUTHORIZATION_EXPIRED without touching the broker (no submission, no broker order id, no re-validation reached).
An authorization that becomes stale between the TOCTOU re-validation and the submission is still blocked (the
second validate in section 3.8 step 6), and the contract bounds ttl_seconds to 5..30.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from mizan import authorization
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts.errors import AuthorizationError, MizanError
from mizan.contracts.types import parse_ts
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW, make_authorization, make_policy
from tests.invariants._support import (
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


def _gate(policy, broker, provider, *, clock, dry_run=False):
    return ExecutionGate(
        broker=broker,
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=provider,
        policy=policy,
        config=ExecutionConfig(enabled=True, dry_run=dry_run),
        clock=clock,
    )


def test_expired_authorization_blocks():
    policy, proposal, decision, context, auth = _issued()
    ttl = auth.ttl_seconds
    assert 5 <= ttl <= 30
    assert parse_ts(auth.expires_at) - parse_ts(auth.issued_at) == timedelta(seconds=ttl)
    assert parse_ts(auth.issued_at) == FIXED_NOW

    expired_now = FIXED_NOW + timedelta(seconds=ttl + 1)
    with pytest.raises(AuthorizationError) as excinfo:
        authorization.validate(auth, now=expired_now)
    assert "AUTHORIZATION_EXPIRED" in codes(excinfo.value), codes(excinfo.value)

    broker = RecordingBroker.from_context(context)
    gate = _gate(policy, broker, ScriptedContextProvider(broker), clock=lambda: expired_now)
    result = gate.execute(auth, proposal, decision)
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in codes(result), codes(result)
    assert broker.submitted == []
    assert "broker.submit_order" not in broker.log
    assert result.broker_order_id is None
    assert result.submitted_at is None
    # blocked at section 3.8 step 2: the TOCTOU re-validation (step 4) is never reached
    assert result.revalidation.performed is False


def test_authorization_window_is_exactly_the_ttl():
    _policy, _proposal, decision, _context, auth = _issued()
    ttl = auth.ttl_seconds
    authorization.validate(auth, now=FIXED_NOW)
    authorization.validate(auth, now=FIXED_NOW + timedelta(seconds=ttl - 1))
    with pytest.raises(AuthorizationError) as too_early:
        authorization.validate(auth, now=FIXED_NOW - timedelta(seconds=1))
    assert "AUTHORIZATION_NOT_YET_VALID" in codes(too_early.value), codes(too_early.value)
    with pytest.raises(AuthorizationError) as too_late:
        authorization.validate(auth, now=FIXED_NOW + timedelta(seconds=ttl + 1))
    assert "AUTHORIZATION_EXPIRED" in codes(too_late.value)


def test_fixture_authorization_expires_too():
    auth = make_authorization()
    with pytest.raises(AuthorizationError) as excinfo:
        authorization.validate(auth, now=parse_ts(auth.expires_at) + timedelta(seconds=1))
    assert "AUTHORIZATION_EXPIRED" in codes(excinfo.value)


def test_ttl_is_policy_bounded_between_5_and_30_seconds():
    for bad in (0, 4, 31, 3600):
        with pytest.raises((ValidationError, MizanError)):
            make_policy(authorization={"ttl_seconds": bad})
    assert make_policy(authorization={"ttl_seconds": 5}).authorization.ttl_seconds == 5
    assert make_policy(authorization={"ttl_seconds": 30}).authorization.ttl_seconds == 30


def test_authorization_going_stale_after_revalidation_still_blocks():
    """E6: the gate validates again immediately before submission (step 6), not only at entry (step 2)."""
    policy, proposal, decision, context, auth = _issued()
    ttl = auth.ttl_seconds
    phase = {"stale": False}

    def clock():
        return FIXED_NOW + timedelta(seconds=ttl + 1) if phase["stale"] else FIXED_NOW

    def go_stale(_fresh_context):
        phase["stale"] = True

    broker = RecordingBroker.from_context(context)
    provider = ScriptedContextProvider(broker, on_build=go_stale)
    gate = _gate(policy, broker, provider, clock=clock)
    result = gate.execute(auth, proposal, decision)
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in codes(result), codes(result)
    assert result.revalidation.performed is True
    assert broker.submitted == []
    assert result.broker_order_id is None
