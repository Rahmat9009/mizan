"""L2 - the append-only, hash-chained decision ledger.

Hard Rules A2 (no update path, no delete path, at any privilege level - enforced at the storage layer),
A3 (credentials, secrets and headers redacted recursively before persistence), A5 (chain integrity
independently verifiable without Mizan's involvement) and B3 (cross-tenant access impossible by
construction - one chain, and one storage unit, per tenant).

:func:`verify_chain_records` is the customer's offline verifier: it depends on nothing but the contracts,
so a customer can re-derive every hash themselves and never has to take our word for the chain. The
shipped command line around it is :mod:`mizan.audit.verify_chain`.

One tenant's chain carries two kinds of link, merged by ``sequence``: :class:`DecisionRecord` (what the
engine decided) and :class:`ControlEvent` (a graduated-response level change or a kill-switch flip,
Addendum 1 section B.6). Both hash the same way and both extend the same chain, so a control action can
never be slipped in beside the decisions whose meaning it changed.

There is no update method and no delete method here, private or otherwise, and no INSERT in this module
carries an ON CONFLICT clause: an upsert is an update wearing a disguise. Storage refuses mutation as
well - the SQLite schema carries BEFORE UPDATE and BEFORE DELETE triggers, so a raw connection holding
full privileges is refused by the database rather than by a Python guard it could simply bypass.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError

from mizan.contracts import (
    Actor,
    ContractModel,
    ControlEvent,
    ControlEventType,
    DecisionRecord,
    ExecutionAuthorization,
    ExecutionResult,
    GovernorDecision,
    Policy,
    PolicyRef,
    ReasonCode,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)
from mizan.contracts._base import SKIP_HASH_CHECK
from mizan.contracts.canonical import (
    REDACTED,
    ZERO_HASH,
    canonical_json,
    library_versions,
    record_hash_for,
    redact,
    uuid7,
)
from mizan.contracts.errors import (
    ChainIntegrityError,
    LedgerError,
    NotFound,
    TenantForbidden,
    ValidationFailed,
)
from mizan.contracts.types import format_ts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime
    from pathlib import Path

__all__ = [
    "CHAIN_ROWS_VIEW_DDL",
    "CONTROL_EVENTS_DDL",
    "DECISION_RECORDS_DDL",
    "LEDGER_META_DDL",
    "SCHEMA_STATEMENTS",
    "STRUCTURAL_SECTION_KEYS",
    "TENANT_ID_RE",
    "ChainEntry",
    "ChainVerification",
    "InMemoryLedger",
    "InMemoryTenantLedger",
    "Ledger",
    "SqliteLedger",
    "SqliteTenantLedger",
    "TenantLedger",
    "append_only_triggers",
    "load_chain_entry",
    "redact_for_persistence",
    "validate_tenant_id",
    "verify_chain_records",
    "verify_stored_rows",
]

#: A tenant id is also a filename and a schema name, so it is validated before it is either.
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: A link of the chain: a decision, or the control action that changed the rules decisions run under.
ChainEntry = DecisionRecord | ControlEvent


def validate_tenant_id(tenant_id: str) -> str:
    """Return ``tenant_id`` if it is safe to use as a storage identifier, else raise ``ValueError``.

    Path separators, traversal, uppercase and empty strings are all refused. A tenant id reaches the
    filesystem and the database, so it is checked before it reaches either.
    """
    if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")
    return tenant_id


class ChainVerification(ContractModel):
    """The result of verifying a hash chain."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ok: bool
    length: int = Field(ge=0)
    first_bad_sequence: int | None = None
    detail: str = ""


