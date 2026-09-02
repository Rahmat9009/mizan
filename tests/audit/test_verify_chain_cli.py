"""Hard Rule A5: the customer verifies the chain without us.

``python -m mizan.audit.verify_chain <file>`` is a shipped product surface, so it is tested the way a
customer uses it - as a process, through ``subprocess``, reading its exit status and its output. If it
only worked when imported, it would not be the thing we are promising.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mizan.audit import SqliteLedger
from mizan.contracts.canonical import canonical_json
from tests.audit._helpers import append_record, drop_every_trigger
from tests.fixtures import FIXED_NOW, TENANT_A

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mizan.audit.verify_chain", *arguments],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _five(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    for _ in range(4):
        append_record(tenant_ledger)
    tenant_ledger.append_control_event(
        event_type="response_level_changed",
        from_level=0,
        to_level=1,
        actor={"type": "system", "id": "mizan-core"},
        trigger_reason_codes=["RESPONSE_LEVEL_RESTRICTS_NEW_RISK"],
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
    )
    return tenant_ledger, tmp_path / f"{TENANT_A}.sqlite"


def _tamper(db_path, sequence: int) -> None:
    drop_every_trigger(db_path)
    connection = sqlite3.connect(db_path)
    try:
        (raw,) = connection.execute(
            "SELECT record_json FROM decision_records WHERE sequence = ?", (sequence,)
        ).fetchone()
        payload = json.loads(raw)
        payload["library_versions"] = {**payload["library_versions"], "python": "tampered"}
        connection.execute(
            "UPDATE decision_records SET record_json = ? WHERE sequence = ?",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False), sequence),
        )
        connection.commit()
    finally:
        connection.close()


def test_the_cli_exits_zero_on_a_good_sqlite_chain(tmp_path):
    _tenant_ledger, db_path = _five(tmp_path)
    completed = run_cli(str(db_path))
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "CHAIN VERIFIED" in completed.stdout
    assert TENANT_A in completed.stdout
    assert "4 decision record(s), 1 control event(s)" in completed.stdout


def test_the_cli_exits_nonzero_on_a_tampered_chain_and_names_the_sequence(tmp_path):
    _tenant_ledger, db_path = _five(tmp_path)
    assert run_cli(str(db_path)).returncode == 0

    _tamper(db_path, 3)
    completed = run_cli(str(db_path))
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "CHAIN BROKEN at sequence 3" in output
    assert "3" in output


def test_the_cli_reads_a_json_lines_export(tmp_path):
    tenant_ledger, _db_path = _five(tmp_path)
    export = tmp_path / "tenant-a.jsonl"
    lines = [canonical_json(entry) for entry in tenant_ledger.chain_entries()]
    export.write_text("\n".join(lines) + "\n", encoding="utf-8")

    completed = run_cli(str(export))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "json-lines" in completed.stdout
    assert "CHAIN VERIFIED" in completed.stdout


def test_the_cli_detects_a_tampered_json_lines_export(tmp_path):
    tenant_ledger, _db_path = _five(tmp_path)
    entries = tenant_ledger.chain_entries()
    payloads = [entry.model_dump(mode="json") for entry in entries]
    payloads[1]["recorded_at"] = "2020-01-01T00:00:00.000000Z"
    export = tmp_path / "tenant-a.jsonl"
    export.write_text(
        "\n".join(json.dumps(p, separators=(",", ":"), sort_keys=True) for p in payloads) + "\n",
        encoding="utf-8",
    )

    completed = run_cli(str(export))
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "CHAIN BROKEN at sequence 2" in output


def test_the_cli_emits_machine_readable_json(tmp_path):
    _tenant_ledger, db_path = _five(tmp_path)
    completed = run_cli(str(db_path), "--json")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["links"] == 5
    assert payload["decision_records"] == 4
    assert payload["control_events"] == 1
    assert payload["tenants"] == [TENANT_A]
    assert payload["first_bad_sequence"] is None


def test_quiet_mode_says_everything_through_the_exit_status(tmp_path):
    _tenant_ledger, db_path = _five(tmp_path)
    good = run_cli(str(db_path), "--quiet")
    assert good.returncode == 0
    assert good.stdout == "" and good.stderr == ""

    _tamper(db_path, 2)
    bad = run_cli(str(db_path), "--quiet")
    assert bad.returncode == 1
    assert bad.stdout == "" and bad.stderr == ""


@pytest.mark.parametrize(
    "make_file",
    [
        pytest.param(lambda path: None, id="missing"),
        pytest.param(lambda path: path.write_bytes(b"not a ledger at all"), id="garbage"),
        pytest.param(lambda path: path.write_text("{ broken json\n", encoding="utf-8"), id="bad-json"),
    ],
)
def test_an_unreadable_file_exits_two_not_one(tmp_path, make_file):
    """A file we cannot read is not the same answer as a chain that failed to verify."""
    path = tmp_path / "candidate.bin"
    make_file(path)
    completed = run_cli(str(path))
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "verify_chain" in completed.stderr


def test_the_help_reads_like_a_product_surface():
    completed = run_cli("--help")
    assert completed.returncode == 0
    text = completed.stdout
    assert "decision replay" in text, "the term is 'decision replay', never bare 'replay'"
    assert "exit status" in text
    assert "sqlite" in text and "jsonl" in text
    assert "python -m mizan.audit.verify_chain" in text


def test_the_verifier_never_writes_to_the_file_it_verifies(tmp_path):
    _tenant_ledger, db_path = _five(tmp_path)
    before = db_path.read_bytes()
    assert run_cli(str(db_path)).returncode == 0
    assert db_path.read_bytes() == before
