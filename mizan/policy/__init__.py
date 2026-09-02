"""L1 — policy loading, validation, hashing and diffing.

A policy is versioned, hashed and immutable. The hash is what binds a decision to the exact rules that
produced it, so a policy that has been edited is a different policy and replaying against it is a
different question (that is policy replay, not exact replay).

The YAML loader must never construct a binary float: money in a policy arrives as a decimal string, and
a loader that turns ``"10000.00"`` into a C double has already broken Hard Rule A6 before validation
ever runs.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from pydantic import ConfigDict

from mizan.contracts import ContractModel, Policy, PolicyId, SemVer, Sha256Hex

__all__ = [
    "InMemoryPolicyStore",
    "PolicyChange",
    "PolicyStore",
    "diff_policies",
    "load_policy",
    "policy_hash",
    "validate_policy",
]


class PolicyChange(ContractModel):
    """One field-level difference between two policy versions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    path: str
    old: Any = None
    new: Any = None


def load_policy(text: str, *, fmt: Literal["yaml", "json"] = "yaml") -> Policy:
    """Parse and validate a policy document, preserving decimal literals exactly as written."""
    raise NotImplementedError("L1 implements this in Sprint 2")


def validate_policy(
    payload: Mapping[str, Any],
    *,
    implemented: frozenset[str] | None = None,
) -> Policy:
    """Validate a policy payload and refuse any enabled check the engine does not implement.

    ``implemented`` defaults to ``mizan.risk.IMPLEMENTED_CHECKS``. Refusing at load time is the whole
    point: a policy that enables a check nothing evaluates would otherwise look like protection while
    providing none.
    """
    raise NotImplementedError("L1 implements this in Sprint 2")


def policy_hash(policy: Policy) -> Sha256Hex:
    """The canonical hash of a policy, excluding the hash field itself."""
    raise NotImplementedError("L1 implements this in Sprint 2")


def diff_policies(old: Policy, new: Policy) -> list[PolicyChange]:
    """Field-level differences, for the console's policy diff and for policy replay."""
    raise NotImplementedError("L1 implements this in Sprint 2")


@runtime_checkable
class PolicyStore(Protocol):
    def get(self, tenant_id: str, policy_id: PolicyId, version: SemVer | None = None) -> Policy: ...

    def get_by_hash(self, tenant_id: str, policy_hash: Sha256Hex) -> Policy: ...

    def put(self, policy: Policy) -> None: ...

    def activate(self, tenant_id: str, policy_id: PolicyId, version: SemVer) -> None: ...

    def active(self, tenant_id: str, policy_id: PolicyId) -> Policy: ...


class InMemoryPolicyStore:
    """Tenant-scoped policy storage. Keys are (tenant_id, policy_id, version); never a flat namespace."""

    def __init__(self) -> None:
        self._policies: dict[tuple[str, str, str], Policy] = {}
        self._active: dict[tuple[str, str], str] = {}

    def get(self, tenant_id: str, policy_id: PolicyId, version: SemVer | None = None) -> Policy:
        raise NotImplementedError("L1 implements this in Sprint 2")

    def get_by_hash(self, tenant_id: str, policy_hash: Sha256Hex) -> Policy:
        raise NotImplementedError("L1 implements this in Sprint 2")

    def put(self, policy: Policy) -> None:
        raise NotImplementedError("L1 implements this in Sprint 2")

    def activate(self, tenant_id: str, policy_id: PolicyId, version: SemVer) -> None:
        raise NotImplementedError("L1 implements this in Sprint 2")

    def active(self, tenant_id: str, policy_id: PolicyId) -> Policy:
        raise NotImplementedError("L1 implements this in Sprint 2")