def verify_chain_records(records: Iterable[DecisionRecord]) -> ChainVerification:
    """Verify a decision chain from the records alone. Pure; no storage, no clock, no Mizan services.

    Three things must hold for every record: its ``audit_hash`` is the hash of its own canonical content,
    its ``sequence`` is one more than its predecessor's, and its ``audit_prev_hash`` is its predecessor's
    ``audit_hash`` (the zero hash for the first). The first record that breaks any of them is reported by
    sequence number, because "the chain is broken" is far less useful than "the chain is broken here".
    """
    previous: DecisionRecord | None = None
    count = 0

    for record in records:
        count += 1
        payload = record.model_dump(mode="json")
        payload.pop("audit_hash", None)
        expected = record_hash_for(payload)
        if record.audit_hash != expected:
            return ChainVerification(
                ok=False,
                length=count,
                first_bad_sequence=record.sequence,
                detail=(
                    f"record {record.sequence} content does not match its audit_hash "
                    f"(recomputed {expected[:12]}..., stored {record.audit_hash[:12]}...)"
                ),
            )

        if previous is None:
            if record.audit_prev_hash != ZERO_HASH:
                return ChainVerification(
                    ok=False,
                    length=count,
                    first_bad_sequence=record.sequence,
                    detail=f"first record {record.sequence} does not start from the zero hash",
                )
        else:
            if record.sequence != previous.sequence + 1:
                return ChainVerification(
                    ok=False,
                    length=count,
                    first_bad_sequence=record.sequence,
                    detail=(
                        f"sequence gap: record {record.sequence} follows {previous.sequence}"
                    ),
                )
            if record.audit_prev_hash != previous.audit_hash:
                return ChainVerification(
                    ok=False,
                    length=count,
                    first_bad_sequence=record.sequence,
                    detail=(
                        f"record {record.sequence} does not link to record {previous.sequence}"
                    ),
                )
        previous = record

    return ChainVerification(ok=True, length=count, detail=f"{count} record(s) verified")


# ---------------------------------------------------------------------------------------------------
# Redaction before persistence (Hard Rule A3, security finding F-7)
# ---------------------------------------------------------------------------------------------------

#: Contract sections whose *name* matches a sensitive key pattern but which carry structure, not a
#: secret. ``Policy.authorization`` is the authorization-TTL section (``{"ttl_seconds": 15}``); it is a
#: nested contract model with no ``schema_version`` of its own, so ``canonical.redact`` cannot tell it
#: apart from an ``Authorization:`` header and replaces the whole section - which would destroy the
#: policy hash and make the record unbuildable. The ledger recurses into these keys instead, and every
#: scalar inside them is still redacted. Raised for L0 in ledger/requests.md.
STRUCTURAL_SECTION_KEYS: frozenset[str] = frozenset({"authorization"})


def _restore_structural_sections(source: Any, target: Any) -> None:
    """Put back the *structure* of a contract section that redaction replaced wholesale."""
    if isinstance(source, Mapping) and isinstance(target, dict):
        for key, value in source.items():
            if (
                key in STRUCTURAL_SECTION_KEYS
                and isinstance(value, (Mapping, list))
                and target.get(key) == REDACTED
            ):
                target[key] = redact(value)
            else:
                _restore_structural_sections(value, target.get(key))
    elif isinstance(source, list) and isinstance(target, list):
        for item, mirrored in zip(source, target, strict=False):
            _restore_structural_sections(item, mirrored)


