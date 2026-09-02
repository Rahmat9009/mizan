"""Invariant 09 - Hard Rule A2: append-only ledger; no delete path at any privilege level.

Pass criterion: (a) neither Ledger nor TenantLedger (InMemoryLedger, SqliteLedger, and the TenantLedger Protocol)
exposes a public attribute whose name contains delete/remove/truncate/purge/clear/drop; records stay retrievable
and countable after any attempt; (b) for SqliteLedger, a raw sqlite3 connection cannot DELETE a single row or the
whole table - the schema trigger aborts - and row count, content, verify_chain() and get() are unchanged
afterwards; the ledger keeps appending at the next sequence.
"""
from __future__ import annotations

import sqlite3

import pytest

from mizan.audit import InMemoryLedger, SqliteLedger, TenantLedger
from mizan.contracts.canonical import canonical_json

from tests.fixtures import TENANT_A
from tests.invariants._support import append_fixture_record

FORBIDDEN_NAME_PARTS = ("delete", "remove", "truncate", "purge", "clear", "drop")


def _deletion_like_names(obj) -> list[str]:
    return [
        name
        for name in dir(obj)
        if not name.startswith("_") and any(part in name.lower() for part in FORBIDDEN_NAME_PARTS)
    ]


def _all_rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT sequence, decision_id, audit_prev_hash, audit_hash, record_json "
            "FROM decision_records ORDER BY sequence"
        ).fetchall()
    finally:
        con.close()


def test_audit_record_cannot_be_deleted(tmp_path):
    assert _deletion_like_names(TenantLedger) == []
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        tenant_ledger = ledger.for_tenant(TENANT_A)
        for target in (ledger, tenant_ledger):
            offenders = _deletion_like_names(target)
            assert offenders == [], f"{type(target).__name__} exposes deletion-like attributes: {offenders}"
        records = [append_fixture_record(tenant_ledger) for _ in range(3)]
        listed = tenant_ledger.list(limit=50)
        assert {r.decision_id for r in listed} == {r.decision_id for r in records}
        assert tenant_ledger.verify_chain().length == 3

    # storage level: the sqlite schema itself refuses DELETE
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    assert db_path.exists(), sorted(p.name for p in tmp_path.iterdir())
    before = _all_rows(db_path)
    assert len(before) == 3
    snapshots = {row[1]: canonical_json(tenant_ledger.get(row[1])) for row in before}

    con = sqlite3.connect(db_path)
    try:
        for statement in (
            "DELETE FROM decision_records WHERE sequence = 1",
            "DELETE FROM decision_records WHERE sequence = 3",
            "DELETE FROM decision_records WHERE decision_id = ?",
            "DELETE FROM decision_records",
        ):
            params = (before[1][1],) if "?" in statement else ()
            with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                con.execute(statement, params)
        con.rollback()
    finally:
        con.close()

    assert _all_rows(db_path) == before
    fresh = ledger.for_tenant(TENANT_A)
    verification = fresh.verify_chain()
    assert verification.ok and verification.length == 3
    for decision_id, snapshot in snapshots.items():
        assert canonical_json(fresh.get(decision_id)) == snapshot
    assert append_fixture_record(fresh).sequence == 4
    assert fresh.verify_chain().ok


def test_listing_never_shrinks_after_delete_attempts_on_sqlite(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    ids = [append_fixture_record(tenant_ledger).decision_id for _ in range(2)]
    con = sqlite3.connect(tmp_path / f"{TENANT_A}.sqlite")
    try:
        with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
            con.execute("DELETE FROM decision_records")
        con.rollback()
    finally:
        con.close()
    assert {r.decision_id for r in tenant_ledger.list(limit=50)} == set(ids)
