"""Hard Rule A2 at the storage layer: the DATABASE refuses UPDATE and DELETE, not just the API.

Security finding F-5 is the reason these are storage-level tests rather than API-level ones: the legacy
ledger had no chain and no triggers, and its execution/order tables were ``ON CONFLICT DO UPDATE``
upserts, so anyone with file access rewrote history and nothing showed. A Python guard would have been
just as easy to walk around. These tests hold a raw ``sqlite3`` connection with full privileges - the
attacker's position - and check that the schema itself says no.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from mizan.audit import SCHEMA_STATEMENTS, InMemoryLedger, SqliteLedger, TenantLedger
from mizan.contracts import ReasonCode
from mizan.contracts.canonical import ZERO_HASH, canonical_json
from tests.audit._helpers import all_rows, append_record
from tests.fixtures import FIXED_NOW, TENANT_A

SQL_REFUSALS = (sqlite3.IntegrityError, sqlite3.OperationalError)
FROZEN_ERRORS = (ValidationError, AttributeError, TypeError)


def _ledger(tmp_path, count: int = 3):
    """A SQLite ledger holding ``count`` decisions and one control event, plus its file path."""
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    records = [append_record(tenant_ledger) for _ in range(count)]
    tenant_ledger.append_control_event(
        event_type="kill_switch_activated",
        actor={"type": "system", "id": "mizan-core"},
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
        trigger_reason_codes=["KILL_SWITCH_ACTIVE"],
    )
    return ledger, tenant_ledger, tmp_path / f"{TENANT_A}.sqlite", records


@pytest.mark.parametrize("table", ["decision_records", "control_events"])
def test_a_raw_connection_cannot_update_any_column(tmp_path, table):
    _ledger_obj, tenant_ledger, db_path, _records = _ledger(tmp_path)
    before = all_rows(db_path, table)
    assert before, f"{table} must hold at least one row for this test to mean anything"

    connection = sqlite3.connect(db_path)
    try:
        for statement, parameters in (
            (f"UPDATE {table} SET record_json = ? WHERE sequence = ?", ("{}", before[0][0])),
            (f"UPDATE {table} SET audit_hash = ? WHERE sequence = ?", (ZERO_HASH, before[0][0])),
            (f"UPDATE {table} SET audit_prev_hash = ?", (ZERO_HASH,)),
            (f"UPDATE {table} SET tenant_id = 'tenant-b'", ()),
            (f"UPDATE {table} SET record_json = record_json", ()),
            (f"UPDATE {table} SET sequence = sequence + 1000", ()),
        ):
            with pytest.raises(SQL_REFUSALS):
                connection.execute(statement, parameters)
        connection.rollback()
    finally:
        connection.close()

    assert all_rows(db_path, table) == before
    assert tenant_ledger.verify_chain().ok


@pytest.mark.parametrize("table", ["decision_records", "control_events"])
def test_a_raw_connection_cannot_delete_a_row_or_the_table(tmp_path, table):
    _ledger_obj, tenant_ledger, db_path, _records = _ledger(tmp_path)
    before = all_rows(db_path, table)
    verification_before = tenant_ledger.verify_chain()

    connection = sqlite3.connect(db_path)
    try:
        for statement in (
            f"DELETE FROM {table} WHERE sequence = {before[0][0]}",
            f"DELETE FROM {table} WHERE 1 = 1",
            # SQLite's "truncate optimisation" (an unqualified DELETE) is disabled by the presence of a
            # BEFORE DELETE trigger, so this takes the row-by-row path and aborts on the first row.
            f"DELETE FROM {table}",
        ):
            with pytest.raises(SQL_REFUSALS):
                connection.execute(statement)
        connection.rollback()
    finally:
        connection.close()

    assert all_rows(db_path, table) == before
    after = tenant_ledger.verify_chain()
    assert after.ok and after.length == verification_before.length


def test_ledger_metadata_is_immutable_as_well(tmp_path):
    _ledger_obj, _tenant_ledger, db_path, _records = _ledger(tmp_path, count=1)
    connection = sqlite3.connect(db_path)
    try:
        for statement in (
            "UPDATE ledger_meta SET value = 'tenant-b' WHERE key = 'tenant_id'",
            "DELETE FROM ledger_meta",
        ):
            with pytest.raises(SQL_REFUSALS):
                connection.execute(statement)
        connection.rollback()
        assert connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'tenant_id'"
        ).fetchone() == (TENANT_A,)
    finally:
        connection.close()


def test_a_direct_insert_cannot_break_the_chain(tmp_path):
    """Even a hand-written INSERT has to extend the chain: right sequence, right link, right tenant."""
    _ledger_obj, tenant_ledger, db_path, records = _ledger(tmp_path, count=2)
    head = tenant_ledger.chain_entries()[-1]
    next_sequence = head.sequence + 1

    connection = sqlite3.connect(db_path)
    try:
        forgeries = (
            ("sequence skips ahead", next_sequence + 5, "forged-1", head.audit_hash),
            ("sequence goes back", 1, "forged-2", head.audit_hash),
            ("link points nowhere", next_sequence, "forged-3", ZERO_HASH),
            ("link points at an older record", next_sequence, "forged-4", records[0].audit_hash),
        )
        for label, sequence, decision_id, previous in forgeries:
            with pytest.raises(SQL_REFUSALS, match="append-only"):
                connection.execute(
                    "INSERT INTO decision_records (sequence, decision_id, audit_prev_hash, "
                    "audit_hash, tenant_id, record_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sequence, decision_id, previous, f"f{sequence:063d}", TENANT_A, "{}", "2026-01-01"),
                )
            assert label
        with pytest.raises(SQL_REFUSALS, match="tenant boundary"):
            connection.execute(
                "INSERT INTO decision_records (sequence, decision_id, audit_prev_hash, audit_hash, "
                "tenant_id, record_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (next_sequence, "forged-5", head.audit_hash, "f" * 64, "tenant-b", "{}", "2026-01-01"),
            )
        connection.rollback()
    finally:
        connection.close()

    assert tenant_ledger.verify_chain().ok
    assert len(tenant_ledger.chain_entries()) == 3


def test_no_sql_the_ledger_runs_is_an_upsert():
    """F-5: an ``ON CONFLICT DO UPDATE`` is an update wearing a disguise. No SQL here is one.

    Every SQL string the module can execute is a string constant, so the check reads the executable
    constants and ignores prose - a docstring is allowed to name the thing it forbids.
    """
    import ast

    import mizan.audit
    import mizan.audit.verify_chain

    forbidden = ("on conflict", "insert or replace", "insert or ignore", "upsert")
    statements = ("UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE")
    for module in (mizan.audit, mizan.audit.verify_chain):
        source = module.__file__
        assert source is not None
        tree = ast.parse(open(source, encoding="utf-8").read(), filename=source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            text = node.value
            for pattern in forbidden:
                assert pattern not in text.lower(), (
                    f"{source}:{node.lineno} runs SQL containing {pattern!r}"
                )
            # UPDATE / DELETE / DROP appear only *inside* CREATE TRIGGER bodies, never as a statement
            # this module could execute.
            assert not text.strip().upper().startswith(statements), (
                f"{source}:{node.lineno} looks like a mutating statement: {text.strip()[:60]!r}"
            )


def test_no_ledger_exposes_mutation_or_deletion_vocabulary(tmp_path):
    forbidden = (
        "update", "delete", "remove", "truncate", "modify", "purge", "clear", "overwrite",
        "rewrite", "drop", "upsert", "replace",
    )

    def offenders(target):
        return [
            name
            for name in dir(target)
            if not name.startswith("_") and any(part in name.lower() for part in forbidden)
        ]

    assert offenders(TenantLedger) == []
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        assert offenders(ledger) == []
        assert offenders(ledger.for_tenant(TENANT_A)) == []


def test_a_record_handed_back_is_a_copy_not_the_stored_row(tmp_path):
    """Mutating a container inside a returned record must not reach storage."""
    for ledger in (InMemoryLedger(), SqliteLedger(root_dir=tmp_path)):
        tenant_ledger = ledger.for_tenant(TENANT_A)
        record = append_record(tenant_ledger)
        stored = canonical_json(tenant_ledger.get(record.decision_id))

        with pytest.raises(FROZEN_ERRORS):
            record.verdict = "REJECT"
        # In-place container mutation on the handle we were given: refused, or harmless. Never stored.
        try:
            record.reason_codes.append(next(iter(ReasonCode)))
        except Exception:  # noqa: BLE001 - a frozen container is an equally good outcome
            pass
        try:
            record.checks.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            record.library_versions["python"] = "tampered"
        except Exception:  # noqa: BLE001
            pass

        assert canonical_json(tenant_ledger.get(record.decision_id)) == stored
        assert tenant_ledger.verify_chain().ok


def test_every_schema_statement_is_idempotent(tmp_path):
    """Opening an existing ledger re-applies the schema; it must never fail and never change a row."""
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    append_record(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    before = all_rows(db_path)

    reopened = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A)
    assert all_rows(db_path) == before
    assert reopened.verify_chain().ok
    assert all("IF NOT EXISTS" in statement for statement in SCHEMA_STATEMENTS)