def redact_for_persistence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``canonical.redact`` over everything about to be written, keeping contract structure intact.

    Applied to the WHOLE record, once, before the hash is computed (A3, F-7): the stored bytes and the
    ``audit_hash`` therefore cover the same redacted content, so a verifier re-hashing what it was given
    agrees with us and never has to be shown a credential to do it. Redacting per record *kind* - the
    legacy mistake - leaves whichever kind was forgotten (there, the verbatim model output) in the clear.

    ``payload`` must already be plain JSON data (every contract object dumped with ``mode="json"``), so
    that redaction reaches every nested credential, header collection and mixed-case key rather than
    stopping at the first pydantic model.
    """
    redacted = redact(dict(payload))
    _restore_structural_sections(payload, redacted)
    return redacted


# ---------------------------------------------------------------------------------------------------
# Building the two kinds of chain link
# ---------------------------------------------------------------------------------------------------


def _code_value(code: ReasonCode | str) -> str:
    return str(code.value) if isinstance(code, Enum) else str(code)


def _decision_record(
    *,
    tenant_id: str,
    sequence: int,
    audit_prev_hash: str,
    proposal: TradeProposal,
    risk_context: RiskContext,
    risk_evaluation: RiskEvaluation,
    governor_decision: GovernorDecision,
    policy_snapshot: Policy,
    authorization: ExecutionAuthorization | None,
    execution: ExecutionResult | None,
    recorded_at: datetime,
) -> DecisionRecord:
    """Assemble, redact and build one chain link. The contract verifies the hash on construction."""
    advisory = governor_decision.llm_advisory
    payload: dict[str, Any] = {
        "decision_id": governor_decision.decision_id,
        "sequence": sequence,
        "tenant_id": tenant_id,
        "agent_id": governor_decision.agent_id,
        "proposal_id": governor_decision.proposal_id,
        "engine_version": governor_decision.engine_version,
        "library_versions": library_versions(),
        "policy": policy_snapshot.ref.model_dump(mode="json"),
        "policy_snapshot": policy_snapshot.model_dump(mode="json"),
        "decision_timestamp": governor_decision.decision_timestamp,
        "verdict": governor_decision.verdict,
        "reason_codes": [_code_value(code) for code in governor_decision.reason_codes],
        "checks": [check.model_dump(mode="json") for check in risk_evaluation.checks],
        "proposal": proposal.model_dump(mode="json"),
        "risk_context": risk_context.model_dump(mode="json"),
        "risk_evaluation": risk_evaluation.model_dump(mode="json"),
        "governor_decision": governor_decision.model_dump(mode="json"),
        "authorization": None if authorization is None else authorization.model_dump(mode="json"),
        "execution": None if execution is None else execution.model_dump(mode="json"),
        "original": governor_decision.original.model_dump(mode="json"),
        "authorized": governor_decision.authorized.model_dump(mode="json"),
        "llm_advisory": None if advisory is None else advisory.model_dump(mode="json"),
        "recorded_at": format_ts(recorded_at),
        "audit_prev_hash": audit_prev_hash,
    }
    return DecisionRecord.build(**redact_for_persistence(payload))


def _control_event(
    *,
    tenant_id: str,
    sequence: int,
    audit_prev_hash: str,
    event_id: str,
    event_type: ControlEventType,
    from_level: int | None,
    to_level: int | None,
    actor: Actor | Mapping[str, str],
    trigger_reason_codes: Sequence[ReasonCode | str],
    policy: Policy | PolicyRef | None,
    occurred_at: datetime,
    recorded_at: datetime,
) -> ControlEvent:
    """Assemble, redact and build one control link of the same per-tenant chain."""
    reference = policy.ref if isinstance(policy, Policy) else policy
    payload: dict[str, Any] = {
        "event_id": event_id,
        "sequence": sequence,
        "tenant_id": tenant_id,
        "event_type": event_type,
        "from_level": from_level,
        "to_level": to_level,
        "actor": actor.model_dump(mode="json") if isinstance(actor, Actor) else dict(actor),
        "trigger_reason_codes": [_code_value(code) for code in trigger_reason_codes],
        "policy": None if reference is None else reference.model_dump(mode="json"),
        "occurred_at": format_ts(occurred_at),
        "recorded_at": format_ts(recorded_at),
        "audit_prev_hash": audit_prev_hash,
    }
    return ControlEvent.build(**redact_for_persistence(payload))


def load_chain_entry(payload: Mapping[str, Any]) -> ChainEntry:
    """Rebuild a stored chain link for verification, WITHOUT trusting its recorded hashes.

    A tampered record cannot pass the contract's own hash check, so it could never be loaded through
    plain validation - and a verifier that cannot load a tampered record cannot report where the chain
    broke. This loads it with the derived-hash checks suspended, keeping the stored ``audit_hash``
    exactly as written, and leaves the recomputation to :func:`verify_chain_records`.
    """
    model: type[ChainEntry] = ControlEvent if "event_id" in payload else DecisionRecord
    try:
        return model.model_validate(dict(payload))
    except ValidationError:
        return model.model_validate(dict(payload), context={SKIP_HASH_CHECK: True})


def _chain_verification(
    entries: Sequence[ChainEntry], total: int, unreadable: tuple[int, str] | None
) -> ChainVerification:
    """``verify_chain_records`` over the loaded links, reported against the whole stored chain length."""
    result = verify_chain_records(entries)
    if not result.ok:
        return ChainVerification(
            ok=False, length=total, first_bad_sequence=result.first_bad_sequence, detail=result.detail
        )
    if unreadable is not None:
        return ChainVerification(
            ok=False, length=total, first_bad_sequence=unreadable[0], detail=unreadable[1]
        )
    return ChainVerification(ok=True, length=total, detail=f"{total} record(s) verified")


def verify_stored_rows(rows: Sequence[tuple[int, str]]) -> ChainVerification:
    """Verify a chain held as ``(sequence, record_json)`` rows in sequence order.

    Shared by both ledgers and by the offline command line, so storage and the customer's own verifier
    can never disagree about what "verified" means.
    """
    entries: list[ChainEntry] = []
    unreadable: tuple[int, str] | None = None
    for sequence, text in rows:
        try:
            entries.append(load_chain_entry(json.loads(text)))
        except (ValidationError, ValueError) as exc:
            unreadable = (
                sequence,
                f"record {sequence} is not a readable chain link ({type(exc).__name__})",
            )
            break
    return _chain_verification(entries, len(rows), unreadable)


# ---------------------------------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------------------------------


@runtime_checkable
class TenantLedger(Protocol):
    """One tenant's chain. There is no update method and no delete method, by construction (A2)."""

    tenant_id: str

    def append(
        self,
        *,
        proposal: TradeProposal,
        risk_context: RiskContext,
        risk_evaluation: RiskEvaluation,
        governor_decision: GovernorDecision,
        policy_snapshot: Policy,
        authorization: ExecutionAuthorization | None = None,
        execution: ExecutionResult | None = None,
        recorded_at: datetime,
    ) -> DecisionRecord: ...

    def append_control_event(
        self,
        *,
        event_type: ControlEventType,
        actor: Actor | Mapping[str, str],
        occurred_at: datetime,
        recorded_at: datetime,
        from_level: int | None = None,
        to_level: int | None = None,
        trigger_reason_codes: Sequence[ReasonCode | str] = (),
        policy: Policy | PolicyRef | None = None,
    ) -> ControlEvent: ...

    def get(self, decision_id: str) -> DecisionRecord: ...

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]: ...

    def list_control_events(
        self, *, limit: int = 50, before_sequence: int | None = None
    ) -> list[ControlEvent]: ...

    def chain_entries(self) -> list[ChainEntry]: ...

    def verify_chain(self) -> ChainVerification: ...


