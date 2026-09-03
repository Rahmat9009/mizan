"""A REJECT is recorded, and no route from it reaches a venue.

The point of a governance layer is not that it says no; it is that saying no is the end of the story.
Three routes to the broker exist — ``Mizan.execute``, ``@mizan.protected`` and the gate itself — and
a rejected decision must be stopped on all three, while still producing a full, chained, replayable
record of the refusal.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mizan.contracts.errors import AuthorizationError, ExecutionBlocked
from mizan.execution import ExecutionGate
from tests.integration._world import build_world, proposal


def test_a_rejected_proposal_is_recorded_and_nothing_reaches_the_venue(tmp_path):
    world = build_world(ledger_dir=tmp_path, strict=True)

    record = world.mizan.evaluate(proposal("30"))

    assert record.verdict == "REJECT", (record.verdict, world.codes(record))
    assert "POSITION_LIMIT_EXCEEDED" in world.codes(record)
    assert Decimal(record.authorized.total_quantity) == 0
    assert record.authorization is None, "there is no authorization to execute a rejection with"

    # the refusal is nevertheless evidence: chained, hashed, replayable
    assert record.sequence == 1
    assert world.mizan.verify_chain().ok is True
    assert world.mizan.replay(record.decision_id).identical is True

    with pytest.raises(ExecutionBlocked) as refusal:
        world.mizan.execute(record.decision_id)
    assert "AUTHORIZATION_INVALID" in [
        str(getattr(code, "value", code)) for code in refusal.value.reason_codes
    ]

    assert world.broker.submitted == []
    assert "broker.submit_order" not in world.log
    assert "broker.find_order" not in world.log, "the gate was never entered at all"


def test_a_rejected_decision_cannot_be_minted_into_an_authorization(tmp_path):
    """The mint refuses a REJECT outright, so no caller can assemble the missing piece by hand."""
    from mizan import authorization as auth_module

    world = build_world(ledger_dir=tmp_path, strict=True)
    record = world.mizan.evaluate(proposal("30"))

    with pytest.raises(AuthorizationError):
        auth_module.issue(
            record.governor_decision,
            record.proposal,
            world.mizan.policy,
            now=world.clock(),
            context=record.risk_context,
        )


def test_a_restricted_symbol_is_refused_before_any_size_question(tmp_path):
    """A second, independent rejection path: the policy's restricted list."""
    world = build_world(ledger_dir=tmp_path)

    record = world.mizan.evaluate(proposal("5", symbol="GME"))

    assert record.verdict == "REJECT", (record.verdict, world.codes(record))
    assert "RESTRICTED_SYMBOL" in world.codes(record)
    assert record.authorization is None
    assert world.broker.submitted == []


def test_the_gate_has_no_entry_point_that_skips_the_checks(tmp_path):
    """E3: the gate is reached only through ``execute``, and ``execute`` runs CHECK_ORDER.

    Asserted structurally, not by exercise: there is exactly one ``submit_order`` call site in the
    execution package, and the gate exposes no public method that reaches it directly.
    """
    import inspect

    import mizan.execution as execution

    source = inspect.getsource(execution)
    assert source.count("self.broker.submit_order(") == 1, "more than one mutation site in the gate"

    public = {
        name
        for name, member in inspect.getmembers(ExecutionGate, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"execute"}, f"the gate grew a public method beside execute: {sorted(public)}"


def test_execution_disabled_is_the_first_refusal_and_touches_no_broker(tmp_path):
    """Check 1 of CHECK_ORDER. A deployment with execution off never reads the account either."""
    world = build_world(ledger_dir=tmp_path, enabled=False)
    record = world.mizan.evaluate(proposal("10"))
    reads_after_evaluate = len(world.log)

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED"
    assert world.codes(result) == ["EXECUTION_DISABLED"]
    assert result.revalidation.performed is False
    assert result.revalidation.supported is False, "unknown is never treated as supported"
    assert len(world.log) == reads_after_evaluate, "a disabled gate must not even call the broker"
    assert world.broker.submitted == []
