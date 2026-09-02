"""ExecutionResult: what the execution gate did with an authorization, and every timestamp it stamped."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mizan.contracts._base import ContractModel
from mizan.contracts.reason_codes import ReasonCodeList
from mizan.contracts.risk_context import ResponseLevel
from mizan.contracts.types import (
    Environment,
    NonEmptyStr,
    NonNegativeDecimalStr,
    PositiveDecimalStr,
    Rfc3339,
    SchemaVersion,
    Sha256Hex,
    TenantId,
    Text,
    Uuid7Str,
)

ExecutionStatus = Literal["SUBMITTED", "WOULD_SUBMIT", "BLOCKED", "FAILED", "RECONCILED_EXISTING"]


class RevalidationReport(ContractModel):
    """The TOCTOU re-check (E9). ``supported`` is False whenever the re-check did not run: unknown is not safe."""

    performed: bool
    fresh_context_id: NonEmptyStr | None
    fresh_evaluation_id: Sha256Hex | None
    fresh_recommended_quantity: NonNegativeDecimalStr | None
    supported: bool
    state_changed: bool = False
    response_level_at_execution: ResponseLevel | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RevalidationReport:
        fresh = (self.fresh_context_id, self.fresh_evaluation_id, self.fresh_recommended_quantity)
        if self.performed:
            if any(value is None for value in fresh):
                raise ValueError("a performed revalidation must report fresh context, evaluation and quantity")
        else:
            if any(value is not None for value in fresh) or self.supported or self.state_changed:
                raise ValueError("a revalidation that was not performed cannot report fresh state or support")
        return self


class Fill(ContractModel):
    filled_quantity: PositiveDecimalStr
    avg_price: PositiveDecimalStr
    filled_at: Rfc3339


class BrokerRef(ContractModel):
    name: NonEmptyStr
    environment: Environment


class ExecutionResult(ContractModel):
    schema_version: SchemaVersion
    result_id: Uuid7Str
    auth_id: Uuid7Str
    decision_id: Uuid7Str
    proposal_id: Sha256Hex
    tenant_id: TenantId
    status: ExecutionStatus
    reason_codes: ReasonCodeList
    broker: BrokerRef
    client_order_id: NonEmptyStr | None
    broker_order_id: NonEmptyStr | None
    checked_at: Rfc3339
    authorization_validated_at: Rfc3339 | None
    kill_switch_checked_at: Rfc3339 | None
    submitted_at: Rfc3339 | None
    revalidation: RevalidationReport
    fills: list[Fill] = Field(default_factory=list)
    broker_status: NonEmptyStr | None
    message: Text

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionResult:
        status = self.status
        if status in ("SUBMITTED", "RECONCILED_EXISTING"):
            if self.client_order_id is None or self.broker_order_id is None:
                raise ValueError(f"{status} requires client_order_id and broker_order_id")
        else:
            if self.broker_order_id is not None:
                raise ValueError(f"{status} cannot carry a broker_order_id")
            if self.fills:
                raise ValueError(f"{status} cannot carry fills")
        if status == "SUBMITTED":
            if self.submitted_at is None:
                raise ValueError("SUBMITTED requires submitted_at")
            if self.kill_switch_checked_at is None or self.authorization_validated_at is None:
                raise ValueError("SUBMITTED requires kill_switch_checked_at and authorization_validated_at (E4/E6)")
            if not self.revalidation.performed or not self.revalidation.supported:
                raise ValueError("SUBMITTED requires a performed, supporting revalidation (E9)")
        if status == "WOULD_SUBMIT":
            if self.kill_switch_checked_at is None or self.authorization_validated_at is None:
                raise ValueError("WOULD_SUBMIT requires kill_switch_checked_at and authorization_validated_at")
            if self.submitted_at is not None:
                raise ValueError("WOULD_SUBMIT cannot carry submitted_at")
        if status in ("BLOCKED", "FAILED"):
            if not self.reason_codes:
                raise ValueError(f"{status} requires at least one reason code (A4)")
            if self.submitted_at is not None and status == "BLOCKED":
                raise ValueError("BLOCKED cannot carry submitted_at")
        return self


__all__ = ["BrokerRef", "ExecutionResult", "ExecutionStatus", "Fill", "RevalidationReport"]
