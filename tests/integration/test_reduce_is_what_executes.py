"""A REDUCE, pinned at the only place it matters: the size that actually reaches the broker.

L3b found and fixed a defect in which ``@mizan.protected`` handed the developer's own submit function
the ORIGINAL proposal after the governor had cut it (ledger/escalations.md, 2026-09-03T05:45:00Z).
The reduction was reported and not applied: a governor that authorizes 20 of 30 while 30 goes to the
venue is not a governor. This module pins that from the outside, through both routes to a broker:

* ``Mizan.execute`` — the gate submits from the AUTHORIZATION's scope, never the proposal;
* ``@mizan.protected`` — the caller's own function is handed the authorized proposal.

A reduction that is only reported is a governance failure that every unit test can miss, because
every object in sight says 20 and only the wire says 30.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mizan.contracts.errors import ConfigurationError, ExecutionBlocked
from mizan.sdk import authorized_proposal
from tests.integration._world import build_world, proposal


def test_the_governor_reduces_and_the_reduced_size_is_what_the_broker_receives(tmp_path):
    world = build_world(ledger_dir=tmp_path)
    asked = proposal("30")  # the policy's max_quantity is 20, configured as a warning

    record = world.mizan.evaluate(asked)

    assert record.verdict == "REDUCE", (record.verdict, world.codes(record))
    assert world.codes(record) == ["POSITION_LIMIT_EXCEEDED"]
    assert Decimal(record.original.total_quantity) == 30
    assert Decimal(record.authorized.total_quantity) == 20
    assert record.authorization is not None
    assert record.authorization.scope.total_quantity == "20"
    assert [leg.quantity for leg in record.authorization.scope.legs] == ["20"]

    result = world.mizan.execute(record.decision_id)
    assert result.status == "SUBMITTED", (result.status, world.codes(result))

    # THE assertion: what left the building.
    assert len(world.broker.submitted) == 1
    order = world.broker.submitted[0]
    assert [(leg.leg_index, leg.quantity) for leg in order.legs] == [(0, "20")], (
        "the gate submitted the proposed size, not the authorized one"
    )
    assert sum(Decimal(leg.quantity) for leg in order.legs) == 20
    assert Decimal(record.original.total_quantity) == 30, "the record still shows what was asked for"


def test_protected_hands_the_caller_the_authorized_proposal_not_the_proposed_one(tmp_path):
    """The developer's ten lines: whatever they do with the object, it is already the allowed size."""
    world = build_world(ledger_dir=tmp_path, dry_run=True)
    seen: list[object] = []
    decisions: list[object] = []

    @world.mizan.protected(on_decision=decisions.append)
    def submit_trade(order):
        seen.append(order)
        world.broker.submit_order  # noqa: B018 - naming the sink without calling it
        return order.total_quantity

    asked = proposal("30")
    returned = submit_trade(asked)

    assert len(seen) == 1
    handed = seen[0]
    assert Decimal(handed.total_quantity) == 20, "the wrapped function was handed the un-reduced order"
    assert Decimal(asked.total_quantity) == 30, "the caller's own object is not mutated behind its back"
    assert Decimal(returned) == 20
    assert handed.proposal_id != asked.proposal_id, "a resized order is a different order"
    assert handed.symbol == asked.symbol and handed.intent == asked.intent
    assert len(decisions) == 1 and decisions[0].verdict == "REDUCE"

    # and the reduction is reproducible from the record alone
    assert authorized_proposal(decisions[0]).proposal_id == handed.proposal_id


def test_protected_stops_the_second_submission_but_only_after_the_first_one_happened(tmp_path):
    """dry_run=False plus a caller that submits is two orders for one authorization. Only one is stopped.

    The caller's function never runs, so the SECOND order is genuinely prevented and the reduction is
    not the thing at risk. But the refusal comes after ``Mizan.execute`` has already returned
    ``SUBMITTED``, so by the time the ``ConfigurationError`` is raised a real order is at the venue -
    and the developer, who sees only an exception, has no reason to think so. The condition is
    decidable at decoration time. This is L5's F-33 and this test corroborates it rather than
    asserting the friendlier claim its name would otherwise make.
    """
    world = build_world(ledger_dir=tmp_path, dry_run=False)
    calls: list[object] = []

    @world.mizan.protected
    def submit_trade(order):
        calls.append(order)

    with pytest.raises(ConfigurationError):
        submit_trade(proposal("30"))

    assert calls == [], "the caller's submit must never run after the gate already submitted"
    assert len(world.broker.submitted) == 1, (
        "F-33 may be fixed - @protected now refuses before executing; update this test"
    )
    assert [leg.quantity for leg in world.broker.submitted[0].legs] == ["20"], (
        "the order that did go out was at least the authorized size"
    )


def test_protected_never_calls_the_function_on_a_rejection(tmp_path):
    world = build_world(ledger_dir=tmp_path, dry_run=True, strict=True)
    calls: list[object] = []

    @world.mizan.protected
    def submit_trade(order):
        calls.append(order)

    with pytest.raises(ExecutionBlocked) as refusal:
        submit_trade(proposal("30"))

    assert calls == []
    assert world.broker.submitted == []
    assert "POSITION_LIMIT_EXCEEDED" in [
        str(getattr(code, "value", code)) for code in refusal.value.reason_codes
    ]


def test_the_same_proposal_reduces_or_rejects_purely_by_policy(tmp_path):
    """The refusal belongs to the policy, not to the proposal: one severity flip changes the verdict."""
    lenient = build_world(ledger_dir=tmp_path / "lenient")
    strict = build_world(ledger_dir=tmp_path / "strict", strict=True)
    asked = proposal("30")

    reduced = lenient.mizan.evaluate(asked)
    rejected = strict.mizan.evaluate(asked)

    assert reduced.verdict == "REDUCE" and rejected.verdict == "REJECT"
    assert reduced.proposal_id == rejected.proposal_id, "the same order, judged twice"
    assert reduced.policy.hash != rejected.policy.hash, "and by two different policy versions"
    assert Decimal(rejected.authorized.total_quantity) == 0
    assert rejected.authorization is None
