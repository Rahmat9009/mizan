"""Tenancy is structural in the SDK: a ``Mizan`` instance is one tenant's whole world.

Finding F-17 was cross-tenant reads through a shared store. The answer here is not a filter but a
binding: the instance resolves its ``TenantLedger`` once and every read and write goes through that
object, so there is no expression in this class that names another tenant's chain. These tests attack
the boundary from the outside — a shared ledger, a stolen decision id, a foreign policy — and check
that each one fails, and fails *identically* to an id that never existed.
"""

from __future__ import annotations

import pytest

from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts.canonical import ZERO_HASH
from mizan.contracts.errors import MizanError, NotFound
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    TENANT_B,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)


def pipeline_for(tenant_id: str, ledger) -> Mizan:
    from mizan.adapters import MockBroker

    proposal = make_proposal()
    return Mizan(
        tenant_id=tenant_id,
        agent=proposal.agent,
        policy=make_policy(tenant_id=tenant_id),
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=ledger,
        advisory=None,
        kill_switch=InMemoryKillSwitch(),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def shared_ledger(request, tmp_path):
    """One ledger object serving both tenants — the arrangement F-17 was found in."""
    if request.param == "memory":
        return InMemoryLedger()
    return SqliteLedger(root_dir=tmp_path)


def test_one_tenants_decision_is_invisible_to_another_over_a_shared_ledger(shared_ledger):
    alice = pipeline_for(TENANT_A, shared_ledger)
    mallory = pipeline_for(TENANT_B, shared_ledger)

    record = alice.evaluate(make_proposal())
    assert record.tenant_id == TENANT_A
    assert alice.get_decision(record.decision_id).decision_id == record.decision_id

    with pytest.raises(NotFound):
        mallory.get_decision(record.decision_id)
    with pytest.raises(NotFound):
        mallory.replay(record.decision_id)
    with pytest.raises((NotFound, MizanError)):
        mallory.execute(record.decision_id)
    assert mallory.list_decisions() == []
    assert mallory.verify_chain().length == 0


def test_a_stolen_id_is_indistinguishable_from_one_that_never_existed(shared_ledger):
    """The refusal must not be an existence oracle: same class, same status, same message."""
    alice = pipeline_for(TENANT_A, shared_ledger)
    mallory = pipeline_for(TENANT_B, shared_ledger)
    record = alice.evaluate(make_proposal())

    with pytest.raises(NotFound) as stolen:
        mallory.get_decision(record.decision_id)
    with pytest.raises(NotFound) as invented:
        mallory.get_decision("01a00000-0000-7000-8000-000000000000")

    assert type(stolen.value) is type(invented.value)
    assert stolen.value.http_status == invented.value.http_status == 404
    assert stolen.value.message == invented.value.message
    assert stolen.value.reason_codes == invented.value.reason_codes


def test_each_tenants_chain_starts_at_its_own_genesis(shared_ledger):
    alice = pipeline_for(TENANT_A, shared_ledger)
    bob = pipeline_for(TENANT_B, shared_ledger)

    first = alice.evaluate(make_proposal())
    second = bob.evaluate(make_proposal())

    assert second.sequence == 1
    assert second.audit_prev_hash == ZERO_HASH
    assert second.audit_prev_hash != first.audit_hash
    assert alice.verify_chain().ok and bob.verify_chain().ok
    assert {r.decision_id for r in alice.list_decisions()} == {first.decision_id}
    assert {r.decision_id for r in bob.list_decisions()} == {second.decision_id}


def test_the_instance_holds_the_tenant_ledger_not_the_shared_one():
    """The boundary is an object, not a parameter. Nothing in the class can name another tenant."""
    ledger = InMemoryLedger()
    alice = pipeline_for(TENANT_A, ledger)

    bound = alice._tenant_ledger()  # noqa: SLF001 - asserting the boundary is the point
    assert bound.tenant_id == TENANT_A
    assert bound is ledger.for_tenant(TENANT_A)
    assert bound is not ledger.for_tenant(TENANT_B)


def test_a_replay_cannot_be_run_against_another_tenants_policy(shared_ledger):
    alice = pipeline_for(TENANT_A, shared_ledger)
    record = alice.evaluate(make_proposal())

    result = alice.replay(record.decision_id, policy=make_policy(tenant_id=TENANT_B))

    # Either the SDK refuses outright or the engine rejects on TENANT_MISMATCH; what must never happen
    # is a decision replayed *successfully* under another tenant's rules.
    assert result.identical is False
    assert result.replayed_verdict == "REJECT"


def test_an_unbindable_tenant_id_is_refused_before_anything_is_created(tmp_path):
    from mizan.adapters import MockBroker

    proposal = make_proposal()
    for bad in ("../x", "a/b", "A", "", "..", "tenant a"):
        with pytest.raises(Exception):  # noqa: B017 - the type varies by layer; the refusal is the point
            Mizan(
                tenant_id=bad,
                agent=proposal.agent,
                policy=make_policy(),
                broker=MockBroker(
                    portfolio_snapshot=make_portfolio_snapshot(),
                    market_snapshot=make_market_snapshot(),
                ),
                ledger=SqliteLedger(root_dir=tmp_path),
                clock=lambda: FIXED_NOW,
            ).evaluate(proposal)
    assert sorted(p.name for p in tmp_path.iterdir()) == []
