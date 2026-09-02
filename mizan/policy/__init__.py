"""L1 -- policy loading, validation, hashing and diffing.

A policy is versioned, hashed and immutable. The hash is what binds a decision to the exact rules that
produced it, so a policy that has been edited is a different policy and replaying against it is a
different question (that is policy replay, not exact replay).

The YAML loader must never construct a binary float: money in a policy arrives as a decimal string, and
a loader that turns ``"10000.00"`` into a C double has already broken Hard Rule A6 before validation
ever runs. ``mizan.policy.loader`` removes that possibility from the parser itself.

``validate_policy`` refuses a policy that enables a check the running engine does not implement. That
refusal is the point: a policy naming a control nothing evaluates would read as protection while
providing none, which is the failure mode Hard Rule E2 exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, ValidationError

from mizan.contracts import ContractModel, Policy, PolicyId, ReasonCode, SemVer, Sha256Hex
from mizan.contracts.canonical import policy_hash_for
from mizan.contracts.errors import NotFound, PolicyError
from mizan.policy.loader import conform_policy, parse_document

__all__ = [
    "InMemoryPolicyStore",
    "PolicyChange",
    "PolicyStore",
    "diff_policies",
    "load_policy",
    "policy_hash",
    "validate_policy",
]

_ABSENT = object()


class PolicyChange(ContractModel):
    """One field-level difference between two policy versions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    path: str
    old: Any = None
    new: Any = None


def load_policy(text: str, *, fmt: Literal["yaml", "json"] = "yaml") -> Policy:
    """Parse and validate a policy document, preserving decimal literals exactly as written."""
    payload = parse_document(text, fmt=fmt)
    if not isinstance(payload, Mapping):
        raise PolicyError(
            message="a policy document must be a mapping of fields",
            reason_codes=[ReasonCode.POLICY_INVALID],
            detail=f"top-level value is {type(payload).__name__}",
        )
    return validate_policy(payload)


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
    from mizan.risk import IMPLEMENTED_CHECKS

    available = IMPLEMENTED_CHECKS if implemented is None else frozenset(implemented)
    fields = conform_policy(payload)
    declared = fields.pop("policy_hash", None)
    if declared is not None and not isinstance(declared, str):
        raise PolicyError(
            message="policy_hash must be the hexadecimal digest string",
            reason_codes=[ReasonCode.POLICY_INVALID],
            detail=f"policy_hash is {type(declared).__name__}",
        )
    try:
        policy = Policy.build(**fields)
    except (ValidationError, ValueError, TypeError) as exc:
        raise PolicyError(
            message="the policy document is not a valid policy",
            reason_codes=[ReasonCode.POLICY_INVALID],
            detail=str(exc),
        ) from exc
    if declared is not None and declared != policy.policy_hash:
        raise PolicyError(
            message="the policy content does not match its declared policy_hash",
            reason_codes=[ReasonCode.POLICY_HASH_MISMATCH],
            detail=f"declared {declared}, computed {policy.policy_hash}",
        )
    unimplemented = sorted(set(policy.enabled_checks) - available)
    if unimplemented:
        raise PolicyError(
            message=(
                "the policy enables checks this engine does not implement: " + ", ".join(unimplemented)
            ),
            reason_codes=[ReasonCode.CHECK_NOT_IMPLEMENTED, ReasonCode.POLICY_INVALID],
            detail="disable them explicitly or run an engine build that implements them",
        )
    return policy


def policy_hash(policy: Policy) -> Sha256Hex:
    """The canonical hash of a policy, excluding the hash field itself."""
    return policy_hash_for(policy)


def diff_policies(old: Policy, new: Policy) -> list[PolicyChange]:
    """Field-level differences, for the console's policy diff and for policy replay."""
    changes: list[PolicyChange] = []
    _walk("", old.model_dump(mode="json"), new.model_dump(mode="json"), changes)
    return changes


def _absent(value: Any) -> bool:
    """A key that is not there and a key explicitly set to null are both "nothing to compare"."""
    return value is _ABSENT or value is None


def _both_mappings(old: Any, new: Any) -> bool:
    if not isinstance(old, Mapping) and not isinstance(new, Mapping):
        return False
    return (isinstance(old, Mapping) or _absent(old)) and (isinstance(new, Mapping) or _absent(new))