@runtime_checkable
class Ledger(Protocol):
    def for_tenant(self, tenant_id: str) -> TenantLedger: ...


# ---------------------------------------------------------------------------------------------------
# Shared ledger body
# ---------------------------------------------------------------------------------------------------

_DECISION_KIND = "decision"
_CONTROL_KIND = "control"


class _StoredRow(NamedTuple):
    sequence: int
    kind: str
    entry_id: str
    record_json: str


class _TenantLedgerBase:
    """Everything both storages share: tenant binding, link assembly, chain reporting.

    Carries no mutation vocabulary, so introspection finds none (invariants 08 and 09).
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = validate_tenant_id(tenant_id)

    # -- tenant boundary (B3) ---------------------------------------------------------------------
    def _own(self, **owners: str) -> None:
        for name, tenant_id in owners.items():
            if tenant_id != self.tenant_id:
                raise TenantForbidden(
                    detail=(
                        f"{name} belongs to tenant {tenant_id!r}; this ledger holds tenant "
                        f"{self.tenant_id!r} and no other tenant's chain"
                    )
                )

    # -- writes -----------------------------------------------------------------------------------
    def append(
        self,
        *,
        proposal: TradeProposal,
        risk_context: RiskContext,
        risk_evaluation: RiskEvaluation,
        governor_decision: GovernorDecision,
        policy_snapshot: Policy,
        authorization: ExecutionAuthorization | None = None,
        execution: ExecutionResult | None = None,
        recorded_at: datetime,
    ) -> DecisionRecord:
        """Append one decision: ``sequence = last + 1``, ``audit_prev_hash = last.audit_hash or ZERO_HASH``.

        Everything persisted is redacted first (A3) and the record is then built through
        ``DecisionRecord.build``, so the contract - not this module - verifies the hash. Reading the
        chain head and writing the new link are one atomic step; a refused append leaves the chain
        exactly as it was. A record belonging to another tenant is refused outright (B3).
        """
        self._own(
            governor_decision=governor_decision.tenant_id,
            risk_context=risk_context.tenant_id,
            risk_evaluation=risk_evaluation.tenant_id,
            policy_snapshot=policy_snapshot.tenant_id,
        )
        if authorization is not None:
            self._own(authorization=authorization.tenant_id)
        if execution is not None:
            self._own(execution=execution.tenant_id)

        def build(sequence: int, audit_prev_hash: str) -> DecisionRecord:
            return _decision_record(
                tenant_id=self.tenant_id,
                sequence=sequence,
                audit_prev_hash=audit_prev_hash,
                proposal=proposal,
                risk_context=risk_context,
                risk_evaluation=risk_evaluation,
                governor_decision=governor_decision,
                policy_snapshot=policy_snapshot,
                authorization=authorization,
                execution=execution,
                recorded_at=recorded_at,
            )

        return self._commit_decision(build)

    def append_control_event(
        self,
        *,
        event_type: ControlEventType,
        actor: Actor | Mapping[str, str],
        occurred_at: datetime,
        recorded_at: datetime,
        from_level: int | None = None,
        to_level: int | None = None,
        trigger_reason_codes: Sequence[ReasonCode | str] = (),
        policy: Policy | PolicyRef | None = None,
    ) -> ControlEvent:
        """Append a control action to the SAME chain (Addendum 1 section B.6, R-GRAD-2).

        ``audit_prev_hash`` links to whatever came last, of either kind, so a level change or a
        kill-switch flip cannot be slipped in among the decisions it governs. A DOWNWARD response-level
        change is refused unless the actor is human (R-GRAD-1).
        """
        actor_type = actor.type if isinstance(actor, Actor) else str(actor.get("type", ""))
        if (
            event_type == "response_level_changed"
            and from_level is not None
            and to_level is not None
            and to_level < from_level
            and actor_type != "human"
        ):
            raise ValidationFailed(
                detail=(
                    f"de-escalation from response level {from_level} to {to_level} requires a human "
                    f"actor (R-GRAD-1); the actor type was {actor_type!r}"
                )
            )
        event_id = uuid7()

        def build(sequence: int, audit_prev_hash: str) -> ControlEvent:
            return _control_event(
                tenant_id=self.tenant_id,
                sequence=sequence,
                audit_prev_hash=audit_prev_hash,
                event_id=event_id,
                event_type=event_type,
                from_level=from_level,
                to_level=to_level,
                actor=actor,
                trigger_reason_codes=trigger_reason_codes,
                policy=policy,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )

        return self._commit_control_event(build)

    # -- storage hooks ----------------------------------------------------------------------------
    def _commit_decision(self, build: Callable[[int, str], DecisionRecord]) -> DecisionRecord:
        raise NotImplementedError  # pragma: no cover - abstract

    def _commit_control_event(self, build: Callable[[int, str], ControlEvent]) -> ControlEvent:
        raise NotImplementedError  # pragma: no cover - abstract


def _decision_from_json(text: str, *, sequence_hint: int | None = None) -> DecisionRecord:
    """Validate a stored decision in full. A record that fails the contract is never handed back."""
    try:
        record = DecisionRecord.model_validate(json.loads(text))
    except (ValidationError, ValueError) as exc:
        where = "" if sequence_hint is None else f" at sequence {sequence_hint}"
        raise ChainIntegrityError(
            detail=f"the stored record{where} no longer satisfies its own contract: {exc}"
        ) from exc
    return record


def _control_event_from_json(text: str) -> ControlEvent:
    try:
        return ControlEvent.model_validate(json.loads(text))
    except (ValidationError, ValueError) as exc:
        raise ChainIntegrityError(
            detail=f"the stored control event no longer satisfies its own contract: {exc}"
        ) from exc


# ---------------------------------------------------------------------------------------------------
# In-memory ledger
# ---------------------------------------------------------------------------------------------------


class InMemoryTenantLedger(_TenantLedgerBase):
    """One tenant's in-memory chain.

    Links are held as canonical JSON text, not as live objects: a caller who mutates a list inside a
    record it was handed changes its own copy and nothing else. Reads rebuild from the stored text.
    """

    def __init__(self, tenant_id: str) -> None:
        super().__init__(tenant_id)
        self._lock = threading.Lock()
        self._rows: list[_StoredRow] = []
        self._index: dict[str, int] = {}
        self._head_sequence = 0
        self._head_hash = ZERO_HASH

    # -- writes -----------------------------------------------------------------------------------
    def _commit_decision(self, build: Callable[[int, str], DecisionRecord]) -> DecisionRecord:
        with self._lock:
            record = build(self._head_sequence + 1, self._head_hash)
            if record.decision_id in self._index:
                raise LedgerError(detail=f"decision {record.decision_id} is already in this chain")
            self._store(
                _StoredRow(record.sequence, _DECISION_KIND, record.decision_id, canonical_json(record))
            )
        return record

    def _commit_control_event(self, build: Callable[[int, str], ControlEvent]) -> ControlEvent:
        with self._lock:
            event = build(self._head_sequence + 1, self._head_hash)
            self._store(_StoredRow(event.sequence, _CONTROL_KIND, event.event_id, canonical_json(event)))
        return event

    def _store(self, row: _StoredRow) -> None:
        self._index[row.entry_id] = len(self._rows)
        self._rows.append(row)
        self._head_sequence = row.sequence
        self._head_hash = json.loads(row.record_json)["audit_hash"]

    # -- reads ------------------------------------------------------------------------------------
    def get(self, decision_id: str) -> DecisionRecord:
        """The decision with this id. ``NotFound`` for an unknown id and for another tenant's id alike.

        Another tenant's decision is simply not in this chain, so it is indistinguishable from one that
        never existed - which is the point: existence must not leak across the tenant boundary (B3).
        """
        with self._lock:
            position = self._index.get(decision_id)
            row = self._rows[position] if position is not None else None
        if row is None or row.kind != _DECISION_KIND:
            raise NotFound(detail=f"no decision {decision_id!r} in tenant {self.tenant_id!r}")
        return _decision_from_json(row.record_json, sequence_hint=row.sequence)

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]:
        """Up to ``limit`` decisions, NEWEST FIRST, strictly before ``before_sequence`` when given."""
        rows = self._select(_DECISION_KIND, limit=limit, before_sequence=before_sequence)
        return [_decision_from_json(row.record_json, sequence_hint=row.sequence) for row in rows]

    def list_control_events(
        self, *, limit: int = 50, before_sequence: int | None = None
    ) -> list[ControlEvent]:
        """Up to ``limit`` control events, NEWEST FIRST, strictly before ``before_sequence``."""
        rows = self._select(_CONTROL_KIND, limit=limit, before_sequence=before_sequence)
        return [_control_event_from_json(row.record_json) for row in rows]

    def _select(self, kind: str, *, limit: int, before_sequence: int | None) -> list[_StoredRow]:
        if limit < 0:
            raise ValidationFailed(detail="limit must not be negative")
        with self._lock:
            rows = [row for row in self._rows if row.kind == kind]
        if before_sequence is not None:
            rows = [row for row in rows if row.sequence < before_sequence]
        rows.reverse()
        return rows[:limit]

    def chain_entries(self) -> list[ChainEntry]:
        """Every link of this tenant's chain - decisions and control events - in sequence order."""
        with self._lock:
            rows = list(self._rows)
        return [load_chain_entry(json.loads(row.record_json)) for row in rows]

    def verify_chain(self) -> ChainVerification:
        """Re-derive every hash and every link from what is stored, decisions and control events alike."""
        with self._lock:
            rows = [(row.sequence, row.record_json) for row in self._rows]
        return verify_stored_rows(rows)


