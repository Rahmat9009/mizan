"""GovernorDecision: the final verdict. The type cannot express an authorized quantity above the original, and an
advisory opinion has no value meaning "increase" or "approve" (Hard Rule E1)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from mizan.contracts._base import ContractModel, build_hashed, hash_check_skipped
from mizan.contracts.canonical import DECIMAL_CONTEXT, verdict_hash_for
from mizan.contracts.policy import AuthorityCeiling
from mizan.contracts.reason_codes import ReasonCodeField, ReasonCodeList
from mizan.contracts.risk_context import PolicyRef
from mizan.contracts.trade_proposal import MAX_LEGS, MAX_REASONING_CHARS
from mizan.contracts.types import (
    AgentId,
    DecimalStr,
    NonEmptyStr,
    NonNegativeDecimalStr,
    PositiveDecimalStr,
    Rfc3339,
    SchemaVersion,
    Sha256Hex,
    TenantId,
    Uuid7Str,
    dec,
)

GovernorVerdict = Literal["APPROVE", "REDUCE", "REJECT"]
AdvisoryRecommendation = Literal["CONCUR", "REDUCE", "REJECT"]
ReductionSource = Literal["deterministic", "advisory"]


class AdvisoryOpinion(ContractModel):
    """What the (optional) LLM advisory said. Downward-only by type: CONCUR, REDUCE or REJECT."""

    profile: NonEmptyStr
    invoked: bool
    available: bool
    recommendation: AdvisoryRecommendation | None
    recommended_quantity: NonNegativeDecimalStr | None
    reasoning: str = Field(default="", max_length=MAX_REASONING_CHARS)
    authority_ceiling: AuthorityCeiling
    provider_ref: NonEmptyStr | None
    raw_hash: Sha256Hex | None

    @model_validator(mode="after")
    def _consistent(self) -> AdvisoryOpinion:
        if not self.invoked and self.available:
            raise ValueError("an advisory that was not invoked cannot be available")
        if not self.available:
            if self.recommendation is not None or self.recommended_quantity is not None:
                raise ValueError("an unavailable advisory carries no recommendation or quantity")
            return self
        if self.recommendation is None:
            raise ValueError("an available advisory must carry a recommendation")
        if self.recommendation == "REDUCE":
            if self.recommended_quantity is None or dec(self.recommended_quantity) <= 0:
                raise ValueError("advisory REDUCE requires a positive recommended_quantity")
        elif self.recommendation == "REJECT":
            if self.recommended_quantity is not None and dec(self.recommended_quantity) != 0:
                raise ValueError("advisory REJECT requires recommended_quantity null or 0")
        elif self.recommended_quantity is not None:
            raise ValueError("advisory CONCUR must not carry a recommended_quantity")
        return self


class Quantities(ContractModel):
    total_quantity: NonNegativeDecimalStr
    total_notional: DecimalStr | None


class AuthorizedLegQuantity(ContractModel):
    leg_index: int = Field(ge=0, le=MAX_LEGS - 1)
    quantity: PositiveDecimalStr


class Reduction(ContractModel):
    source: ReductionSource
    from_quantity: NonNegativeDecimalStr
    to_quantity: NonNegativeDecimalStr
    reason_code: ReasonCodeField

    @model_validator(mode="after")
    def _downward(self) -> Reduction:
        if dec(self.to_quantity) >= dec(self.from_quantity):
            raise ValueError("a reduction must lower the quantity")
        return self


class Authorized(ContractModel):
    total_quantity: NonNegativeDecimalStr
    total_notional: DecimalStr | None
    legs: list[AuthorizedLegQuantity] = Field(max_length=MAX_LEGS)
    reductions: list[Reduction]

    @model_validator(mode="after")
    def _legs(self) -> Authorized:
        indices = [leg.leg_index for leg in self.legs]
        if indices != sorted(set(indices)):
            raise ValueError("authorized legs must be ordered by leg_index with no duplicates")
        total = dec(self.total_quantity)
        if self.legs:
            leg_sum = Decimal(0)
            for leg in self.legs:
                leg_sum = DECIMAL_CONTEXT.add(leg_sum, dec(leg.quantity))
            if leg_sum != total:
                raise ValueError("authorized leg quantities must sum to total_quantity")
        elif total != 0:
            raise ValueError("a non-zero authorized total requires legs")
        return self


def _verdict_hash(payload: Mapping[str, Any]) -> str:
    return verdict_hash_for(
        payload["verdict"],
        payload["reason_codes"],
        payload["authorized"]["total_quantity"],
        payload["authorized"]["legs"],
        payload["evaluation_id"],
    )


class GovernorDecision(ContractModel):
    schema_version: SchemaVersion
    decision_id: Uuid7Str
    proposal_id: Sha256Hex
    evaluation_id: Sha256Hex
    tenant_id: TenantId
    agent_id: AgentId
    policy: PolicyRef
    engine_version: NonEmptyStr
    decision_timestamp: Rfc3339
    verdict: GovernorVerdict
    reason_codes: ReasonCodeList
    original: Quantities
    authorized: Authorized
    llm_advisory: AdvisoryOpinion | None
    verdict_hash: Sha256Hex

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> GovernorDecision:
        original = dec(self.original.total_quantity)
        authorized = dec(self.authorized.total_quantity)
        if original <= 0:
            raise ValueError("original.total_quantity must be positive")
        if authorized > original:
            raise ValueError("authorized.total_quantity cannot exceed original.total_quantity")
        if self.verdict == "REJECT":
            if authorized != 0 or self.authorized.legs:
                raise ValueError("REJECT requires authorized total 0 and no legs")
            if self.authorized.total_notional is not None and dec(self.authorized.total_notional) != 0:
                raise ValueError("REJECT requires authorized.total_notional null or 0")
        elif self.verdict == "APPROVE":
            if authorized != original:
                raise ValueError("APPROVE requires authorized.total_quantity == original.total_quantity")
            if self.authorized.total_notional != self.original.total_notional:
                raise ValueError("APPROVE requires authorized.total_notional == original.total_notional")
            if self.authorized.reductions:
                raise ValueError("APPROVE cannot carry reductions")
        else:
            if not 0 < authorized < original:
                raise ValueError("REDUCE requires 0 < authorized.total_quantity < original.total_quantity")
            if not self.authorized.reductions:
                raise ValueError("REDUCE requires at least one reduction")
        if self.verdict in ("REJECT", "REDUCE") and not self.reason_codes:
            raise ValueError(f"{self.verdict} requires at least one reason code (A4)")
        advisory = self.llm_advisory
        if advisory is not None and advisory.available:
            if advisory.recommendation == "REJECT" and self.verdict != "REJECT":
                raise ValueError("an advisory REJECT cannot be overridden upward")
            if advisory.recommendation == "REDUCE" and advisory.recommended_quantity is not None:
                if authorized > dec(advisory.recommended_quantity):
                    raise ValueError("authorized.total_quantity cannot exceed the advisory recommended_quantity")
        if not hash_check_skipped(info) and self.verdict_hash != _verdict_hash(self.model_dump(mode="json")):
            raise ValueError("verdict_hash does not match verdict_hash_for(...)")
        return self

    @classmethod
    def build(cls, **fields: Any) -> GovernorDecision:
        """Construct a decision, computing ``verdict_hash``. ``decision_id`` must be supplied (``uuid7()``)."""
        return build_hashed(cls, "verdict_hash", _verdict_hash, fields)


__all__ = [
    "AdvisoryOpinion",
    "AdvisoryRecommendation",
    "Authorized",
    "AuthorizedLegQuantity",
    "GovernorDecision",
    "GovernorVerdict",
    "Quantities",
    "Reduction",
    "ReductionSource",
]
