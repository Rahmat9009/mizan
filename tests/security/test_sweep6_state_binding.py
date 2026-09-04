"""L5 Sweep 6, new target — defeat state-bound authorization.

Can an authorization be used after the state it was bound to changed? Can the binding be forged?
Can one authorization be made to cover a different proposal, account, amount or tenant?

The answers, pinned:

* **the binding is not self-authenticating and does not need to be.** ``authorization_hash`` is a
  plain SHA-256 of the content, not a MAC, so anyone who can construct an ``ExecutionAuthorization``
  can make one internally consistent. What actually stops a forged binding is (a) the gate comparing
  the authorization against the *decision* and the *proposal* it is handed, and (b) the TOCTOU
  re-evaluation, which re-derives the world and re-runs the engine rather than trusting any recorded
  hash. Both are pinned below;
* **the ledger is what makes the objects trustworthy.** A tampered stored record is refused on read,
  and the SQLite storage refuses UPDATE and DELETE at full privilege;
* **F-29 (raised here):** ``BoundState`` carries ``market_snapshot_id`` but no market state hash, so
  the market half of the binding is compared by *identity*. A snapshot that keeps its id and changes
  its quotes is reported as ``state_changed=False``.

Self-contained by design (ESC-3).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mizan import authorization as authorization_module
from mizan import governor, risk
from mizan.adapters import BrokerContextProvider, MockBroker
from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts import BoundState, ExecutionAuthorization, MarketSnapshot, object_hash
from mizan.contracts.errors import AuthorizationError, ChainIntegrityError
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

pytestmark = pytest.mark.security


def a_chain(policy: Any = None, proposal: Any = None) -> dict[str, Any]:
    policy = policy if policy is not None else make_policy()
    proposal = proposal if proposal is not None else make_proposal()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    provider = BrokerContextProvider(broker)
    context = provider.build(
        tenant_id=policy.tenant_id, agent_id=AGENT_ID, proposal=proposal, policy=policy, now=FIXED_NOW
    )
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization_module.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    broker.log.clear()
    return {
        "policy": policy,
        "proposal": proposal,
        "context": context,
        "decision": decision,
        "auth": auth,
        "broker": broker,
        "provider": provider,
    }


def a_gate(chain: dict[str, Any], *, dry_run: bool = False, policy: Any = None) -> ExecutionGate:
    return ExecutionGate(
        broker=chain["broker"],
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=chain["provider"],
        policy=policy if policy is not None else chain["policy"],
        config=ExecutionConfig(enabled=True, dry_run=dry_run),
        clock=lambda: FIXED_NOW,
    )


def forge(auth: ExecutionAuthorization, **overrides: Any) -> ExecutionAuthorization:
    payload = auth.model_dump(mode="json")
    for derived in ("authorization_hash", "idempotency_key", "expires_at"):
        payload.pop(derived, None)
    payload.update(overrides)
    return ExecutionAuthorization.build(**payload)


def codes(result: Any) -> list[str]:
    return [str(getattr(code, "value", code)) for code in result.reason_codes]


# ---------------------------------------------------------------------------------------------
# What the binding actually is
# ---------------------------------------------------------------------------------------------
def test_the_authorization_hash_is_a_content_hash_not_a_signature() -> None:
    """Stated so that nobody mistakes it for one. It proves integrity of a copy, not of an origin."""
    chain = a_chain()
    tampered = forge(chain["auth"], agent_id="agent-someone-else")
    assert tampered.authorization_hash != chain["auth"].authorization_hash
    # ...and it verifies perfectly, because the attacker recomputed it. No secret is involved.
    authorization_module.validate(tampered, now=FIXED_NOW)


def test_a_hand_edited_authorization_dies_at_use_not_only_at_construction() -> None:
    """``_check_self_consistent`` re-derives every hash at USE. An object can reach the gate without
    passing a constructor again (deserialised, unpickled, mutated through ``model_construct``)."""
    chain = a_chain()
    smuggled = chain["auth"].model_copy(update={"ttl_seconds": 3600})
    with pytest.raises(AuthorizationError):
        authorization_module.validate(smuggled, now=FIXED_NOW)
    result = a_gate(chain).execute(smuggled, chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_INVALID" in codes(result)
    assert chain["broker"].submitted == []


def test_a_forged_bound_state_cannot_lift_the_authorized_quantity() -> None:
    """Whatever the binding claims, the gate re-runs the engine and compares to the DECISION."""
    chain = a_chain()
    lying = chain["auth"].bound_state.model_dump(mode="json")
    lying["portfolio_state_hash"] = "0" * 64
    lying["response_level"] = 5
    forged = forge(chain["auth"], bound_state=lying)
    result = a_gate(chain).execute(forged, chain["proposal"], chain["decision"])
    # It executes - because the FRESH evaluation still supports the size, which is the real control.
    assert result.status == "SUBMITTED"
    submitted = chain["broker"].submitted[0]
    assert [leg.quantity for leg in submitted.legs] == [
        leg.quantity for leg in chain["decision"].authorized.legs
    ], "the forged binding did not change one authorized unit"


def test_a_forged_policy_binding_is_refused_because_the_policy_hash_is_checked_three_ways() -> None:
    chain = a_chain()
    lying = chain["auth"].bound_state.model_dump(mode="json")
    lying["policy_hash"] = "1" * 64
    with pytest.raises(ValidationError):
        # The contract itself refuses: bound_state.policy_hash must equal policy.hash.
        forge(chain["auth"], bound_state=lying)


def test_a_policy_swapped_under_the_gate_forces_reauthorization() -> None:
    chain = a_chain()
    rebound = make_policy(policy_version="1.0.1")
    assert rebound.policy_hash != chain["policy"].policy_hash
    result = a_gate(chain, policy=rebound).execute(
        chain["auth"], chain["proposal"], chain["decision"]
    )
    assert result.status == "BLOCKED"
    assert "STATE_BINDING_MISMATCH" in codes(result)
    assert chain["broker"].submitted == []


def test_a_response_level_escalation_between_decision_and_execution_forces_reauthorization() -> None:
    """R-GRAD: the ladder may only tighten, and a tightening invalidates an outstanding permission."""
    chain = a_chain()
    escalated = BrokerContextProvider(chain["broker"], response_level=3)
    gate = ExecutionGate(
        broker=chain["broker"],
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=escalated,
        policy=chain["policy"],
        config=ExecutionConfig(enabled=True, dry_run=False),
        clock=lambda: FIXED_NOW,
    )
    result = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert "RESPONSE_LEVEL_ESCALATED" in codes(result)
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert chain["broker"].submitted == []


def test_a_forged_response_level_defeats_only_the_escalation_check_not_the_engine() -> None:
    """Honest about the residual: a bound level of 5 makes ``escalated`` unreachable.

    The fresh evaluation still runs the escalated response-level gate against the *fresh context*,
    so the size is still re-derived under the tighter rules. The forged binding buys the attacker
    the escalation branch and nothing else.
    """
    chain = a_chain()
    lying = chain["auth"].bound_state.model_dump(mode="json")
    lying["response_level"] = 5
    forged = forge(chain["auth"], bound_state=lying)
    escalated = BrokerContextProvider(chain["broker"], response_level=3)
    gate = ExecutionGate(
        broker=chain["broker"],
        kill_switch=InMemoryKillSwitch(),
        registry=InMemoryAuthorizationRegistry(),
        context_provider=escalated,
        policy=chain["policy"],
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )
    result = gate.execute(forged, chain["proposal"], chain["decision"])
    assert "RESPONSE_LEVEL_ESCALATED" not in codes(result)
    assert result.revalidation.response_level_at_execution == 3
    # And the fresh engine ran against level 3, so the size on the wire is still the engine's.
    assert result.revalidation.performed is True


def test_the_portfolio_half_of_the_binding_is_compared_by_content_hash() -> None:
    """A snapshot that keeps its id and changes its contents IS detected - for the portfolio."""
    chain = a_chain()
    payload = chain["context"].portfolio_snapshot.model_dump(mode="json")
    payload["equity"] = "90000"
    payload["cash"] = "90000"
    payload["buying_power"] = "90000"
    mutated = type(chain["context"].portfolio_snapshot).model_validate(payload)
    assert mutated.snapshot_id == chain["context"].portfolio_snapshot.snapshot_id
    assert object_hash(mutated) != chain["auth"].bound_state.portfolio_state_hash
    chain["broker"].set_portfolio_snapshot(mutated)
    result = a_gate(chain, dry_run=True).execute(
        chain["auth"], chain["proposal"], chain["decision"]
    )
    assert result.revalidation.state_changed is True


# ---------------------------------------------------------------------------------------------
# FINDING F-29 - the market half of the binding is compared by identity, not by content
# ---------------------------------------------------------------------------------------------
def test_f29_bound_state_carries_no_market_state_hash() -> None:
    """The contract field simply does not exist, so ``_state_changed`` cannot compare content."""
    fields = set(BoundState.model_fields)
    assert "portfolio_state_hash" in fields
    assert "market_state_hash" not in fields, (
        "F-29 fixed: BoundState gained a market state hash - update security/findings.md"
    )


def test_f29_a_moved_market_cannot_reuse_its_snapshot_id() -> None:
    """F-29 is closed, and not by a change to the gate: the attack stopped being REPRESENTABLE.

    The finding was that ``ExecutionGate._state_changed`` compares market snapshots by id, so quotes
    that moved under a REUSED id would be submitted against with ``state_changed=False`` - an order
    placed on prices the authorization was never bound to, and a record saying otherwise.

    REQ-34 then made ``snapshot_id`` a hash of the snapshot's own content. A payload carrying moved
    quotes under its old id no longer validates, so the state the finding describes cannot be built,
    passed to a broker, or recorded. Comparing by id IS comparing by content once the id is derived
    from the content.

    Which is exactly why this is pinned here. The gate's correctness now RESTS on that derivation,
    silently - make ``snapshot_id`` an opaque string again, for any reasonable-sounding reason, and
    ``_state_changed`` goes back to being unsound with nothing to say so.
    """
    chain = a_chain()
    original = chain["context"].market_snapshot
    payload = original.model_dump(mode="json")
    quote = payload["quotes"][chain["proposal"].symbol]
    quote["price"] = "230.0"
    quote["bid"] = "229.9"
    quote["ask"] = "230.1"

    with pytest.raises(ValidationError, match="snapshot_id"):
        MarketSnapshot.model_validate(payload)


def test_f29_every_quote_change_moves_the_snapshot_id() -> None:
    """The property the gate depends on, stated directly: content in, identity out.

    If any field a decision was priced against could change without moving the id, an authorization
    could be revalidated against different numbers and report that nothing had changed.
    """
    chain = a_chain()
    original = chain["context"].market_snapshot
    symbol = chain["proposal"].symbol
    seen = {original.snapshot_id}

    for field, value in (("price", "230.0"), ("bid", "229.9"), ("ask", "230.1")):
        payload = original.model_dump(mode="json")
        payload["quotes"][symbol][field] = value
        payload.pop("snapshot_id")
        rebuilt = MarketSnapshot.build(**payload)
        assert rebuilt.snapshot_id not in seen, f"changing {field} left the snapshot id unmoved"
        seen.add(rebuilt.snapshot_id)


def test_f29_the_price_move_is_still_re_evaluated_even_though_it_is_not_reported() -> None:
    """The half that holds: the fresh engine runs on the fresh quotes, so a move that no longer
    supports the size still blocks. F-29 is an audit-fidelity defect, not a size bypass."""
    chain = a_chain()
    payload = chain["context"].market_snapshot.model_dump(mode="json")
    payload["quotes"][chain["proposal"].symbol]["price"] = "1.0"
    payload["quotes"][chain["proposal"].symbol]["bid"] = "0.99"
    payload["quotes"][chain["proposal"].symbol]["ask"] = "1.01"
    # REQ-34: snapshot_id is content-derived, so a mutated payload must be REBUILT rather than
    # revalidated with its old id - which is precisely the staleness F-29 was about.
    payload.pop("snapshot_id", None)
    chain["broker"].set_market_snapshot(MarketSnapshot.build(**payload))
    result = a_gate(chain).execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert chain["broker"].submitted == []


def test_e5_the_gate_never_places_the_smaller_order_a_shrunken_account_supports() -> None:
    """E5: a quiet cut is the most dangerous outcome, because it looks like success."""
    chain = a_chain()
    shrunk = make_portfolio_snapshot(
        equity="20000",
        peak_equity="20000",
        cash="20000",
        buying_power="20000",
        daily_pnl="0",
        positions=[],
        greeks=None,
        gross_exposure="0",
        net_exposure="0",
        margin_requirement="0",
        maintenance_excess="20000",
        factor_exposures=None,
    )
    chain["broker"].set_portfolio_snapshot(shrunk)
    result = a_gate(chain).execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status in {"BLOCKED", "SUBMITTED"}
    if result.status == "SUBMITTED":
        submitted = chain["broker"].submitted[0]
        assert [leg.quantity for leg in submitted.legs] == [
            leg.quantity for leg in chain["auth"].scope.legs
        ], "the gate resized the order instead of refusing it"
    else:
        assert "REAUTHORIZATION_REQUIRED" in codes(result)
        assert chain["broker"].submitted == []


# ---------------------------------------------------------------------------------------------
# The ledger is what makes an authorization trustworthy in the first place
# ---------------------------------------------------------------------------------------------
def test_a_tampered_stored_record_is_refused_on_read() -> None:
    """``Mizan.execute`` takes its authorization from the ledger, so this is the real entry point."""
    policy = make_policy()
    pipeline = Mizan(
        tenant_id=policy.tenant_id,
        agent=make_agent(),
        policy=policy,
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=InMemoryLedger(),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )
    record = pipeline.evaluate(make_proposal())
    tenant_ledger = pipeline.ledger.for_tenant(policy.tenant_id)
    row = tenant_ledger._rows[0]
    document = json.loads(row.record_json)
    document["authorization"]["scope"]["total_quantity"] = "1000"
    document["authorization"]["scope"]["legs"][0]["quantity"] = "1000"
    tenant_ledger._rows[0] = row._replace(record_json=json.dumps(document))

    with pytest.raises(ChainIntegrityError):
        pipeline.get_decision(record.decision_id)
    with pytest.raises(ChainIntegrityError):
        pipeline.execute(record.decision_id)
    assert tenant_ledger.verify_chain().ok is False


def test_the_sqlite_ledger_refuses_update_and_delete_at_full_privilege(tmp_path: Path) -> None:
    """A2: refused by the database, not by a Python guard a raw connection could step around."""
    policy = make_policy()
    pipeline = Mizan(
        tenant_id=policy.tenant_id,
        agent=make_agent(),
        policy=policy,
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=SqliteLedger(tmp_path),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )
    pipeline.evaluate(make_proposal())
    database = next(iter(tmp_path.glob("*.sqlite")))
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("update decision_records set record_json = record_json")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("delete from decision_records")
    finally:
        connection.close()
