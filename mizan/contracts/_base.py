"""Base class and build helper shared by every contract model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationInfo

from mizan.contracts.canonical import ZERO_HASH, canonical_json, object_hash
from mizan.contracts.types import SCHEMA_VERSION

# Validation-context flag used only by ``build()`` while the derived hash/id is not yet known.
SKIP_HASH_CHECK = "mizan.skip_hash_check"

ModelT = TypeVar("ModelT", bound="ContractModel")


class ContractModel(BaseModel):
    """Frozen, strict, closed. Unknown fields are errors; ints are not strings; nothing is coerced."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_json(self) -> str:
        return canonical_json(self)

    def object_hash(self) -> str:
        return object_hash(self)


def hash_check_skipped(info: ValidationInfo) -> bool:
    context = info.context
    return bool(context) and bool(context.get(SKIP_HASH_CHECK))


def verify_presented_hash(
    model: BaseModel,
    data: Any,
    info: ValidationInfo,
    *,
    field: str,
    compute: Callable[[Any], str],
    message: str,
) -> None:
    """Check a derived hash against the content AS PRESENTED, not against a re-serialisation of it.

    Comparing to ``compute(model)`` sounds equivalent and is not. It asks "does this hash match what
    today's contract would produce?", when the only question an audit trail may ask is "does this hash
    match what was written?". The two answers diverge the moment a contract gains a field: every stored
    record silently acquires the new field's default, every hash moves, and a ledger nobody touched
    stops verifying. One optional policy section (``ev``) did exactly that to all 12 live records here
    while their bytes were provably intact - the records were fine, the reader had changed.

    Hashing the presented content is also the only check an outside party can reproduce. They have the
    stored JSON and the published format, not our model: drop the hash field, canonicalise, compare.
    If we verify differently we can call a ledger good that they call broken, or the reverse, and a
    tamper-evidence claim that only our own code can confirm is not evidence of anything.

    Objects built in memory pass unchanged - there ``data`` is not a mapping and the model IS the
    presented content.
    """
    if hash_check_skipped(info):
        return
    presented = data if isinstance(data, Mapping) else model
    if getattr(model, field) != compute(presented):
        raise ValueError(message)


def build_hashed(
    cls: type[ModelT],
    hash_field: str,
    compute: Callable[[Mapping[str, Any]], str],
    fields: Mapping[str, Any],
) -> ModelT:
    """Validate ``fields`` (normalising every DecimalStr/Rfc3339), derive ``hash_field`` from the normalised dump,
    then validate again for real so the returned object has passed every validator including the hash check."""
    payload = dict(fields)
    if hash_field in payload:
        raise ValueError(f"{cls.__name__}.build() computes {hash_field}; do not pass it")
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload[hash_field] = ZERO_HASH
    provisional = cls.model_validate(payload, context={SKIP_HASH_CHECK: True})
    normalised = provisional.model_dump(mode="json")
    normalised[hash_field] = compute(normalised)
    return cls.model_validate(normalised)


__all__ = [
    "SKIP_HASH_CHECK",
    "ContractModel",
    "build_hashed",
    "hash_check_skipped",
    "verify_presented_hash",
]
