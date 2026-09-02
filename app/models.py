from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class InstrumentType(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class TradeProposal(BaseModel):
    """A single-leg US-equity proposal.

    ``instrument_type`` carries a default so payloads and stored rows written
    before options support still validate unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_type: Literal["equity"] = "equity"
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str = Field(min_length=1, max_length=16)
    side: Side
    quantity: int = Field(gt=0)
    estimated_price: float = Field(gt=0)
    strategy_confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=1)
    invalidation_condition: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional(self) -> float:
        return self.quantity * self.estimated_price


# Explicit alias for call sites that want to name the equity member of the
# proposal union. `TradeProposal` keeps its identity so existing imports,
# constructor calls, and isinstance checks are untouched.
EquityTradeProposal = TradeProposal


class PortfolioPosition(BaseModel):
    """Broker-neutral view of one open portfolio position."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    quantity: float = Field(allow_inf_nan=False)
    market_value: float = Field(allow_inf_nan=False)
    current_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    unrealized_pl: float | None = Field(default=None, allow_inf_nan=False)
    unrealized_pl_pct: float | None = Field(default=None, allow_inf_nan=False)


class PortfolioSnapshot(BaseModel):
    """Provider-neutral portfolio state consumed by deterministic policy.

    ``daily_pnl_pct`` is optional on purpose: unavailable brokerage data must
    remain unavailable instead of being represented as a safe-looking zero.
    The risk engine treats ``None`` as a deterministic block.
    """

    equity: float = Field(gt=0, allow_inf_nan=False)
    cash: float = Field(allow_inf_nan=False)
    buying_power: float = Field(ge=0, allow_inf_nan=False)
    daily_pnl_pct: float | None = Field(default=None, allow_inf_nan=False)
    current_positions: dict[str, float] = Field(default_factory=dict)
    positions: list[PortfolioPosition] = Field(default_factory=list)
    source: Literal["MANUAL", "ALPACA_PAPER"] = "MANUAL"


class MarketRiskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    annualized_volatility: float = Field(ge=0)
    max_drawdown_30d: float = Field(ge=0, le=1)
    liquidity_score: float = Field(ge=0, le=1)


class RiskRuleResult(BaseModel):
    rule: str
    passed: bool
    severity: Literal["INFO", "WATCH", "HIGH", "BLOCK"]
    message: str
    recommended_quantity: int | None = None


class RiskReport(BaseModel):
    proposal_id: str
    symbol: str
    original_quantity: int
    recommended_quantity: int
    blocked: bool
    risk_score: int = Field(ge=0, le=100)
    reasons: list[str]
    checks: list[RiskRuleResult]


class AIRiskAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    recommendation: Decision
    confidence: float = Field(ge=0, le=1)
    recommended_quantity: int = Field(ge=0)
    risk_thesis: str = Field(min_length=1)
    hidden_risks: list[str]
    reasoning: list[str]
    model_name: str = Field(min_length=1)

    @field_validator("risk_thesis", "model_name")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class GovernorDecision(BaseModel):
    proposal_id: str
    symbol: str | None = None
    side: Side | None = None
    decision: Decision
    original_quantity: int = Field(gt=0)
    approved_quantity: int = Field(ge=0)
    reason: str
    risk_score: int = Field(ge=0, le=100)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    proposal_id: str
    actor: Literal["risk_engine", "ai_risk_agent", "governor", "execution"]
    action: str
    payload: dict
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