def _both_lists(old: Any, new: Any) -> bool:
    if not isinstance(old, list) and not isinstance(new, list):
        return False
    return (isinstance(old, list) or _absent(old)) and (isinstance(new, list) or _absent(new))


def _walk(path: str, old: Any, new: Any, changes: list[PolicyChange]) -> None:
    """Descend to the leaves, so an added section reads as its fields rather than as one blob."""
    if _both_mappings(old, new):
        old_map: Mapping[str, Any] = old if isinstance(old, Mapping) else {}
        new_map: Mapping[str, Any] = new if isinstance(new, Mapping) else {}
        for key in sorted(set(old_map) | set(new_map)):
            child = f"{path}.{key}" if path else key
            _walk(child, old_map.get(key, _ABSENT), new_map.get(key, _ABSENT), changes)
        return
    if _both_lists(old, new):
        old_list: list[Any] = old if isinstance(old, list) else []
        new_list: list[Any] = new if isinstance(new, list) else []
        for index in range(max(len(old_list), len(new_list))):
            child = f"{path}[{index}]"
            left = old_list[index] if index < len(old_list) else _ABSENT
            right = new_list[index] if index < len(new_list) else _ABSENT
            _walk(child, left, right, changes)
        return
    if _absent(old) and _absent(new):
        # An absent key and a key explicitly set to null say the same thing: there is no value here.
        return
    if old != new:
        changes.append(
            PolicyChange(
                path=path,
                old=None if old is _ABSENT else old,
                new=None if new is _ABSENT else new,
            )
        )


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
        if version is None:
            return self.active(tenant_id, policy_id)
        policy = self._policies.get((tenant_id, policy_id, version))
        if policy is None:
            raise NotFound(
                message="no policy with that id and version for this tenant",
                reason_codes=[ReasonCode.POLICY_NOT_FOUND],
                detail=f"{tenant_id}/{policy_id}/{version}",
            )
        return policy

    def get_by_hash(self, tenant_id: str, policy_hash: Sha256Hex) -> Policy:
        for key in sorted(self._policies):
            if key[0] != tenant_id:
                continue
            policy = self._policies[key]
            if policy.policy_hash == policy_hash:
                return policy
        raise NotFound(
            message="no policy with that hash for this tenant",
            reason_codes=[ReasonCode.POLICY_NOT_FOUND],
            detail=f"{tenant_id}/{policy_hash}",
        )

    def put(self, policy: Policy) -> None:
        """Store a version. The first version of a policy id becomes the active one; later ones do not.

        A later ``put`` never switches the active version: activation is an explicit, auditable act
        (Addendum 1 B.6 records it as a ControlEvent), not a side effect of writing a draft.
        """
        key = (policy.tenant_id, policy.policy_id, policy.policy_version)
        existing = self._policies.get(key)
        if existing is not None and existing.policy_hash != policy.policy_hash:
            raise PolicyError(
                message="a different policy is already stored under this id and version",
                reason_codes=[ReasonCode.POLICY_INVALID],
                detail=f"{policy.tenant_id}/{policy.policy_id}/{policy.policy_version}",
            )
        self._policies[key] = policy
        self._active.setdefault((policy.tenant_id, policy.policy_id), policy.policy_version)

    def activate(self, tenant_id: str, policy_id: PolicyId, version: SemVer) -> None:
        self.get(tenant_id, policy_id, version)  # raises NotFound when the version does not exist
        self._active[(tenant_id, policy_id)] = version

    def active(self, tenant_id: str, policy_id: PolicyId) -> Policy:
        version = self._active.get((tenant_id, policy_id))
        if version is None:
            raise NotFound(
                message="no active policy with that id for this tenant",
                reason_codes=[ReasonCode.POLICY_NOT_FOUND],
                detail=f"{tenant_id}/{policy_id}",
            )
        return self.get(tenant_id, policy_id, version)

    def versions(self, tenant_id: str, policy_id: PolicyId) -> list[str]:
        """Every stored version of one policy id, sorted; a read model for the console."""
        return sorted(key[2] for key in self._policies if key[0] == tenant_id and key[1] == policy_id)

    def tenants(self) -> list[str]:
        return sorted({key[0] for key in self._policies})
