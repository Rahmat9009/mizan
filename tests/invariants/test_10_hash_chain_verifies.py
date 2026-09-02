"""Invariant 10 - Hard Rules A1/A5: the ledger is hash-chained and independently verifiable.

Pass criterion: after five appends to a tenant ledger (InMemoryLedger and SqliteLedger) verify_chain() is ok with
length 5; sequences run 1..5; record 1 has audit_prev_hash == ZERO_HASH; every later record's audit_prev_hash equals
its predecessor's audit_hash; every audit_hash equals record_hash_for(dump without audit_hash); the pure
verify_chain_records() agrees. Tampering one byte of record 3 inside the sqlite file (after dropping the
append-only triggers with a raw connection) makes verify_chain() report ok=False with first_bad_sequence == 3, the
pure verifier detects a content-tampered record (3) and a link-broken successor (4), and the offline CLI
`python -m mizan.audit.verify_chain <sqlite-file>` exits non-zero on the tampered file and zero on the good one.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

from mizan.audit import InMemoryLedger, SqliteLedger, verify_chain_records
from mizan.contracts import DecisionRecord
from mizan.contracts.canonical import ZERO_HASH, canonical_json, record_hash_for, sha256_hex

from tests.fixtures import TENANT_A
from tests.invariants._support import PENDING_MARKER, REPO_ROOT, append_fixture_record


def _append_five(tenant_ledger):
    records = [append_fixture_record(tenant_ledger) for _ in range(5)]
    assert len({r.decision_id for r in records}) == 5
    return records


def _tamper_sqlite_record(db_path, sequence: int) -> None:
    con = sqlite3.connect(db_path)
    try:
        triggers = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
        assert triggers, "the append-only triggers must exist on decision_records"
        for name in triggers:
            con.execute(f'DROP TRIGGER "{name}"')
        (raw,) = con.execute(
            "SELECT record_json FROM decision_records WHERE sequence = ?", (sequence,)
        ).fetchone()
        payload = json.loads(raw)
        payload["library_versions"] = {**payload["library_versions"], "python": "tampered"}
        tampered = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        assert tampered != raw
        con.execute(
            "UPDATE decision_records SET record_json = ? WHERE sequence = ?", (tampered, sequence)
        )
        con.commit()
    finally:
        con.close()


def test_hash_chain_verifies(tmp_path):
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        tenant_ledger = ledger.for_tenant(TENANT_A)
        empty = tenant_ledger.verify_chain()
        assert empty.ok is True and empty.length == 0 and empty.first_bad_sequence is None

        records = _append_five(tenant_ledger)
        assert [r.sequence for r in records] == [1, 2, 3, 4, 5]
        assert records[0].audit_prev_hash == ZERO_HASH
        for previous, current in zip(records, records[1:]):
            assert current.audit_prev_hash == previous.audit_hash
            assert current.audit_hash != previous.audit_hash
        for record in records:
            dump = record.model_dump(mode="json")
            assert record.audit_hash == record_hash_for(dump)
            assert record.audit_hash == sha256_hex(
                canonical_json({k: v for k, v in dump.items() if k != "audit_hash"})
            )

        verification = tenant_ledger.verify_chain()
        assert verification.ok is True, verification.detail
        assert verification.length == 5
        assert verification.first_bad_sequence is None

        listed = sorted(tenant_ledger.list(limit=50), key=lambda r: r.sequence)
        assert [r.decision_id for r in listed] == [r.decision_id for r in records]
        for record in records:
            assert canonical_json(tenant_ledger.get(record.decision_id)) == canonical_json(record)

        pure = verify_chain_records(listed)
        assert pure.ok is True and pure.length == 5 and pure.first_bad_sequence is None


def test_tampered_sqlite_record_is_detected_at_its_sequence(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    _append_five(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    assert ledger.for_tenant(TENANT_A).verify_chain().ok

    _tamper_sqlite_record(db_path, 3)

    # a fresh handle: verification must read storage, not a cache
    verification = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain()
    assert verification.ok is False
    assert verification.first_bad_sequence == 3, verification
    assert verification.length == 5


def test_pure_verifier_detects_content_tampering_and_broken_links():
    records = _append_five(InMemoryLedger().for_tenant(TENANT_A))

    # content changed, hash NOT recomputed: detected at 3 (model_construct bypasses validation on purpose)
    forged = DecisionRecord.model_construct(
        **{**dict(records[2]), "library_versions": {**records[2].library_versions, "python": "tampered"}}
    )
    tampered = verify_chain_records([records[0], records[1], forged, records[3], records[4]])
    assert tampered.ok is False and tampered.first_bad_sequence == 3, tampered

    # content changed AND hash recomputed (a self-consistent forgery): the link from 4 breaks
    payload = {k: v for k, v in records[2].model_dump(mode="json").items() if k != "audit_hash"}
    payload["library_versions"] = {**payload["library_versions"], "python": "tampered"}
    rebuilt = DecisionRecord.build(**payload)
    assert rebuilt.audit_hash != records[2].audit_hash
    relinked = verify_chain_records([records[0], records[1], rebuilt, records[3], records[4]])
    assert relinked.ok is False and relinked.first_bad_sequence == 4, relinked

    # a missing record breaks the chain at the gap
    gapped = verify_chain_records([records[0], records[1], records[3], records[4]])
    assert gapped.ok is False and gapped.first_bad_sequence == 4, gapped

    # the untouched chain still verifies
    assert verify_chain_records(records).ok is True


def _run_cli(db_path):
    completed = subprocess.run(
        [sys.executable, "-m", "mizan.audit.verify_chain", str(db_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if "NotImplementedError" in output and PENDING_MARKER in output:
        raise NotImplementedError(output.strip().splitlines()[-1])
    return completed


def test_offline_cli_verifies_a_good_chain_and_rejects_a_tampered_one(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    _append_five(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"

    good = _run_cli(db_path)
    assert good.returncode == 0, good.stdout + good.stderr

    _tamper_sqlite_record(db_path, 3)
    bad = _run_cli(db_path)
    assert bad.returncode != 0, bad.stdout + bad.stderr
    assert "3" in (bad.stdout + bad.stderr)