class InMemoryLedger:
    """Per-tenant in-memory chains. Separate objects, never a filtered shared list (B3)."""

    def __init__(self) -> None:
        self._tenants: dict[str, InMemoryTenantLedger] = {}

    def for_tenant(self, tenant_id: str) -> InMemoryTenantLedger:
        validate_tenant_id(tenant_id)
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = InMemoryTenantLedger(tenant_id)
        return self._tenants[tenant_id]


# ---------------------------------------------------------------------------------------------------
# SQLite schema: append-only at the DATABASE level (Hard Rule A2, security finding F-5)
# ---------------------------------------------------------------------------------------------------

LEDGER_META_DDL = """
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

DECISION_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS decision_records (
    sequence        INTEGER PRIMARY KEY,
    decision_id     TEXT NOT NULL UNIQUE,
    audit_prev_hash TEXT NOT NULL,
    audit_hash      TEXT NOT NULL UNIQUE,
    tenant_id       TEXT NOT NULL,
    record_json     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
)
"""

CONTROL_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS control_events (
    sequence        INTEGER PRIMARY KEY,
    event_id        TEXT NOT NULL UNIQUE,
    audit_prev_hash TEXT NOT NULL,
    audit_hash      TEXT NOT NULL UNIQUE,
    tenant_id       TEXT NOT NULL,
    record_json     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
)
"""

#: The two tables are ONE chain. Every sequence and every link is checked across both.
CHAIN_ROWS_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS chain_rows AS
    SELECT sequence, audit_hash FROM decision_records
    UNION ALL
    SELECT sequence, audit_hash FROM control_events
"""


