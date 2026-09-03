"""The state moved between the decision and the order. E9 (re-check) and E5 (never resize).

The attack this defends against is not exotic: an authorization is a permission granted against a
particular account state, and if the state degrades before the order is placed, the permission is for
a world that no longer exists. Mizan re-derives the whole context from the broker at execution time
and re-runs the engine against it.

E5 is the half that is easy to get wrong in the helpful direction. When fresh risk supports 12 units
and the authorization says 20, the tempting move is to submit 12. Mizan refuses instead: a quietly
resized order is one nobody authorized, and the agent must come back through the decision plane.

The race is driven from inside the broker's own calls, so the state changes while the gate is
mid-read rather than tidily between two of the test's statements.
"""

from __future__ import annotations

from decimal import Decimal

from tests.integration._world import build_world, portfolio_snapshot, proposal, starved_portfolio


def test_buying_power_that_collapses_mid_gate_forces_reauthorization(tmp_path):
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    authorized = Decimal(record.authorization.scope.total_quantity)
    assert authorized == 10

    # the account is drained at the exact instant the gate reaches its idempotency read
    world.broker.on_find_order = lambda: world.broker.set_portfolio_snapshot(starved_portfolio("1000"))

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert "REAUTHORIZATION_REQUIRED" in world.codes(result)
    assert "TOCTOU_STATE_CHANGED" in world.codes(result)
    assert result.revalidation.performed is True
    assert result.revalidation.supported is False
    assert result.revalidation.state_changed is True
    assert result.revalidation.fresh_evaluation_id is not None
    assert result.revalidation.fresh_evaluation_id != record.risk_evaluation.evaluation_id
    assert Decimal(result.revalidation.fresh_recommended_quantity) < authorized

    # E5: nothing was submitted, and nothing was submitted SMALLER
    assert world.broker.submitted == []
    assert "broker.submit_order" not in world.log
    assert record.authorization.scope.total_quantity == "10", "the authorization was not rewritten"


def test_a_fresh_rejection_is_a_refusal_not_a_smaller_order(tmp_path):
    """Buying power gone entirely: the fresh evaluation REJECTs and the gate does not haggle."""
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    world.broker.set_portfolio_snapshot(starved_portfolio("0"))

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED"
    assert "REAUTHORIZATION_REQUIRED" in world.codes(result)
    assert Decimal(result.revalidation.fresh_recommended_quantity) == 0
    assert world.broker.submitted == []


def test_a_response_level_escalation_between_decision_and_execution_blocks(tmp_path):
    """Addendum 1: the graduated-response level is bound into the authorization.

    The level is a context input, so it is raised here the way a deployment would raise it — through
    the context provider the pipeline already holds — and the gate must notice that the world it was
    authorized in is stricter now.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    assert record.authorization.bound_state.response_level == 0

    world.mizan.context_provider.response_level = 2

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert "REAUTHORIZATION_REQUIRED" in world.codes(result)
    assert "RESPONSE_LEVEL_ESCALATED" in world.codes(result)
    assert result.revalidation.response_level_at_execution == 2
    assert world.broker.submitted == []


def test_a_policy_rebind_between_decision_and_execution_blocks(tmp_path):
    """The authorization is bound to a policy HASH, so an edited policy invalidates it."""
    from tests.integration._world import policy_for

    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))

    world.mizan.policy = policy_for(world.mizan.tenant_id, ttl_seconds=20)
    assert world.mizan.policy.policy_hash != record.authorization.bound_state.policy_hash

    result = world.mizan.execute(record.decision_id)

    assert result.status == "BLOCKED", (result.status, world.codes(result))
    assert "REAUTHORIZATION_REQUIRED" in world.codes(result)
    assert "STATE_BINDING_MISMATCH" in world.codes(result)
    assert world.broker.submitted == []


def test_a_state_change_that_still_supports_the_order_is_recorded_but_not_blocked(tmp_path):
    """The other half of the rule, and the one worth stating out loud.

    A changed snapshot that still supports the authorized size does NOT block; it executes, and the
    change is recorded on the result as ``state_changed``. That is a deliberate design position -
    blocking on any movement at all would make the gate unusable in a live market - so it is pinned
    here rather than left to be discovered. An auditor reading the result can still see that the
    world moved.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))

    # same buying power, different snapshot identity and a slightly different cash balance
    world.broker.set_portfolio_snapshot(
        portfolio_snapshot(snapshot_id="pf-integration-moved", cash="79000")
    )

    result = world.mizan.execute(record.decision_id)

    assert result.status == "SUBMITTED", (result.status, world.codes(result))
    assert result.revalidation.performed is True
    assert result.revalidation.supported is True
    assert result.revalidation.state_changed is True, "the movement must still be visible in the record"
    assert len(world.broker.submitted) == 1
