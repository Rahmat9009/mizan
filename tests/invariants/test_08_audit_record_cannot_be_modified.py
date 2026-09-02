"""Invariant 08 - Hard Rule A2: append-only ledger; no update path at any privilege level.

Pass criterion: (a) neither Ledger nor TenantLedger (InMemoryLedger and SqliteLedger, and the TenantLedger
Protocol itself) exposes any public attribute whose name contains update/delete/remove/truncate/modify/purge/
clear/overwrite/rewrite; a DecisionRecord obtained from the ledger is frozen (attribute assignment raises, on the
record and on its nested objects) and in-place mutation of a returned container never reaches storage; (b) for
SqliteLedger, a raw sqlite3 connection cannot UPDATE any column of decision_records - the schema trigger aborts -
and rows, verify_chain() and get() are unchanged afterwards; (c) a record with an altered audit_hash cannot even be
instantiated through the contract.
"""
from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from mizan.audit import InMemoryLedger, SqliteLedger, TenantLedger
from mizan.contracts import DecisionRecord, ReasonCode
from mizan.contracts.canonical import ZERO_HASH, canonical_json

from tests.fixtures import TENANT_A
from tests.invariants._support import append_fixture_record

FORBIDDEN_NAME_PARTS = (
    "update", "delete", "remove", "truncate", "modify", "purge", "clear", "overwrite", "rewrite",
)
FROZEN_ERRORS = (ValidationError, AttributeError, TypeError)


def _mutation_like_names(obj) -> list[str]:
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


def test_audit_record_cannot_be_modified(tmp_path):
    assert _mutation_like_names(TenantLedger) == []
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        tenant_ledger = ledger.for_tenant(TENANT_A)
        for target in (ledger, tenant_ledger):
            offenders = _mutation_like_names(target)
            assert offenders == [], f"{type(target).__name__} exposes mutation-like attributes: {offenders}"

        record = append_fixture_record(tenant_ledger)
        stored = canonical_json(tenant_ledger.get(record.decision_id))

        with pytest.raises(FROZEN_ERRORS):
            record.verdict = "APPROVE"
        with pytest.raises(FROZEN_ERRORS):
            record.audit_hash = ZERO_HASH
        with pytest.raises(FROZEN_ERRORS):
            record.governor_decision.verdict = "APPROVE"
        with pytest.raises(FROZEN_ERRORS):
            record.risk_evaluation.recommended_quantity = "999999"

        # in-place container mutation on the handle we were given must not reach storage
        try:
            record.reason_codes.append(next(iter(ReasonCode)))
        except Exception:
            pass
        try:
            record.checks.clear()
        except Exception:
            pass
        assert canonical_json(tenant_ledger.get(record.decision_id)) == stored
        assert tenant_ledger.verify_chain().ok

    # storage level: the sqlite schema itself refuses UPDATE
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    assert db_path.exists(), sorted(p.name for p in tmp_path.iterdir())
    before = _all_rows(db_path)
    assert len(before) >= 1

    con = sqlite3.connect(db_path)
    try:
        for statement, params in (
            ("UPDATE decision_records SET record_json = ? WHERE sequence = 1", ("{}",)),
            ("UPDATE decision_records SET audit_hash = ? WHERE sequence = 1", (ZERO_HASH,)),
            ("UPDATE decision_records SET audit_prev_hash = ? WHERE sequence = 1", (ZERO_HASH,)),
            ("UPDATE decision_records SET decision_id = 'forged' WHERE sequence = 1", ()),
            ("UPDATE decision_records SET sequence = 999 WHERE sequence = 1", ()),
            ("UPDATE decision_records SET record_json = record_json", ()),
        ):
            with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                con.execute(statement, params)
        con.rollback()
    finally:
        con.close()

    assert _all_rows(db_path) == before
    verification = ledger.for_tenant(TENANT_A).verify_chain()
    assert verification.ok and verification.length == len(before)
    assert tenant_ledger.get(before[0][1]).sequence == 1
    # the ledger keeps working after the refused writes
    next_record = append_fixture_record(tenant_ledger)
    assert next_record.sequence == len(before) + 1
    assert tenant_ledger.verify_chain().ok


def test_modified_record_cannot_be_instantiated_through_the_contract():
    record = append_fixture_record(InMemoryLedger().for_tenant(TENANT_A))
    payload = record.model_dump(mode="json")
    forged_hash = {**payload, "audit_hash": ZERO_HASH}
    with pytest.raises(ValidationError):
        DecisionRecord.model_validate(forged_hash)
    forged_content = {**payload, "recorded_at": "2020-01-01T00:00:00.000000Z"}
    with pytest.raises(ValidationError):
        DecisionRecord.model_validate(forged_content)
    assert DecisionRecord.model_validate(payload).audit_hash == record.audit_hash
