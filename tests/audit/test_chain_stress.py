"""Adversarial probes against the chain seal, written after a bug of exactly this class got through.

The bug: verification re-serialised each record through the CURRENT contract before hashing it, so
adding one optional field silently rewrote every record ever written and the chain stopped verifying.
The stored bytes were never touched. The reader was wrong.

So these tests attack the reader, not the writer. Every one of them drops the storage triggers first -
the triggers are the lock and the chain is the seal, and the seal is what is under test here. A probe
that ends ok=True after a semantic change to the evidence is a finding, and two of the nine were:
duplicate keys (fixed) and tail truncation (cannot be fixed from inside the file; see below).
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from mizan.audit import SqliteLedger
from mizan.contracts.canonical import record_hash_for
from tests.audit._helpers import append_record, drop_every_trigger
from tests.fixtures import TENANT_A

LINKS = 6


@pytest.fixture
def chain(tmp_path: Path):
    """A real six-link ledger with the storage triggers removed, i.e. an attacker already inside."""
    tenant = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A)
    for _ in range(LINKS):
        append_record(tenant)
    db = tmp_path / f"{TENANT_A}.sqlite"
    assert SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain().ok
    assert drop_every_trigger(db), "there must be triggers to drop"
    return tmp_path, db


def _verify(root: Path):
    return SqliteLedger(root_dir=root).for_tenant(TENANT_A).verify_chain()


def _raw(db: Path, sequence: int) -> str:
    connection = sqlite3.connect(db)
    try:
        (text,) = connection.execute(
            "SELECT record_json FROM decision_records WHERE sequence = ?", (sequence,)
        ).fetchone()
        return str(text)
    finally:
        connection.close()


def _write(db: Path, sequence: int, text: str) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE decision_records SET record_json = ? WHERE sequence = ?", (text, sequence)
        )
        connection.commit()
    finally:
        connection.close()


def _execute(db: Path, statement: str, args: tuple = ()) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute(statement, args)
        connection.commit()
    finally:
        connection.close()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


# -- the reader must accept what is only cosmetically different -------------------------------------


def test_reformatting_the_stored_json_is_not_tampering(chain):
    """The hash is over the CANONICAL form, so indentation and key order carry no meaning.

    This is the control for every test below it: if reformatting failed, the suite would be detecting
    serialisation noise rather than tampering, and every other result here would be worthless.
    """
    root, db = chain
    payload = json.loads(_raw(db, 3))
    _write(db, 3, json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False))

    assert _verify(root).ok is True


# -- the reader must reject anything that changes what the record SAYS -------------------------------


def test_a_duplicate_key_is_refused_because_two_verifiers_would_read_it_differently(chain):
    """JSON does not define what a repeated key means, and that ambiguity was exploitable.

    Python, Go and jq keep the LAST occurrence; other conforming parsers keep the first. So bytes
    carrying `"verdict"` twice are a record whose content depends on who reads it - and since we hash
    what we parsed, the hash matched and the chain verified. The record read APPROVE to us and REJECT
    to a customer running their own verifier, with both of us reporting an intact chain.

    That defeats the actual promise, which is not "our tool says this is fine" but "check it yourself
    and get the same answer".
    """
    root, db = chain
    text = _canonical(json.loads(_raw(db, 3)))
    _write(db, 3, text.replace('{"', '{"verdict":"REJECT","', 1))

    result = _verify(root)
    assert result.ok is False
    assert result.first_bad_sequence == 3


def test_retyping_a_number_is_detected(chain):
    root, db = chain
    _write(db, 3, _canonical(json.loads(_raw(db, 3))).replace('"sequence":3', '"sequence":3.0', 1))

    assert _verify(root).ok is False


def test_a_unicode_decomposed_field_is_detected(chain):
    """NFD and NFC render identically and hash differently; the record must not accept both."""
    root, db = chain
    payload = json.loads(_raw(db, 3))
    payload["tenant_id"] = unicodedata.normalize("NFD", payload["tenant_id"] + "é")
    _write(db, 3, _canonical(payload))

    assert _verify(root).ok is False


def test_an_unknown_field_smuggled_into_a_record_is_detected(chain):
    root, db = chain
    _write(db, 3, _canonical({**json.loads(_raw(db, 3)), "note": "approved by phone"}))

    result = _verify(root)
    assert result.ok is False
    assert result.first_bad_sequence == 3


def test_deleting_a_record_from_the_middle_leaves_a_sequence_gap(chain):
    root, db = chain
    _execute(db, "DELETE FROM decision_records WHERE sequence = ?", (3,))

    result = _verify(root)
    assert result.ok is False
    assert "sequence gap" in result.detail


def test_two_records_cannot_claim_the_same_predecessor(chain):
    """A fork is how you keep one version of history for the auditor and another for yourself."""
    root, db = chain
    fourth, fifth = json.loads(_raw(db, 4)), json.loads(_raw(db, 5))
    fifth["audit_prev_hash"] = fourth["audit_prev_hash"]
    _write(db, 5, _canonical(fifth))

    result = _verify(root)
    assert result.ok is False
    assert result.first_bad_sequence == 5


# -- the one attack the chain cannot see, and the anchor that can ------------------------------------


def test_truncating_the_tail_leaves_a_chain_that_still_verifies(chain):
    """A KNOWN LIMITATION, pinned here so that improving it is a deliberate act rather than an accident.

    Delete the last two records and every remaining hash still chains perfectly - because the evidence
    that those records existed is exactly what was deleted. No arrangement of data inside the file
    fixes this: whoever can delete records can delete a counter beside them just as easily.

    It is not a defect in the hash chain so much as the boundary of what a hash chain is FOR, and the
    honest response is to say so and provide the anchor, not to imply a completeness guarantee that
    nothing in the file could support.
    """
    root, db = chain
    _execute(db, "DELETE FROM decision_records WHERE sequence > ?", (LINKS - 2,))

    result = _verify(root)
    assert result.ok is True, "if this ever fails, the limitation was fixed - update the docs"
    assert result.length == LINKS - 2


def test_the_head_is_reported_so_truncation_can_be_caught_from_outside(chain):
    """The mitigation: the customer keeps the head, and a shortened chain no longer matches it.

    Certificate Transparency publishes a signed tree head rather than letting the log describe itself.
    This is the small version of the same idea.
    """
    root, db = chain
    before = _verify(root)
    assert before.head_sequence == LINKS
    assert before.head_hash

    # Opening the ledger put the append-only triggers back (see the test below), so the attacker has
    # to drop them a second time. That is the lock re-arming itself, not a quirk of the fixture.
    assert drop_every_trigger(db)
    _execute(db, "DELETE FROM decision_records WHERE sequence > ?", (LINKS - 2,))
    after = _verify(root)

    assert after.ok is True, "internally consistent, which is the whole problem"
    assert after.head_hash != before.head_hash, (
        "the head must move when records are removed, or the anchor could not detect it"
    )
    assert after.head_sequence == LINKS - 2


def test_an_empty_ledger_and_a_deleted_one_are_indistinguishable(chain):
    """The same limitation at its limit: 'nothing was ever written' and 'everything was removed' are
    the same six bytes on disk. Only an anchor held elsewhere separates them."""
    root, db = chain
    _execute(db, "DELETE FROM decision_records")

    result = _verify(root)
    assert result.ok is True
    assert result.length == 0
    assert result.head_hash is None


def test_opening_the_ledger_puts_the_append_only_triggers_back(chain):
    """Dropping the triggers is not a durable win: the next open re-arms them.

    Found by a stress test failing for the right reason - a delete that had worked moments earlier was
    refused, because verifying the chain in between had reopened the ledger. An attacker must therefore
    hold the file open and un-triggered for the whole operation rather than disarming it once.
    """
    root, db = chain
    assert _triggers(db) == [], "the fixture drops them"

    _verify(root)

    assert _triggers(db), "reopening the ledger must restore the append-only guard"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _execute(db, "DELETE FROM decision_records WHERE sequence = 3")


def _triggers(db: Path) -> list[str]:
    connection = sqlite3.connect(db)
    try:
        return [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ]
    finally:
        connection.close()


# -- the head record: the one position nothing links to ---------------------------------------------


def _forge_head(db: Path, mutate) -> None:
    """Rewrite the last record and recompute its OWN audit_hash, so it is internally consistent."""
    payload = json.loads(_raw(db, LINKS))
    mutate(payload)
    payload["audit_hash"] = record_hash_for(
        {key: value for key, value in payload.items() if key != "audit_hash"}
    )
    _write(db, LINKS, _canonical(payload))


def test_forging_a_field_bound_by_another_derived_hash_is_still_caught(chain):
    """Changing the verdict fails even on the head, because verdict_hash independently covers it.

    Worth knowing before reading the next test: the record's other derived hashes are doing real work,
    and the exposure below is narrower than "the head record can say anything".
    """
    root, db = chain
    _forge_head(db, lambda p: p.update(verdict="REJECT" if p["verdict"] != "REJECT" else "APPROVE"))

    assert _verify(root).ok is False


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("backdate when the decision was recorded",
         lambda p: p.update(recorded_at="2099-01-01T00:00:00.000000Z")),
        ("rewrite what the agent said it was doing",
         lambda p: p["proposal"].update(reasoning="because the CEO said so")),
        ("rewrite the library versions the decision ran on",
         lambda p: p.update(library_versions={**p["library_versions"], "python": "9.9.9"})),
    ],
)
def test_forging_the_head_record_is_not_caught_by_the_chain(chain, label, mutate):
    """A KNOWN LIMITATION, and the same one as truncation wearing a different hat.

    Every record is protected by the record AFTER it, which carries its hash. The last record has no
    record after it, so a forgery there - recomputing its own audit_hash so it stays self-consistent -
    leaves a chain that verifies. Backdating `recorded_at` on the head is the version of this that
    matters: it changes WHEN a decision was made, and nothing in the file contradicts it.

    Fields covered by another derived hash (the verdict, via verdict_hash) survive this; the ones
    tested here do not. The mitigation is the same anchor truncation needs, for the same reason - the
    head is the position no internal structure can defend, so the defence has to be external.
    """
    root, db = chain
    _forge_head(db, mutate)

    assert _verify(root).ok is True, f"if this fails, {label} became detectable - update the docs"


def test_the_head_anchor_catches_a_forged_head(chain):
    """Which is why the head is printed on every successful verification and --expect-head exists."""
    root, db = chain
    before = _verify(root)

    assert drop_every_trigger(db)
    _forge_head(db, lambda p: p.update(recorded_at="2099-01-01T00:00:00.000000Z"))
    after = _verify(root)

    assert after.ok is True, "internally consistent, which is the whole problem"
    assert after.head_hash != before.head_hash, (
        "a forged head must move the head hash, or the anchor could not detect it"
    )
