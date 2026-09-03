"""An APPROVE that goes all the way: proposal -> policy -> risk -> governor -> authorization ->
ledger -> gate -> broker -> decision replay -> chain verification.

This is the spine of the product in one test. Every seam is the shipped one; the broker is the only
stand-in and it is the shipped ``MockBroker`` adapter (see ``_world``). The ledger is a real SQLite
file, so the append-only triggers and the per-tenant file boundary are in the path.
"""

from __future__ import annotations

from decimal import Decimal

from mizan.audit import SqliteLedger
from mizan.contracts.canonical import canonical_json, record_hash_for
from tests.integration._world import TENANT_A, build_world, proposal


def test_an_approved_proposal_traverses_every_seam(tmp_path):
    world = build_world(ledger_dir=tmp_path)
    asked = proposal("10")

    record = world.mizan.evaluate(asked)

    # -- the decision plane ---------------------------------------------------------------------
    assert record.verdict == "APPROVE", (record.verdict, world.codes(record))
    assert record.reason_codes == [], world.codes(record)
    assert record.tenant_id == TENANT_A
    assert record.proposal_id == asked.proposal_id
    assert record.sequence == 1
    # the policy that judged it is the one the YAML document loaded, by hash
    assert record.policy.hash == world.mizan.policy.policy_hash
    assert record.policy_snapshot.policy_hash == world.mizan.policy.policy_hash
    # nothing was cut
    assert Decimal(record.authorized.total_quantity) == Decimal(record.original.total_quantity) == 10
    # every check the engine implements ran; none of them is a stub result
    assert len(record.checks) > 30
    assert {check.check_id for check in record.checks} == {check.check_id for check in record.checks}

    # -- the authorization ----------------------------------------------------------------------
    auth = record.authorization
    assert auth is not None, "an APPROVE must carry an authorization"
    assert auth.decision_id == record.decision_id
    assert auth.environment == "paper"
    assert auth.ttl_seconds == 15
    assert auth.scope.total_quantity == record.authorized.total_quantity
    assert auth.bound_state.policy_hash == world.mizan.policy.policy_hash
    assert auth.idempotency_key.startswith("mz1-")

    # -- the ledger append ----------------------------------------------------------------------
    assert record.audit_prev_hash == "0" * 64, "the first record links to ZERO_HASH"
    assert record.audit_hash == record_hash_for(record.model_dump(mode="json"))
    stored = world.mizan.get_decision(record.decision_id)
    assert canonical_json(stored) == canonical_json(record), "what was stored is what was returned"

    # -- the execution gate ---------------------------------------------------------------------
    result = world.mizan.execute(record.decision_id)
    assert result.status == "SUBMITTED", (result.status, world.codes(result))
    assert result.reason_codes == []
    assert result.broker.environment == "paper"
    assert result.decision_id == record.decision_id
    assert result.auth_id == auth.auth_id
    assert result.revalidation.performed is True, "E9: the gate re-evaluates on every execution"
    assert result.revalidation.supported is True
    assert result.client_order_id == auth.idempotency_key, "E7: the key is derived, never chosen"
    assert result.broker_order_id is not None

    # -- what actually reached the venue --------------------------------------------------------
    assert len(world.broker.submitted) == 1
    order = world.broker.submitted[0]
    assert order.symbol == "AAPL"
    assert [(leg.leg_index, leg.quantity) for leg in order.legs] == [(0, "10")]
    assert order.client_order_id == auth.idempotency_key

    # -- E4: the kill switch is read after the last broker read, immediately before the mutation --
    assert world.log.index("kill_switch.is_active") == world.log.index("broker.submit_order") - 1
    assert world.log[-1] == "broker.submit_order"

    # -- decision replay ------------------------------------------------------------------------
    replayed = world.mizan.replay(record.decision_id)
    assert replayed.identical is True, replayed.detail
    assert replayed.replayed_verdict == record.verdict == "APPROVE"
    assert replayed.replayed_verdict_hash == record.governor_decision.verdict_hash

    # -- chain verification ---------------------------------------------------------------------
    verification = world.mizan.verify_chain()
    assert verification.ok is True
    assert verification.length == 1
    assert verification.first_bad_sequence is None

    # -- the chain really is on disk, in this tenant's own file ---------------------------------
    path = SqliteLedger(root_dir=tmp_path).path_for(TENANT_A)
    assert path.exists(), "the SQLite ledger must have written a per-tenant database file"
    reopened = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A)
    assert reopened.verify_chain().ok is True
    assert canonical_json(reopened.get(record.decision_id)) == canonical_json(record)


def test_a_second_execution_reconciles_rather_than_duplicating(tmp_path):
    """E7: asking twice places one order. The gate finds the existing one at the idempotency step."""
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))

    first = world.mizan.execute(record.decision_id)
    second = world.mizan.execute(record.decision_id)

    assert first.status == "SUBMITTED"
    assert second.status == "RECONCILED_EXISTING", (second.status, world.codes(second))
    assert world.codes(second) == ["IDEMPOTENT_ORDER_EXISTS"]
    assert second.client_order_id == first.client_order_id
    assert len(world.broker.submitted) == 1, "one authorization, one order"
    assert world.log.count("broker.submit_order") == 1


def test_the_authorization_is_single_use_even_when_the_broker_forgets(tmp_path):
    """The idempotency step is the broker's memory; the registry is Mizan's, and both must hold.

    Clearing the broker's order book after a successful submission removes the first defence. What
    stops a second order is the authorization registry, which consumed the auth on the first pass.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    assert world.mizan.execute(record.decision_id).status == "SUBMITTED"

    world.broker.orders.clear()  # the venue no longer admits to holding the order

    replayed = world.mizan.execute(record.decision_id)
    assert replayed.status == "BLOCKED", (replayed.status, world.codes(replayed))
    assert world.codes(replayed) == ["AUTHORIZATION_ALREADY_USED"]
    assert len(world.broker.submitted) == 1, "the authorization was already spent"


def test_a_dry_run_stops_one_step_short_of_the_venue(tmp_path):
    """Every check passes and nothing is submitted: the shape ``@protected`` relies on."""
    world = build_world(ledger_dir=tmp_path, dry_run=True)
    record = world.mizan.evaluate(proposal("10"))

    result = world.mizan.execute(record.decision_id)

    assert result.status == "WOULD_SUBMIT", (result.status, world.codes(result))
    assert result.reason_codes == []
    assert result.revalidation.performed is True and result.revalidation.supported is True
    assert result.kill_switch_checked_at is not None, "the switch is still read before stopping"
    assert world.broker.submitted == []
    assert "broker.submit_order" not in world.log
