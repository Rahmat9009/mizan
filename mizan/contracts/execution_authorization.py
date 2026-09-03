"""ExecutionAuthorization: minimum authority, shortest time, bound to the exact policy and state that justified it.

Single-use, paper-only, 5..30 seconds, and state-bound: ``bound_state`` records the hashes the decision was made
under so the execution gate can refuse to act on stale state (Addendum 1 B.4, Hard Rules E6/E9).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationInfo, model_validator

from mizan.contracts._base import verify_presented_hash, ContractModel, build_hashed
from mizan.contracts.canonical import DECIMAL_CONTEXT, authorization_hash_for, idempotency_key_for
from mizan.contracts.risk_context import PolicyRef, ResponseLevel
from mizan.contracts.trade_proposal import (
    MAX_LEGS,
    AssetClass,
    ContractType,
    Intent,
    OrderType,
    Side,
    occ_symbol_for,
)
from mizan.contracts.types import (
    AgentId,
    DateStr,
    DecimalStr,
    Environment,
    NonEmptyStr,
    OccSymbol,
    PositiveDecimalStr,
    Rfc3339,
    SchemaVersion,
    Sha256Hex,
    StrictTrue,
    Symbol,
    TenantId,
    Uuid7Str,
    dec,
    format_ts,
    parse_ts,
)

IDEMPOTENCY_KEY_PATTERN = r"^mz1-[0-9a-f]{40}$"
IdempotencyKey = Annotated[str, StringConstraints(strict=True, pattern=IDEMPOTENCY_KEY_PATTERN)]
TTL_MIN_SECONDS = 5
TTL_MAX_SECONDS = 30


class AuthorizedLeg(ContractModel):
    leg_index: int = Field(ge=0, le=MAX_LEGS - 1)
    side: Side
    symbol: Symbol
    occ_symbol: OccSymbol | None
    contract_type: ContractType | None
    strike: PositiveDecimalStr | None
    expiry: DateStr | None
    quantity: PositiveDecimalStr
    limit_price: PositiveDecimalStr | None
    order_type: OrderType

    @model_validator(mode="after")
    def _consistent(self) -> AuthorizedLeg:
        option_fields = (self.occ_symbol, self.contract_type, self.strike, self.expiry)
        present = [field is not None for field in option_fields]
        if any(present) and not all(present):
            raise ValueError("occ_symbol, contract_type, strike and expiry must be all present or all null")
        if self.contract_type is not None:
            assert self.expiry is not None and self.strike is not None
            expected = occ_symbol_for(self.symbol, self.contract_type, self.expiry, self.strike)
            if self.occ_symbol != expected:
                raise ValueError(f"occ_symbol {self.occ_symbol!r} does not match the leg (expected {expected!r})")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("a market order must not carry a limit_price")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("a limit order requires a limit_price")
        return self

    @property
    def is_option(self) -> bool:
        return self.contract_type is not None


class BoundState(ContractModel):
    """The state the decision was made under. The gate re-derives these from fresh state and compares."""

    policy_hash: Sha256Hex
    portfolio_snapshot_id: NonEmptyStr
    portfolio_state_hash: Sha256Hex
    market_snapshot_id: NonEmptyStr
    response_level: ResponseLevel
    path_state_hash: Sha256Hex | None = None
    aggregate_state_hash: Sha256Hex | None = None


class AuthorizationScope(ContractModel):
    symbol: Symbol
    asset_class: AssetClass
    intent: Intent
    legs: list[AuthorizedLeg] = Field(min_length=1, max_length=MAX_LEGS)
    total_quantity: PositiveDecimalStr
    max_notional: DecimalStr | None

    @model_validator(mode="after")
    def _consistent(self) -> AuthorizationScope:
        indices = [leg.leg_index for leg in self.legs]
        if indices != sorted(set(indices)):
            raise ValueError("scope legs must be ordered by leg_index with no duplicates")
        total = Decimal(0)
        for leg in self.legs:
            if leg.symbol != self.symbol:
                raise ValueError(f"leg {leg.leg_index} symbol {leg.symbol!r} differs from scope symbol {self.symbol!r}")
            if leg.is_option != (self.asset_class == "equity_option"):
                raise ValueError(f"leg {leg.leg_index} does not match asset_class {self.asset_class}")
            total = DECIMAL_CONTEXT.add(total, dec(leg.quantity))
        if total != dec(self.total_quantity):
            raise ValueError("scope leg quantities must sum to total_quantity")
        return self


class ExecutionAuthorization(ContractModel):
    schema_version: SchemaVersion
    auth_id: Uuid7Str
    decision_id: Uuid7Str
    proposal_id: Sha256Hex
    tenant_id: TenantId
    agent_id: AgentId
    policy: PolicyRef
    engine_version: NonEmptyStr
    issued_at: Rfc3339
    expires_at: Rfc3339
    ttl_seconds: int = Field(ge=TTL_MIN_SECONDS, le=TTL_MAX_SECONDS)
    scope: AuthorizationScope
    idempotency_key: IdempotencyKey
    environment: Environment
    single_use: StrictTrue
    authorization_hash: Sha256Hex
    bound_state: BoundState

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> ExecutionAuthorization:
        lifetime = parse_ts(self.expires_at) - parse_ts(self.issued_at)
        if lifetime != timedelta(seconds=self.ttl_seconds):
            raise ValueError("expires_at - issued_at must equal ttl_seconds exactly")
        expected_key = idempotency_key_for(self.tenant_id, self.proposal_id, self.scope.legs)
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency_key does not match idempotency_key_for(tenant_id, proposal_id, scope.legs)")
        if self.bound_state.policy_hash != self.policy.hash:
            raise ValueError("bound_state.policy_hash must equal policy.hash")
        return self

    @model_validator(mode="wrap")
    @classmethod
    def _hash_covers_the_content_as_presented(
        cls, data: Any, handler: Any, info: ValidationInfo
    ) -> ExecutionAuthorization:
        """See :func:`verify_presented_hash` - the hash covers what was written, not what we'd write."""
        model: ExecutionAuthorization = handler(data)
        verify_presented_hash(
            model, data, info, field="authorization_hash", compute=authorization_hash_for, message="authorization_hash does not match the canonical hash of the authorization content"
        )
        return model

    @classmethod
    def build(cls, **fields: Any) -> ExecutionAuthorization:
        """Construct an authorization, computing ``authorization_hash``; ``idempotency_key`` and ``expires_at`` are
        derived when not supplied (``environment`` defaults to ``"paper"`` and ``single_use`` to ``True``)."""
        payload: dict[str, Any] = dict(fields)
        payload.setdefault("environment", "paper")
        payload.setdefault("single_use", True)
        scope = payload["scope"]
        if not isinstance(scope, AuthorizationScope):
            scope = AuthorizationScope.model_validate(scope)
        payload["scope"] = scope
        payload.setdefault("idempotency_key", idempotency_key_for(payload["tenant_id"], payload["proposal_id"], scope.legs))
        if "expires_at" not in payload:
            issued = parse_ts(payload["issued_at"])
            payload["expires_at"] = format_ts(issued + timedelta(seconds=int(payload["ttl_seconds"])))
        return build_hashed(cls, "authorization_hash", _authorization_hash, payload)


def _authorization_hash(payload: Mapping[str, Any]) -> str:
    return authorization_hash_for(payload)


__all__ = [
    "IDEMPOTENCY_KEY_PATTERN",
    "TTL_MAX_SECONDS",
    "TTL_MIN_SECONDS",
    "AuthorizationScope",
    "AuthorizedLeg",
    "BoundState",
    "ExecutionAuthorization",
    "IdempotencyKey",
]