def append_only_triggers(table: str) -> tuple[str, ...]:
    """The five triggers that make ``table`` append-only and chain-linked *in the database*.

    A Python guard protects nothing from a raw connection, a DBA or a stray script. These do: UPDATE and
    DELETE abort unconditionally; an INSERT whose sequence is not the next one, or whose
    ``audit_prev_hash`` is not the chain's current head, aborts; and an INSERT carrying another tenant's
    id aborts. The sequence and link guards read ``chain_rows``, which spans BOTH tables, so decisions
    and control events extend one chain and neither can be interleaved after the fact. There is
    deliberately no exception, no privilege level and no debug flag that lifts any of them.
    """
    return (
        f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, 'append-only: {table} rows cannot be modified');
END
""",
        f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, 'append-only: {table} rows cannot be deleted');
END
""",
        f"""
CREATE TRIGGER IF NOT EXISTS {table}_sequence_guard
BEFORE INSERT ON {table}
FOR EACH ROW
WHEN NEW.sequence <> (SELECT ifnull(max(sequence), 0) + 1 FROM chain_rows)
BEGIN
    SELECT RAISE(ABORT, 'append-only: sequence must be max(sequence) + 1 over the whole chain');
END
""",
        f"""
CREATE TRIGGER IF NOT EXISTS {table}_link_guard
BEFORE INSERT ON {table}
FOR EACH ROW
WHEN NEW.audit_prev_hash <> ifnull(
         (SELECT audit_hash FROM chain_rows ORDER BY sequence DESC LIMIT 1),
         '{ZERO_HASH}')
BEGIN
    SELECT RAISE(ABORT, 'append-only: audit_prev_hash must be the last audit_hash in the chain');
END
""",
        f"""
CREATE TRIGGER IF NOT EXISTS {table}_tenant_guard
BEFORE INSERT ON {table}
FOR EACH ROW
WHEN NEW.tenant_id <> (SELECT value FROM ledger_meta WHERE key = 'tenant_id')
BEGIN
    SELECT RAISE(ABORT, 'tenant boundary: this database holds exactly one tenant chain');
END
""",
    )


