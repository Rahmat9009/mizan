from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Decision, Side


class ExecutionState(str, Enum):
    DISABLED = "EXECUTION_DISABLED"
    BLOCKED = "BLOCKED"
    AUTHORIZED = "AUTHORIZED"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    MARKET_CLOSED = "MARKET_CLOSED"
    ASSET_NOT_TRADABLE = "ASSET_NOT_TRADABLE"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    WOULD_SUBMIT = "WOULD_SUBMIT"
    RECONCILED_EXISTING_ORDER = "RECONCILED_EXISTING_ORDER"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


class ExecutionMode(str, Enum):
    PAPER_DRY_RUN = "ALPACA_PAPER_DRY_RUN"
    PAPER = "ALPACA_PAPER"


class OrderLifecycleState(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.REJECTED,
        }


class ExecutionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=16)
    side: Side
    original_quantity: int = Field(gt=0)
    approved_quantity: int = Field(gt=0)
    governor_decision: Decision
    governor_decided_at: datetime
    authorization_created_at: datetime
    risk_score: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_authorized_size(self) -> "ExecutionAuthorization":
        if self.governor_decision not in {Decision.APPROVE, Decision.REDUCE}:
            raise ValueError("Only APPROVE or REDUCE can be authorized.")
        if self.approved_quantity > self.original_quantity:
            raise ValueError("Authorization cannot exceed the original quantity.")
        return self


class IntendedPaperOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    symbol: str = Field(min_length=1, max_length=16)
    side: Side
    quantity: int = Field(gt=0)
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False


class BrokerOrder(BaseModel):
    """Broker-neutral order state; raw Alpaca SDK objects never escape the adapter."""

    model_config = ConfigDict(extra="forbid")

    alpaca_order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: float = Field(gt=0, allow_inf_nan=False)
    status: str = Field(min_length=1)
    submitted_at: datetime
    filled_at: datetime | None = None
    filled_quantity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    filled_avg_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class BrokerOrderLeg(BaseModel):
    """One leg of a multi-leg broker order.

    Alpaca returns each leg of an MLEG order as a full order object, so every
    field here comes from the broker. Nothing is derived from the parent: a
    value the broker does not report stays ``None`` rather than being inferred.
    """

    model_config = ConfigDict(extra="forbid")

    alpaca_order_id: str = Field(min_length=1)
    option_symbol: str = Field(min_length=1, max_length=32)
    side: Side
    ratio: float = Field(gt=0, allow_inf_nan=False)
    lifecycle_status: OrderLifecycleState
    broker_status: str = Field(min_length=1)
    filled_quantity: float = Field(default=0, ge=0, allow_inf_nan=False)
    filled_avg_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    filled_at: datetime | None = None


class BrokerOrderSnapshot(BaseModel):
    """Durable, provider-neutral order lifecycle returned by reconciliation.

    ``asset_class``, ``order_class``, ``underlying`` and ``legs`` carry
    equity-shaped defaults so every snapshot written before options support
    still validates unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    alpaca_order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    quantity: float = Field(gt=0, allow_inf_nan=False)
    lifecycle_status: OrderLifecycleState
    broker_status: str = Field(min_length=1)
    submitted_at: datetime
    filled_at: datetime | None = None
    filled_quantity: float = Field(default=0, ge=0, allow_inf_nan=False)
    filled_avg_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    paper: Literal[True] = True
    asset_class: Literal["us_equity", "us_option"] = "us_equity"
    order_class: Literal["simple", "mleg"] = "simple"
    underlying: str | None = Field(default=None, max_length=16)
    legs: list[BrokerOrderLeg] | None = None


class MarketClockSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class ExecutionAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    status: str = Field(min_length=1)
    tradable: bool


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    client_order_id: str | None = Field(default=None, max_length=128)
    alpaca_order_id: str | None = None
    symbol: str = Field(min_length=1, max_length=16)
    side: Side
    quantity: int = Field(ge=0)
    status: ExecutionState
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_quantity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    filled_avg_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    broker_status: str | None = None
    execution_mode: ExecutionMode
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    paper: Literal[True] = True
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def validate_state_requirements(self) -> "ExecutionResult":
        order_states = {
            ExecutionState.WOULD_SUBMIT,
            ExecutionState.RECONCILED_EXISTING_ORDER,
            ExecutionState.SUBMITTED,
        }
        if self.status in order_states:
            if self.quantity <= 0 or not self.client_order_id:
                raise ValueError("Order result states require quantity and client_order_id.")
        if self.status in {
            ExecutionState.RECONCILED_EXISTING_ORDER,
            ExecutionState.SUBMITTED,
        }:
            if not self.alpaca_order_id or self.submitted_at is None or not self.broker_status:
                raise ValueError("Broker order states require mapped Alpaca order fields.")
        if self.status == ExecutionState.WOULD_SUBMIT and self.execution_mode != ExecutionMode.PAPER_DRY_RUN:
            raise ValueError("WOULD_SUBMIT is valid only in dry-run mode.")
        if self.status == ExecutionState.SUBMITTED and self.execution_mode != ExecutionMode.PAPER:
            raise ValueError("SUBMITTED is valid only in paper execution mode.")
        return self
