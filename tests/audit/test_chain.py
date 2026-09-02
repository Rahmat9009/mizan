"""The chain itself: how links are formed, how a break is found, and what the two kinds share.

Hard Rules A1 (a decision is reproducible from what was recorded) and A5 (a customer can verify the
chain without us). Addendum 1 section B.6 puts control events - response-level changes and kill-switch
flips - in the SAME per-tenant chain, so this file checks both kinds together: a chain that records
decisions but lets someone quietly change the response level beside them proves much less.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from mizan.audit import InMemoryLedger, SqliteLedger, verify_chain_records
from mizan.contracts.canonical import ZERO_HASH, canonical_json, record_hash_for
from mizan.contracts.errors import NotFound, ValidationFailed
from tests.audit._helpers import all_rows, append_record, drop_every_trigger
from tests.fixtures import FIXED_NOW, TENANT_A

HUMAN = {"type": "human", "id": "risk-officer-7"}
SYSTEM = {"type": "system", "id": "mizan-core"}


def _ledgers(tmp_path):
    return (InMemoryLedger(), SqliteLedger(root_dir=tmp_path))


def test_an_empty_chain_verifies(tmp_path):
    for ledger in _ledgers(tmp_path):
        verification = ledger.for_tenant(TENANT_A).verify_chain()
        assert verification.ok is True
        assert verification.length == 0
        assert verification.first_bad_sequence is None


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_one_hundred_appends_form_one_unbroken_chain(tmp_path, storage):
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)

    records = [append_record(tenant_ledger) for _ in range(100)]

    assert [record.sequence for record in records] == list(range(1, 101))
    assert len({record.decision_id for record in records}) == 100
    assert len({record.audit_hash for record in records}) == 100
    assert records[0].audit_prev_hash == ZERO_HASH
    for previous, current in zip(records, records[1:], strict=False):
        assert current.audit_prev_hash == previous.audit_hash

    for record in records:
        dump = record.model_dump(mode="json")
        assert record.audit_hash == record_hash_for(dump)

    verification = tenant_ledger.verify_chain()
    assert verification.ok is True, verification.detail
    assert verification.length == 100
    assert verification.first_bad_sequence is None

    # the pure verifier, given only the records, agrees
    assert verify_chain_records(tenant_ledger.chain_entries()).ok is True

    # and a fresh handle reads the same chain back out of storage, not out of a cache
    reopened = (ledger if storage == "memory" else SqliteLedger(root_dir=tmp_path)).for_tenant(TENANT_A)
    assert reopened.verify_chain().length == 100
    assert canonical_json(reopened.get(records[42].decision_id)) == canonical_json(records[42])


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_listing_is_newest_first_and_pages_backwards(tmp_path, storage):
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    records = [append_record(tenant_ledger) for _ in range(7)]

    newest = tenant_ledger.list(limit=3)
    assert [record.sequence for record in newest] == [7, 6, 5]

    older = tenant_ledger.list(limit=3, before_sequence=newest[-1].sequence)
    assert [record.sequence for record in older] == [4, 3, 2]

    assert [record.sequence for record in tenant_ledger.list(limit=50)] == list(range(7, 0, -1))
    assert tenant_ledger.list(limit=0) == []
    assert {record.decision_id for record in tenant_ledger.list(limit=50)} == {
        record.decision_id for record in records
    }


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_control_events_extend_the_same_chain_as_decisions(tmp_path, storage):
    """R-GRAD-2: a level change is a link of the chain, not a note beside it."""
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)

    first = append_record(tenant_ledger)
    escalation = tenant_ledger.append_control_event(
        event_type="response_level_changed",
        from_level=0,
        to_level=2,
        actor=SYSTEM,
        trigger_reason_codes=["RESPONSE_LEVEL_RESTRICTS_NEW_RISK"],
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW + timedelta(milliseconds=5),
    )
    second = append_record(tenant_ledger)

    assert escalation.sequence == 2
    assert escalation.audit_prev_hash == first.audit_hash
    assert second.sequence == 3
    assert second.audit_prev_hash == escalation.audit_hash

    entries = tenant_ledger.chain_entries()
    assert [entry.sequence for entry in entries] == [1, 2, 3]
    assert tenant_ledger.verify_chain().ok is True
    assert tenant_ledger.verify_chain().length == 3

    # a decision listing never shows control events, and vice versa
    assert [record.sequence for record in tenant_ledger.list(limit=50)] == [3, 1]
    events = tenant_ledger.list_control_events(limit=50)
    assert [event.event_id for event in events] == [escalation.event_id]

    # ... and a decision id is not a control event id
    with pytest.raises(NotFound):
        tenant_ledger.get(escalation.event_id)


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_downward_response_level_change_requires_a_human(tmp_path, storage):
    """R-GRAD-1: escalation may be automatic; de-escalation is a human decision, on the record."""
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    tenant_ledger.append_control_event(
        event_type="response_level_changed",
        from_level=0,
        to_level=3,
        actor=SYSTEM,
        trigger_reason_codes=["RESPONSE_LEVEL_RESTRICTS_NEW_RISK"],
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
    )

    with pytest.raises(ValidationFailed):
        tenant_ledger.append_control_event(
            event_type="response_level_changed",
            from_level=3,
            to_level=0,
            actor=SYSTEM,
            occurred_at=FIXED_NOW,
            recorded_at=FIXED_NOW,
        )
    assert len(tenant_ledger.chain_entries()) == 1, "the refused event must not have been written"

    stepped_down = tenant_ledger.append_control_event(
        event_type="response_level_changed",
        from_level=3,
        to_level=0,
        actor=HUMAN,
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
    )
    assert stepped_down.sequence == 2
    assert stepped_down.actor.type == "human"
    assert tenant_ledger.verify_chain().ok


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_refused_append_leaves_the_chain_exactly_as_it_was(tmp_path, storage):
    """Append is atomic: the head is read and the link written together, or neither happens."""
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    records = [append_record(tenant_ledger) for _ in range(2)]
    before = [canonical_json(entry) for entry in tenant_ledger.chain_entries()]

    with pytest.raises(Exception):  # noqa: B017 - any refusal will do; nothing may be written
        tenant_ledger.append_control_event(
            event_type="response_level_changed",
            from_level=None,
            to_level=None,
            actor=SYSTEM,
            occurred_at=FIXED_NOW,
            recorded_at=FIXED_NOW,
        )

    assert [canonical_json(entry) for entry in tenant_ledger.chain_entries()] == before
    assert tenant_ledger.verify_chain().ok
    third = append_record(tenant_ledger)
    assert third.sequence == 3
    assert third.audit_prev_hash == records[1].audit_hash


def test_tampering_with_a_stored_record_is_detected_at_its_sequence(tmp_path):
    """The triggers are the lock; the chain is the seal. Break the lock and the seal still shows."""
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    for _ in range(5):
        append_record(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    assert tenant_ledger.verify_chain().ok

    assert drop_every_trigger(db_path), "there must be triggers to drop"
    connection = sqlite3.connect(db_path)
    try:
        (raw,) = connection.execute(
            "SELECT record_json FROM decision_records WHERE sequence = 3"
        ).fetchone()
        payload = json.loads(raw)
        payload["library_versions"] = {**payload["library_versions"], "python": "tampered"}
        connection.execute(
            "UPDATE decision_records SET record_json = ? WHERE sequence = 3",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    verification = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain()
    assert verification.ok is False
    assert verification.first_bad_sequence == 3
    assert verification.length == 5, "the whole chain length is reported, not just what was verified"
    assert "audit_hash" in verification.detail


def test_tampering_with_a_stored_control_event_is_detected_too(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    append_record(tenant_ledger)
    tenant_ledger.append_control_event(
        event_type="response_level_changed",
        from_level=0,
        to_level=4,
        actor=SYSTEM,
        trigger_reason_codes=["RESPONSE_LEVEL_HALT"],
        occurred_at=FIXED_NOW,
        recorded_at=FIXED_NOW,
    )
    append_record(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"

    drop_every_trigger(db_path)
    connection = sqlite3.connect(db_path)
    try:
        (raw,) = connection.execute(
            "SELECT record_json FROM control_events WHERE sequence = 2"
        ).fetchone()
        payload = json.loads(raw)
        payload["to_level"] = 1  # "we were never at level 4"
        connection.execute(
            "UPDATE control_events SET record_json = ? WHERE sequence = 2",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    verification = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain()
    assert verification.ok is False
    assert verification.first_bad_sequence == 2
    assert verification.length == 3


def test_a_self_consistent_forgery_breaks_the_next_link(tmp_path):
    """Rewriting a record AND its hash does not help: the successor no longer points at it."""
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    for _ in range(4):
        append_record(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    drop_every_trigger(db_path)

    connection = sqlite3.connect(db_path)
    try:
        (raw,) = connection.execute(
            "SELECT record_json FROM decision_records WHERE sequence = 2"
        ).fetchone()
        payload = json.loads(raw)
        payload["library_versions"] = {**payload["library_versions"], "python": "tampered"}
        forged_hash = record_hash_for({k: v for k, v in payload.items() if k != "audit_hash"})
        payload["audit_hash"] = forged_hash
        connection.execute(
            "UPDATE decision_records SET record_json = ?, audit_hash = ? WHERE sequence = 2",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False), forged_hash),
        )
        connection.commit()
    finally:
        connection.close()

    verification = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain()
    assert verification.ok is False
    assert verification.first_bad_sequence == 3, verification.detail
    assert verification.length == 4


def test_unreadable_stored_content_is_reported_at_its_sequence(tmp_path):
    ledger = SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    for _ in range(3):
        append_record(tenant_ledger)
    db_path = tmp_path / f"{TENANT_A}.sqlite"
    drop_every_trigger(db_path)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE decision_records SET record_json = '{}' WHERE sequence = 2")
        connection.commit()
    finally:
        connection.close()

    verification = SqliteLedger(root_dir=tmp_path).for_tenant(TENANT_A).verify_chain()
    assert verification.ok is False
    assert verification.first_bad_sequence == 2
    assert verification.length == 3
    assert all_rows(db_path)  # the rows are still there; only their content is nonsense
