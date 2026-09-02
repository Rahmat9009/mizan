"""L2 — the append-only, hash-chained decision ledger.

Hard Rules A2 (no update path, no delete path, at any privilege level - enforced at the storage layer),
A5 (chain integrity independently verifiable without Mizan's involvement) and B3 (cross-tenant access
impossible by construction - one chain, and one storage unit, per tenant).

:func:`verify_chain_records` is implemented here and now, deliberately. It is the customer's offline
verifier: it depends on nothing but the contracts, so a customer can re-derive every hash themselves and
never has to take our word for the chain. The ledgers that produce the records come in Sprint 2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from mizan.contracts import (
    ContractModel,
    DecisionRecord,
    ExecutionAuthorization,
    ExecutionResult,
    GovernorDecision,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)
from mizan.contracts.canonical import ZERO_HASH, record_hash_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime
    from pathlib import Path

__all__ = [
    "ChainVerification",
    "InMemoryLedger",
    "Ledger",
    "SqliteLedger",
    "TenantLedger",
    "verify_chain_records",
]

#: A tenant id is also a filename and a schema name, so it is validated before it is either.
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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
        recorded_at: "datetime",
    ) -> DecisionRecord: ...

    def get(self, decision_id: str) -> DecisionRecord: ...

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]: ...

    def verify_chain(self) -> ChainVerification: ...


@runtime_checkable
class Ledger(Protocol):
    def for_tenant(self, tenant_id: str) -> TenantLedger: ...


class _TenantLedgerStub:
    """Shared stub body. Carries no mutation vocabulary, so introspection sees none."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = validate_tenant_id(tenant_id)

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
        recorded_at: "datetime",
    ) -> DecisionRecord:
        raise NotImplementedError("L2 implements this in Sprint 2")

    def get(self, decision_id: str) -> DecisionRecord:
        raise NotImplementedError("L2 implements this in Sprint 2")

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]:
        raise NotImplementedError("L2 implements this in Sprint 2")

    def verify_chain(self) -> ChainVerification:
        raise NotImplementedError("L2 implements this in Sprint 2")


class InMemoryTenantLedger(_TenantLedgerStub):
    """One tenant's in-memory chain."""


class SqliteTenantLedger(_TenantLedgerStub):
    """One tenant's chain in its own SQLite file, protected by BEFORE UPDATE/DELETE triggers."""

    def __init__(self, tenant_id: str, path: "Path") -> None:
        super().__init__(tenant_id)
        self.path = path


class InMemoryLedger:
    """Per-tenant in-memory chains. Separate objects, never a filtered shared list (B3)."""

    def __init__(self) -> None:
        self._tenants: dict[str, InMemoryTenantLedger] = {}

    def for_tenant(self, tenant_id: str) -> InMemoryTenantLedger:
        validate_tenant_id(tenant_id)
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = InMemoryTenantLedger(tenant_id)
        return self._tenants[tenant_id]


class SqliteLedger:
    """Per-tenant chains, one database file each: ``<root_dir>/<tenant_id>.sqlite``.

    A separate file per tenant is the strongest isolation available without a server: a query cannot
    reach another tenant's rows because they are not in the same database at all.
    """

    def __init__(self, root_dir: "Path") -> None:
        self.root_dir = root_dir

    def path_for(self, tenant_id: str) -> "Path":
        return self.root_dir / f"{validate_tenant_id(tenant_id)}.sqlite"

    def for_tenant(self, tenant_id: str) -> SqliteTenantLedger:
        return SqliteTenantLedger(tenant_id, self.path_for(tenant_id))