SCHEMA_STATEMENTS: tuple[str, ...] = (
    LEDGER_META_DDL,
    DECISION_RECORDS_DDL,
    CONTROL_EVENTS_DDL,
    CHAIN_ROWS_VIEW_DDL,
    """
CREATE TRIGGER IF NOT EXISTS ledger_meta_no_update
BEFORE UPDATE ON ledger_meta
BEGIN
    SELECT RAISE(ABORT, 'append-only: ledger_meta rows cannot be modified');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS ledger_meta_no_delete
BEFORE DELETE ON ledger_meta
BEGIN
    SELECT RAISE(ABORT, 'append-only: ledger_meta rows cannot be deleted');
END
""",
    *append_only_triggers("decision_records"),
    *append_only_triggers("control_events"),
)

_LEDGER_SCHEMA_VERSION = "1"

_HEAD_SQL = "SELECT sequence, audit_hash FROM chain_rows ORDER BY sequence DESC LIMIT 1"

_INSERT_DECISION_SQL = (
    "INSERT INTO decision_records "
    "(sequence, decision_id, audit_prev_hash, audit_hash, tenant_id, record_json, recorded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_CONTROL_SQL = (
    "INSERT INTO control_events "
    "(sequence, event_id, audit_prev_hash, audit_hash, tenant_id, record_json, recorded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_CHAIN_SQL = (
    "SELECT sequence, record_json FROM ("
    "SELECT sequence, record_json FROM decision_records "
    "UNION ALL "
    "SELECT sequence, record_json FROM control_events"
    ") ORDER BY sequence"
)


