"""TradeProposal: what an agent asks to do. ``reasoning`` is audit-only and never enters enforcement."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator

from mizan.contracts._base import ContractModel, build_hashed, hash_check_skipped
from mizan.contracts.canonical import DECIMAL_CONTEXT, proposal_id_for
from mizan.contracts.types import (
    AgentId,
    DateStr,
    DecimalStr,
    NonEmptyStr,
    PositiveDecimalStr,
    RatioStr,
    Rfc3339,
    SchemaVersion,
    Sha256Hex,
    Symbol,
    dec,
    parse_ts,
)

AgentType = Literal["trader", "analyst", "portfolio_manager"]
Framework = Literal["tradingagents", "ai-hedge-fund", "custom"]
Side = Literal["buy", "sell"]
ContractType = Literal["call", "put"]
OrderType = Literal["limit", "market"]
Intent = Literal["open", "close", "adjust"]
AssetClass = Literal["equity", "equity_option"]
Strategy = Literal[
    "long_equity",
    "short_equity",
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "bull_put_spread",
    "bear_call_spread",
    "iron_condor",
    "custom",
]
InvalidationDirection = Literal["below", "above"]

# strategy -> (min legs, max legs)
STRATEGY_LEG_COUNTS: dict[str, tuple[int, int]] = {
    "long_equity": (1, 1),
    "short_equity": (1, 1),
    "long_call": (1, 1),
    "long_put": (1, 1),
    "bull_call_spread": (2, 2),
    "bear_put_spread": (2, 2),
    "bull_put_spread": (2, 2),
    "bear_call_spread": (2, 2),
    "iron_condor": (4, 4),
    "custom": (1, 4),
}
STRATEGIES: tuple[str, ...] = tuple(STRATEGY_LEG_COUNTS)
EQUITY_STRATEGIES: frozenset[str] = frozenset({"long_equity", "short_equity"})
OPTION_STRATEGIES: frozenset[str] = frozenset(STRATEGIES) - EQUITY_STRATEGIES - {"custom"}
MAX_LEGS = 4
MAX_SIGNAL_SOURCES = 16
MAX_REASONING_CHARS = 20000
# One US equity option contract controls 100 shares; notional and greeks are scaled by it.
OPTION_CONTRACT_MULTIPLIER = Decimal(100)
_OCC_STRIKE_SCALE = Decimal(1000)


def occ_symbol_for(underlying: str, contract_type: str, expiry: str, strike: str) -> str:
    """Derive the OCC symbol ``ROOT + YYMMDD + C|P + strike*1000 (8 digits)`` for an option leg.

    Raises ``ValueError`` when the underlying is not a valid OCC root (1-6 upper-case alphanumerics) or the strike
    cannot be expressed in thousandths.
    """
    root = str(underlying)
    if not (1 <= len(root) <= 6) or not root.isalnum() or root.upper() != root:
        raise ValueError(f"{root!r} is not a valid OCC root (1-6 upper-case alphanumerics)")
    year, month, day = expiry.split("-")
    scaled = DECIMAL_CONTEXT.multiply(dec(strike), _OCC_STRIKE_SCALE)
    if scaled != scaled.to_integral_value() or scaled <= 0 or scaled >= 10**8:
        raise ValueError(f"strike {strike!r} cannot be expressed as an OCC strike (thousandths, 8 digits)")
    letter = "C" if contract_type == "call" else "P"
    return f"{root}{year[2:]}{month}{day}{letter}{int(scaled):08d}"


class AgentIdentity(ContractModel):
    agent_id: AgentId
    agent_type: AgentType
    agent_version: NonEmptyStr
    framework: Framework


class ModelIdentity(ContractModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    version: NonEmptyStr
    prompt_hash: Sha256Hex


class Invalidation(ContractModel):
    """The price level at which the thesis is wrong. Agent-supplied; the policy decides whether it is required."""

    level: PositiveDecimalStr
    direction: InvalidationDirection
    target: PositiveDecimalStr | None = None


class Leg(ContractModel):
    leg_index: int = Field(ge=0, le=MAX_LEGS - 1)
    side: Side
    contract_type: ContractType | None
    strike: PositiveDecimalStr | None
    expiry: DateStr | None
    quantity: PositiveDecimalStr
    limit_price: PositiveDecimalStr | None
    order_type: OrderType

    @model_validator(mode="after")
    def _consistent(self) -> Leg:
        option_fields = (self.contract_type, self.strike, self.expiry)
        present = [field is not None for field in option_fields]
        if any(present) and not all(present):
            raise ValueError("contract_type, strike and expiry must be all present (option) or all null (equity)")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("a market order must not carry a limit_price")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("a limit order requires a limit_price")
        return self

    @property
    def is_option(self) -> bool:
        return self.contract_type is not None

    def occ_symbol(self, underlying: str) -> str:
        if not self.is_option:
            raise ValueError("an equity leg has no OCC symbol")
        assert self.contract_type is not None and self.expiry is not None and self.strike is not None
        return occ_symbol_for(underlying, self.contract_type, self.expiry, self.strike)


class TradeProposal(ContractModel):
    schema_version: SchemaVersion
    proposal_id: Sha256Hex
    agent: AgentIdentity
    model: ModelIdentity
    created_at: Rfc3339
    expires_at: Rfc3339
    intent: Intent
    symbol: Symbol
    asset_class: AssetClass
    strategy: Strategy
    legs: list[Leg] = Field(min_length=1, max_length=MAX_LEGS)
    reasoning: str = Field(default="", max_length=MAX_REASONING_CHARS)
    market_snapshot_ref: NonEmptyStr
    portfolio_snapshot_ref: NonEmptyStr
    confidence: RatioStr | None = None
    signal_sources: list[NonEmptyStr] = Field(default_factory=list, max_length=MAX_SIGNAL_SOURCES)
    invalidation: Invalidation | None = None

    @model_validator(mode="after")
    def _structure(self, info: ValidationInfo) -> TradeProposal:
        if parse_ts(self.expires_at) <= parse_ts(self.created_at):
            raise ValueError("expires_at must be after created_at")
        for position, leg in enumerate(self.legs):
            if leg.leg_index != position:
                raise ValueError(f"legs[{position}].leg_index must be {position}")
        low, high = STRATEGY_LEG_COUNTS[self.strategy]
        if not low <= len(self.legs) <= high:
            raise ValueError(f"strategy {self.strategy} requires {low}..{high} legs, got {len(self.legs)}")
        if self.asset_class == "equity":
            if any(leg.is_option for leg in self.legs):
                raise ValueError("equity proposals must not carry option fields on any leg")
            if self.strategy in OPTION_STRATEGIES:
                raise ValueError(f"strategy {self.strategy} requires asset_class equity_option")
        else:
            if not all(leg.is_option for leg in self.legs):
                raise ValueError("equity_option proposals require contract_type, strike and expiry on every leg")
            if self.strategy in EQUITY_STRATEGIES:
                raise ValueError(f"strategy {self.strategy} requires asset_class equity")
            for leg in self.legs:
                leg.occ_symbol(self.symbol)  # raises when the leg cannot name an OCC contract
        if len(set(self.signal_sources)) != len(self.signal_sources):
            raise ValueError("signal_sources must not contain duplicates")
        if not hash_check_skipped(info) and self.proposal_id != proposal_id_for(self):
            raise ValueError("proposal_id does not match the canonical hash of the proposal content")
        return self

    @property
    def total_quantity(self) -> Decimal:
        total = Decimal(0)
        for leg in self.legs:
            total = DECIMAL_CONTEXT.add(total, dec(leg.quantity))
        return total

    @property
    def notional_estimate(self) -> Decimal | None:
        """Sum of ``quantity x limit_price`` (x 100 per option contract); ``None`` unless every leg has a limit."""
        multiplier = OPTION_CONTRACT_MULTIPLIER if self.asset_class == "equity_option" else Decimal(1)
        total = Decimal(0)
        for leg in self.legs:
            if leg.limit_price is None:
                return None
            leg_value = DECIMAL_CONTEXT.multiply(dec(leg.quantity), dec(leg.limit_price))
            total = DECIMAL_CONTEXT.add(total, DECIMAL_CONTEXT.multiply(leg_value, multiplier))
        return total

    @property
    def occ_symbols(self) -> list[str]:
        """Derived OCC symbols, one per leg, in leg order (empty for equity proposals)."""
        return [leg.occ_symbol(self.symbol) for leg in self.legs if leg.is_option]

    @classmethod
    def build(cls, **fields: Any) -> TradeProposal:
        """Construct a proposal, computing ``proposal_id`` from the normalised content."""
        return build_hashed(cls, "proposal_id", proposal_id_for, fields)


DecimalStr_ = DecimalStr  # re-exported for type-checking convenience in lane code

__all__ = [
    "EQUITY_STRATEGIES",
    "MAX_LEGS",
    "MAX_REASONING_CHARS",
    "MAX_SIGNAL_SOURCES",
    "OPTION_CONTRACT_MULTIPLIER",
    "OPTION_STRATEGIES",
    "STRATEGIES",
    "STRATEGY_LEG_COUNTS",
    "AgentIdentity",
    "AgentType",
    "AssetClass",
    "ContractType",
    "Framework",
    "Intent",
    "Invalidation",
    "InvalidationDirection",
    "Leg",
    "ModelIdentity",
    "OrderType",
    "Side",
    "Strategy",
    "TradeProposal",
    "occ_symbol_for",
]
