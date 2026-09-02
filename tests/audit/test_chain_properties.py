"""Property tests for the pure verifier: it accepts every honest chain and refuses every altered one.

``verify_chain_records`` is the function a customer runs against their own copy of the ledger (A5), so
"it works on the examples we thought of" is not a good enough claim. These generate the alterations
instead: content changed without the hash, content changed WITH the hash, a broken link, a renumbered
record, a record removed. Each must be refused, and refused at the right sequence - "the chain is
broken" is far less useful than "the chain is broken here".
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizan.audit import InMemoryLedger, verify_chain_records
from mizan.contracts import DecisionRecord
from mizan.contracts.canonical import ZERO_HASH
from tests.audit._helpers import append_record
from tests.fixtures import TENANT_A

CHAIN_LENGTH = 6


def _build_chain(length: int) -> list[DecisionRecord]:
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    return [append_record(tenant_ledger) for _ in range(length)]


CHAIN: list[DecisionRecord] = _build_chain(CHAIN_LENGTH)


def _with(record: DecisionRecord, **fields: object) -> DecisionRecord:
    """A record whose fields were changed WITHOUT re-deriving anything. The forger's tool."""
    return DecisionRecord.model_construct(**{**dict(record), **fields})


def _rebuilt(record: DecisionRecord, **fields: object) -> DecisionRecord:
    """A record whose content changed and whose audit_hash was honestly recomputed for it."""
    payload = {k: v for k, v in record.model_dump(mode="json").items() if k != "audit_hash"}
    payload.update(fields)
    return DecisionRecord.build(**payload)


def _cases() -> list[tuple[str, list[DecisionRecord], int]]:
    """(label, altered chain, the sequence the verifier must name)."""
    cases: list[tuple[str, list[DecisionRecord], int]] = []
    for index in range(CHAIN_LENGTH):
        record = CHAIN[index]

        # 1. content changed, hash left alone -> caught on this record
        forged = _with(record, library_versions={**record.library_versions, "python": "tampered"})
        cases.append(("content-only", [*CHAIN[:index], forged, *CHAIN[index + 1 :]], record.sequence))

        # 2. content changed and the hash honestly recomputed -> the SUCCESSOR no longer links to it.
        #    The last record has no successor, which is exactly why the chain alone cannot protect its
        #    own tail: that is what the storage triggers and a periodic external anchor are for.
        if index < CHAIN_LENGTH - 1:
            relinked = _rebuilt(
                record, library_versions={**record.library_versions, "python": "tampered"}
            )
            cases.append(
                (
                    "content-and-hash",
                    [*CHAIN[:index], relinked, *CHAIN[index + 1 :]],
                    CHAIN[index + 1].sequence,
                )
            )

        # 3. the link is cut
        if index > 0:
            unlinked = _with(record, audit_prev_hash=ZERO_HASH)
            cases.append(("link-cut", [*CHAIN[:index], unlinked, *CHAIN[index + 1 :]], record.sequence))

        # 4. the record is renumbered. ``sequence`` is part of the hashed content, so this is caught by
        #    the hash check on the record itself - under its new, forged number.
        renumbered = _with(record, sequence=record.sequence + 100)
        cases.append(
            ("renumbered", [*CHAIN[:index], renumbered, *CHAIN[index + 1 :]], record.sequence + 100)
        )

        # 5. the record is removed
        if 0 < index < CHAIN_LENGTH - 1:
            cases.append(
                ("removed", [*CHAIN[:index], *CHAIN[index + 1 :]], CHAIN[index + 1].sequence)
            )
    return cases


CASES = _cases()

SETTINGS = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.data_too_large]
)


@SETTINGS
@given(length=st.integers(min_value=0, max_value=CHAIN_LENGTH))
def test_any_correctly_built_chain_verifies(length: int) -> None:
    result = verify_chain_records(CHAIN[:length])
    assert result.ok is True, result.detail
    assert result.length == length
    assert result.first_bad_sequence is None


@SETTINGS
@given(case=st.sampled_from(CASES))
def test_any_single_record_alteration_is_refused_at_the_right_sequence(case) -> None:
    label, altered, expected_sequence = case
    result = verify_chain_records(altered)
    assert result.ok is False, f"{label}: an altered chain verified"
    assert result.first_bad_sequence == expected_sequence, f"{label}: {result.detail}"
    assert result.detail


@SETTINGS
@given(start=st.integers(min_value=1, max_value=CHAIN_LENGTH - 1))
def test_a_chain_that_does_not_start_at_the_zero_hash_is_refused(start: int) -> None:
    """A chain handed over with its opening records removed is not a chain, it is an excerpt."""
    result = verify_chain_records(CHAIN[start:])
    assert result.ok is False
    assert result.first_bad_sequence == CHAIN[start].sequence


def test_the_untouched_chain_still_verifies_after_every_case_was_built() -> None:
    """Nothing above mutated the shared chain in place."""
    result = verify_chain_records(CHAIN)
    assert result.ok is True and result.length == CHAIN_LENGTH
