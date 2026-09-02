"""ControlEvent: graduated-response level changes and kill-switch flips, hash-chained in the SAME per-tenant chain
as decision records (R-GRAD-2). Escalation may be automatic; de-escalation requires a human (R-GRAD-1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from mizan.contracts._base import ContractModel, build_hashed, hash_check_skipped
from mizan.contracts.canonical import ZERO_HASH, record_hash_for
from mizan.contracts.reason_codes import ReasonCodeList
from mizan.contracts.risk_context import PolicyRef, ResponseLevel
from mizan.contracts.types import NonEmptyStr, Rfc3339, SchemaVersion, Sha256Hex, TenantId, Uuid7Str

ControlEventType = Literal[
    "response_level_changed",
    "kill_switch_activated",
    "kill_switch_deactivated",
    "policy_activated",
]
ActorType = Literal["system", "human"]


class Actor(ContractModel):
    type: ActorType
    id: NonEmptyStr


class ControlEvent(ContractModel):
    schema_version: SchemaVersion
    event_id: Uuid7Str
    sequence: int = Field(ge=1)
    tenant_id: TenantId
    event_type: ControlEventType
    from_level: ResponseLevel | None
    to_level: ResponseLevel | None
    actor: Actor
    trigger_reason_codes: ReasonCodeList
    policy: PolicyRef | None
    occurred_at: Rfc3339
    recorded_at: Rfc3339
    audit_prev_hash: Sha256Hex
    audit_hash: Sha256Hex

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> ControlEvent:
        if self.event_type == "response_level_changed":
            if self.from_level is None or self.to_level is None:
                raise ValueError("response_level_changed requires from_level and to_level")
            if self.from_level == self.to_level:
                raise ValueError("response_level_changed requires from_level != to_level")
            if self.to_level < self.from_level and self.actor.type != "human":
                raise ValueError("a downward response-level change requires a human actor (R-GRAD-1)")
        else:
            if self.from_level is not None or self.to_level is not None:
                raise ValueError(f"{self.event_type} must not carry response levels")
        if self.event_type == "policy_activated" and self.policy is None:
            raise ValueError("policy_activated requires a policy reference")
        if self.event_type == "kill_switch_deactivated" and self.actor.type != "human":
            raise ValueError("deactivating the kill switch requires a human actor")
        if (self.sequence == 1) != (self.audit_prev_hash == ZERO_HASH):
            raise ValueError("audit_prev_hash is ZERO_HASH exactly when sequence == 1")
        if not hash_check_skipped(info) and self.audit_hash != record_hash_for(self):
            raise ValueError("audit_hash does not match record_hash_for(event)")
        return self

    @classmethod
    def build(cls, **fields: Any) -> ControlEvent:
        """Construct an event, computing ``audit_hash`` from the normalised content."""
        return build_hashed(cls, "audit_hash", record_hash_for, fields)


__all__ = ["Actor", "ActorType", "ControlEvent", "ControlEventType"]