class SqliteTenantLedger(_TenantLedgerBase):
    """One tenant's chain in its own SQLite file, protected by BEFORE UPDATE/DELETE triggers."""

    def __init__(self, tenant_id: str, path: Path) -> None:
        super().__init__(tenant_id)
        self.path = path
        self._lock = threading.Lock()
        self._prepare()

    # -- schema -----------------------------------------------------------------------------------
    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            existing = connection.execute(
                "SELECT value FROM ledger_meta WHERE key = 'tenant_id'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO ledger_meta (key, value) VALUES ('tenant_id', ?), "
                    "('schema_version', ?)",
                    (self.tenant_id, _LEDGER_SCHEMA_VERSION),
                )
            elif existing[0] != self.tenant_id:
                connection.execute("ROLLBACK")
                raise TenantForbidden(
                    detail=(
                        f"{self.path} holds tenant {existing[0]!r}; it cannot be opened as tenant "
                        f"{self.tenant_id!r}"
                    )
                )
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            _rollback(connection)
            raise LedgerError(detail=f"cannot open the ledger at {self.path}: {exc}") from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- writes -----------------------------------------------------------------------------------
    def _commit_decision(self, build: Callable[[int, str], DecisionRecord]) -> DecisionRecord:
        def write(connection: sqlite3.Connection, sequence: int, previous: str) -> DecisionRecord:
            record = build(sequence, previous)
            connection.execute(
                _INSERT_DECISION_SQL,
                (
                    record.sequence,
                    record.decision_id,
                    record.audit_prev_hash,
                    record.audit_hash,
                    record.tenant_id,
                    canonical_json(record),
                    record.recorded_at,
                ),
            )
            return record

        return self._atomic_append(write)

    def _commit_control_event(self, build: Callable[[int, str], ControlEvent]) -> ControlEvent:
        def write(connection: sqlite3.Connection, sequence: int, previous: str) -> ControlEvent:
            event = build(sequence, previous)
            connection.execute(
                _INSERT_CONTROL_SQL,
                (
                    event.sequence,
                    event.event_id,
                    event.audit_prev_hash,
                    event.audit_hash,
                    event.tenant_id,
                    canonical_json(event),
                    event.recorded_at,
                ),
            )
            return event

        return self._atomic_append(write)

    def _atomic_append[T: ChainEntry](
        self, write: Callable[[sqlite3.Connection, int, str], T]
    ) -> T:
        """Read the chain head and write the next link inside one transaction, or write nothing."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                head = connection.execute(_HEAD_SQL).fetchone()
                sequence = (head[0] if head else 0) + 1
                previous = head[1] if head else ZERO_HASH
                entry = write(connection, sequence, previous)
                connection.execute("COMMIT")
                return entry
            except sqlite3.DatabaseError as exc:
                _rollback(connection)
                raise LedgerError(detail=f"the ledger refused the append: {exc}") from exc
            except BaseException:
                _rollback(connection)
                raise
            finally:
                connection.close()

    # -- reads ------------------------------------------------------------------------------------
    def get(self, decision_id: str) -> DecisionRecord:
        """The decision with this id. ``NotFound`` for an unknown id and for another tenant's id alike.

        Another tenant's decisions are in another database file entirely, so the lookup cannot even
        reach them: existence does not leak across the tenant boundary (B3).
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT sequence, record_json FROM decision_records WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise NotFound(detail=f"no decision {decision_id!r} in tenant {self.tenant_id!r}")
        return _decision_from_json(row[1], sequence_hint=row[0])

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]:
        """Up to ``limit`` decisions, NEWEST FIRST, strictly before ``before_sequence`` when given."""
        rows = self._select("decision_records", limit=limit, before_sequence=before_sequence)
        return [_decision_from_json(text, sequence_hint=sequence) for sequence, text in rows]

    def list_control_events(
        self, *, limit: int = 50, before_sequence: int | None = None
    ) -> list[ControlEvent]:
        """Up to ``limit`` control events, NEWEST FIRST, strictly before ``before_sequence``."""
        rows = self._select("control_events", limit=limit, before_sequence=before_sequence)
        return [_control_event_from_json(text) for _sequence, text in rows]

    def _select(
        self, table: str, *, limit: int, before_sequence: int | None
    ) -> list[tuple[int, str]]:
        if limit < 0:
            raise ValidationFailed(detail="limit must not be negative")
        if table not in ("decision_records", "control_events"):  # pragma: no cover - internal
            raise LedgerError(detail=f"unknown table {table!r}")
        clause = "" if before_sequence is None else " WHERE sequence < ?"
        parameters: tuple[Any, ...] = (limit,) if before_sequence is None else (before_sequence, limit)
        connection = self._connect()
        try:
            return list(
                connection.execute(
                    f"SELECT sequence, record_json FROM {table}{clause} "
                    "ORDER BY sequence DESC LIMIT ?",
                    parameters,
                )
            )
        finally:
            connection.close()

    def chain_entries(self) -> list[ChainEntry]:
        """Every link of this tenant's chain - decisions and control events - in sequence order."""
        return [load_chain_entry(json.loads(text)) for _sequence, text in self._chain_rows()]

    def verify_chain(self) -> ChainVerification:
        """Re-derive every hash and every link from the file, decisions and control events alike."""
        return verify_stored_rows(self._chain_rows())

    def _chain_rows(self) -> list[tuple[int, str]]:
        connection = self._connect()
        try:
            return list(connection.execute(_CHAIN_SQL))
        finally:
            connection.close()


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.DatabaseError:  # pragma: no cover - no transaction was open
        pass


class SqliteLedger:
    """Per-tenant chains, one database file each: ``<root_dir>/<tenant_id>.sqlite``.

    A separate file per tenant is the strongest isolation available without a server: a query cannot
    reach another tenant's rows because they are not in the same database at all.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def path_for(self, tenant_id: str) -> Path:
        return self.root_dir / f"{validate_tenant_id(tenant_id)}.sqlite"

    def for_tenant(self, tenant_id: str) -> SqliteTenantLedger:
        return SqliteTenantLedger(tenant_id, self.path_for(tenant_id))
