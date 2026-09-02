"""Hard Rule B3: cross-tenant access is impossible by construction, not by a WHERE clause.

One tenant, one chain, one storage unit. For SQLite that means one database FILE per tenant: a query
cannot reach another tenant's rows because they are not in the same database. A tenant id therefore
reaches the filesystem, and is validated before it does.
"""

from __future__ import annotations

import sqlite3

import pytest

from mizan.audit import InMemoryLedger, SqliteLedger, validate_tenant_id
from mizan.contracts.canonical import ZERO_HASH
from mizan.contracts.errors import MizanError, NotFound, TenantForbidden
from tests.audit._helpers import append_record, chain_parts
from tests.fixtures import FIXED_NOW, TENANT_A, TENANT_B

REJECTED_TENANT_IDS = (
    "../x",
    "../../etc/passwd",
    "a/b",
    "a\\b",
    "A",
    "Tenant-A",
    "",
    ".",
    "..",
    "tenant a",
    "tenant-a.sqlite",
    "-a",
    "a" * 64,
    "tenant_a",
    "tenant-a\x00",
    ":memory:",
)

ACCEPTED_TENANT_IDS = ("a", "tenant-a", "0", "a" * 63, "acme-capital-2026")


@pytest.mark.parametrize("tenant_id", REJECTED_TENANT_IDS)
def test_validate_tenant_id_refuses_anything_that_could_escape_its_own_store(tenant_id):
    with pytest.raises(ValueError):
        validate_tenant_id(tenant_id)


@pytest.mark.parametrize("tenant_id", ACCEPTED_TENANT_IDS)
def test_validate_tenant_id_accepts_the_contract_form(tenant_id):
    assert validate_tenant_id(tenant_id) == tenant_id


def test_a_rejected_tenant_id_never_reaches_the_filesystem(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    ledger.for_tenant(TENANT_A)
    before = sorted(path.name for path in tmp_path.iterdir())

    for tenant_id in REJECTED_TENANT_IDS:
        with pytest.raises((ValueError, MizanError)):
            ledger.for_tenant(tenant_id)
        with pytest.raises((ValueError, MizanError)):
            InMemoryLedger().for_tenant(tenant_id)

    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not (tmp_path.parent / "x.sqlite").exists()
    assert not (tmp_path / "a").exists()


def test_two_tenants_get_two_files_and_neither_can_see_the_other(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    ledger_a = ledger.for_tenant(TENANT_A)
    ledger_b = ledger.for_tenant(TENANT_B)

    record_a = append_record(ledger_a)
    record_b = append_record(ledger_b, tenant_id=TENANT_B)

    db_a = tmp_path / f"{TENANT_A}.sqlite"
    db_b = tmp_path / f"{TENANT_B}.sqlite"
    assert db_a.exists() and db_b.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([db_a.name, db_b.name])

    # each chain starts at 1 from the zero hash: tenant-b is not chained behind tenant-a
    assert record_a.sequence == record_b.sequence == 1
    assert record_a.audit_prev_hash == record_b.audit_prev_hash == ZERO_HASH
    assert record_a.audit_hash != record_b.audit_hash

    with pytest.raises(NotFound):
        ledger_b.get(record_a.decision_id)
    with pytest.raises(NotFound):
        ledger_a.get(record_b.decision_id)
    assert [r.decision_id for r in ledger_a.list(limit=50)] == [record_a.decision_id]
    assert [r.decision_id for r in ledger_b.list(limit=50)] == [record_b.decision_id]

    for needle, expected_in_a, expected_in_b in (
        (record_a.decision_id, True, False),
        (record_a.audit_hash, True, False),
        (record_b.decision_id, False, True),
        (record_b.audit_hash, False, True),
        (TENANT_A, True, False),
        (TENANT_B, False, True),
    ):
        assert (_references(db_a, needle) > 0) is expected_in_a, needle
        assert (_references(db_b, needle) > 0) is expected_in_b, needle


def _references(db_path, needle: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        tables = [
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        hits = 0
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                (count,) = connection.execute(
                    f'SELECT count(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?',
                    (f"%{needle}%",),
                ).fetchone()
                hits += count
        return hits
    finally:
        connection.close()


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_record_belonging_to_another_tenant_is_refused(tmp_path, storage):
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    ledger_a = ledger.for_tenant(TENANT_A)

    foreign = chain_parts(tenant_id=TENANT_B)
    with pytest.raises(TenantForbidden):
        ledger_a.append(
            proposal=foreign.proposal,
            risk_context=foreign.context,
            risk_evaluation=foreign.evaluation,
            governor_decision=foreign.decision,
            policy_snapshot=foreign.policy,
            recorded_at=FIXED_NOW,
        )

    assert ledger_a.list(limit=50) == []
    assert ledger_a.verify_chain().length == 0


def test_a_ledger_file_cannot_be_reopened_under_another_tenant_id(tmp_path):
    """The tenant is written into the database and checked on every open, in Python and in SQL."""
    ledger = SqliteLedger(root_dir=tmp_path)
    append_record(ledger.for_tenant(TENANT_A))

    renamed = tmp_path / f"{TENANT_B}.sqlite"
    (tmp_path / f"{TENANT_A}.sqlite").replace(renamed)

    with pytest.raises(TenantForbidden):
        SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_B)


def test_control_events_are_tenant_scoped_too(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    ledger_a = ledger.for_tenant(TENANT_A)
    ledger_b = ledger.for_tenant(TENANT_B)
    event = ledger_a.append_control_event(
        event_type="kill_switch_activated",
        actor={"type": "system", "id": "mizan-core"},
        trigger_reason_codes=["KILL_SWITCH_ACTIVE"],
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
    )
    assert event.tenant_id == TENANT_A
    assert ledger_b.list_control_events(limit=50) == []
    assert ledger_b.verify_chain().length == 0
    assert _references(tmp_path / f"{TENANT_B}.sqlite", event.event_id) == 0
