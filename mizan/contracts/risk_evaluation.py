"""RiskEvaluation: the deterministic engine's verdict, one CheckResult per check, in CHECK_IDS order."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from mizan.contracts._base import ContractModel, build_hashed, hash_check_skipped
from mizan.contracts.canonical import evaluation_id_for
from mizan.contracts.policy import CHECK_INDEX, CheckIdStr
from mizan.contracts.reason_codes import ReasonCodeField, ReasonCodeList
from mizan.contracts.risk_context import PolicyRef
from mizan.contracts.types import (
    DecimalStr,
    NonEmptyStr,
    NonNegativeDecimalStr,
    PositiveDecimalStr,
    Rfc3339,
    SchemaVersion,
    Sha256Hex,
    TenantId,
    Text,
    dec,
)

Severity = Literal["blocking", "warning", "info"]
EvaluationVerdict = Literal["PASS", "REDUCE", "REJECT"]


class CheckResult(ContractModel):
    check_id: CheckIdStr
    passed: bool
    severity: Severity
    reason_code: ReasonCodeField | None
    threshold: DecimalStr | None
    actual: DecimalStr | None
    data_source: NonEmptyStr | None
    snapshot_ts: Rfc3339 | None
    recommended_quantity: NonNegativeDecimalStr | None
    detail: Text

    @model_validator(mode="after")
    def _failed_needs_code(self) -> CheckResult:
        if not self.passed and self.reason_code is None:
            raise ValueError("a failed check must carry a reason_code (A4)")
        return self


class RiskEvaluation(ContractModel):
    schema_version: SchemaVersion
    evaluation_id: Sha256Hex
    proposal_id: Sha256Hex
    context_id: NonEmptyStr
    tenant_id: TenantId
    policy: PolicyRef
    engine_version: NonEmptyStr
    evaluated_at: Rfc3339
    verdict: EvaluationVerdict
    reason_codes: ReasonCodeList
    checks: list[CheckResult] = Field(min_length=1)
    original_quantity: PositiveDecimalStr
    recommended_quantity: NonNegativeDecimalStr
    original_notional: DecimalStr | None
    recommended_notional: DecimalStr | None
    data_complete: bool

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> RiskEvaluation:
        indices = [CHECK_INDEX[check.check_id] for check in self.checks]
        if indices != sorted(set(indices)):
            raise ValueError("checks must be in CHECK_IDS order with no duplicates")
        original = dec(self.original_quantity)
        recommended = dec(self.recommended_quantity)
        blocking_failures = [check for check in self.checks if not check.passed and check.severity == "blocking"]
        if blocking_failures and self.verdict != "REJECT":
            raise ValueError("a failed blocking check requires verdict REJECT")
        codes = set(self.reason_codes)
        for check in blocking_failures:
            if check.reason_code not in codes:
                raise ValueError(f"reason_codes must include {check.reason_code} from failed check {check.check_id}")
        if self.verdict == "REJECT":
            if recommended != 0:
                raise ValueError("REJECT requires recommended_quantity == 0")
            if self.recommended_notional is not None and dec(self.recommended_notional) != 0:
                raise ValueError("REJECT requires recommended_notional to be null or 0")
        elif self.verdict == "REDUCE":
            if not 0 < recommended < original:
                raise ValueError("REDUCE requires 0 < recommended_quantity < original_quantity")
        else:
            if recommended != original:
                raise ValueError("PASS requires recommended_quantity == original_quantity")
            if self.recommended_notional != self.original_notional:
                raise ValueError("PASS requires recommended_notional == original_notional")
        if self.verdict in ("REJECT", "REDUCE") and not self.reason_codes:
            raise ValueError(f"{self.verdict} requires at least one reason code (A4)")
        if not hash_check_skipped(info) and self.evaluation_id != evaluation_id_for(self):
            raise ValueError("evaluation_id does not match the canonical hash of the evaluation content")
        return self

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    @classmethod
    def build(cls, **fields: Any) -> RiskEvaluation:
        """Construct an evaluation, computing ``evaluation_id`` from the normalised content."""
        return build_hashed(cls, "evaluation_id", evaluation_id_for, fields)


__all__ = ["CheckResult", "EvaluationVerdict", "RiskEvaluation", "Severity"]
