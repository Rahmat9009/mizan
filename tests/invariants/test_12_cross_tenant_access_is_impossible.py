"""Invariant 12 - Hard Rule B3: cross-tenant access is impossible by construction (separate schemas, not filters).

Pass criterion: for both InMemoryLedger and SqliteLedger, a decision appended through tenant-a's TenantLedger is
NotFound through tenant-b's, absent from tenant-b's list(), and tenant-b's chain starts at sequence 1 with
ZERO_HASH (it is not chained to tenant-a's); for SqliteLedger the tenants live in separate files and tenant-b's
database holds no reference to tenant-a's decision_id or audit_hash; tenant ids containing path characters or
violating TenantId ("../x", "a/b", "A", "", ...) are rejected without creating any file; a record whose objects
belong to another tenant cannot be appended; mizan.risk.evaluate REJECTs with TENANT_MISMATCH when the policy's
tenant differs from the context's; and the SDK is tenant-scoped over a shared ledger.
"""
from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from mizan import risk
from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts.canonical import ZERO_HASH
from mizan.contracts.errors import MizanError, NotFound
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan

from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    TENANT_B,
    make_context,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)
from tests.invariants._support import RecordingBroker, append_fixture_record, codes

BAD_TENANT_IDS = ("../x", "a/b", "A", "", "..", "a\\b", "tenant a", "tenant-a.sqlite", "-a", "a" * 64)


def _sqlite_references(db_path, needle: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        tables = [
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        hits = 0
        for table in tables:
            columns = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                (count,) = con.execute(
                    f'SELECT count(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?',
                    (f"%{needle}%",),
                ).fetchone()
                hits += count
        return hits
    finally:
        con.close()


def test_cross_tenant_access_is_impossible(tmp_path):
    assert TENANT_A != TENANT_B
    sqlite_record = None
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        ledger_a = ledger.for_tenant(TENANT_A)
        ledger_b = ledger.for_tenant(TENANT_B)
        assert ledger_a.tenant_id == TENANT_A and ledger_b.tenant_id == TENANT_B

        record = append_fixture_record(ledger_a)
        assert record.tenant_id == TENANT_A
        assert ledger_a.get(record.decision_id).decision_id == record.decision_id

        with pytest.raises(NotFound):
            ledger_b.get(record.decision_id)
        assert ledger_b.list(limit=50) == []
        assert record.decision_id not in {r.decision_id for r in ledger_b.list(limit=50)}
        assert ledger_b.verify_chain().length == 0

        # tenant-b's chain is its own: sequence restarts and nothing links to tenant-a
        record_b = append_fixture_record(ledger_b, tenant_id=TENANT_B)
        assert record_b.tenant_id == TENANT_B
        assert record_b.sequence == 1
        assert record_b.audit_prev_hash == ZERO_HASH
        assert record_b.audit_prev_hash != record.audit_hash
        with pytest.raises(NotFound):
            ledger_a.get(record_b.decision_id)
        assert {r.decision_id for r in ledger_a.list(limit=50)} == {record.decision_id}
        assert ledger_a.verify_chain().ok and ledger_b.verify_chain().ok
        if isinstance(ledger, SqliteLedger):
            sqlite_record = (record, record_b)

    # sqlite: one file per tenant, and tenant-b's file holds no trace of tenant-a's record
    record_a, record_b = sqlite_record
    db_a = tmp_path / f"{TENANT_A}.sqlite"
    db_b = tmp_path / f"{TENANT_B}.sqlite"
    assert db_a.exists() and db_b.exists(), sorted(p.name for p in tmp_path.iterdir())
    assert _sqlite_references(db_a, record_a.decision_id) >= 1
    assert _sqlite_references(db_b, record_a.decision_id) == 0
    assert _sqlite_references(db_b, record_a.audit_hash) == 0
    assert _sqlite_references(db_a, record_b.decision_id) == 0

    # tenant ids that could escape the per-tenant schema are rejected and create nothing
    files_before = sorted(p.name for p in tmp_path.iterdir())
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        for bad in BAD_TENANT_IDS:
            with pytest.raises((ValidationError, MizanError, ValueError)):
                ledger.for_tenant(bad)
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    assert not (tmp_path.parent / "x.sqlite").exists()
    assert not (tmp_path / "a" / "b.sqlite").exists()


def test_tenant_mismatch_between_policy_and_context_rejects():
    policy_b = make_policy(tenant_id=TENANT_B)
    context_a = make_context(tenant_id=TENANT_A, policy=policy_b.ref)
    evaluation = risk.evaluate(make_proposal(), context_a, policy_b)
    assert evaluation.verdict == "REJECT"
    assert "TENANT_MISMATCH" in codes(evaluation), codes(evaluation)
    assert evaluation.recommended_quantity == "0"


def test_record_belonging_to_another_tenant_cannot_be_appended(tmp_path):
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        ledger_a = ledger.for_tenant(TENANT_A)
        with pytest.raises((MizanError, ValidationError)):
            append_fixture_record(ledger_a, tenant_id=TENANT_B)
        assert ledger_a.list(limit=50) == []
        assert ledger_a.verify_chain().length == 0


def test_sdk_is_tenant_scoped_over_a_shared_ledger():
    shared = InMemoryLedger()
    proposal = make_proposal()

    def sdk(tenant_id):
        broker = RecordingBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        )
        return Mizan(
            tenant_id=tenant_id,
            agent=proposal.agent,
            policy=make_policy(tenant_id=tenant_id),
            broker=broker,
            ledger=shared,
            advisory=None,
            kill_switch=InMemoryKillSwitch(),
            config=ExecutionConfig(),
            clock=lambda: FIXED_NOW,
        )

    mizan_a, mizan_b = sdk(TENANT_A), sdk(TENANT_B)
    record = mizan_a.evaluate(proposal)
    assert record.tenant_id == TENANT_A
    assert mizan_a.get_decision(record.decision_id).decision_id == record.decision_id
    with pytest.raises(NotFound):
        mizan_b.get_decision(record.decision_id)
    with pytest.raises(NotFound):
        mizan_b.replay(record.decision_id)
    with pytest.raises((NotFound, MizanError)):
        mizan_b.execute(record.decision_id)
