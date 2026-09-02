"""RiskContext: every input the engine needs, assembled by the context provider and captured verbatim.

Missing data is ``None``, never zero (Hard Rule E2). Path state, aggregate multi-agent exposure, agent budgets, the
graduated-response level and the calendar are *inputs* here (ADR-0006) so that ``risk.evaluate`` stays a pure
function and replay reproduces them exactly.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from mizan.contracts._base import ContractModel
from mizan.contracts.trade_proposal import AssetClass, Side
from mizan.contracts.types import (
    AgentId,
    DecimalStr,
    NonEmptyStr,
    NonNegativeDecimalStr,
    OccSymbol,
    PolicyId,
    PositiveDecimalStr,
    RatioStr,
    Rfc3339,
    SchemaVersion,
    SemVer,
    Sha256Hex,
    Symbol,
    TenantId,
)

Count = Annotated[int, Field(ge=0)]
ResponseLevel = Annotated[int, Field(ge=0, le=5)]
Session = Literal["pre", "open", "close", "after", "closed"]
Direction = Literal["long", "short"]


class Quote(ContractModel):
    symbol: Symbol
    price: PositiveDecimalStr
    bid: PositiveDecimalStr | None
    ask: PositiveDecimalStr | None
    as_of: Rfc3339
    source: NonEmptyStr
    adv: NonNegativeDecimalStr | None = None
    spread_pct: RatioStr | None = None


class OptionQuote(ContractModel):
    occ_symbol: OccSymbol
    mark: PositiveDecimalStr
    delta: DecimalStr | None
    gamma: DecimalStr | None
    vega: DecimalStr | None
    theta: DecimalStr | None
    as_of: Rfc3339
    source: NonEmptyStr
    open_interest: Count | None = None
    spread_pct: RatioStr | None = None
    iv: NonNegativeDecimalStr | None = None


class MarketSnapshot(ContractModel):
    snapshot_id: NonEmptyStr
    as_of: Rfc3339
    quotes: dict[Symbol, Quote]
    option_quotes: dict[OccSymbol, OptionQuote] = Field(default_factory=dict)
    sectors: dict[Symbol, NonEmptyStr] = Field(default_factory=dict)
    source: NonEmptyStr

    @model_validator(mode="after")
    def _keys_match(self) -> MarketSnapshot:
        for key, quote in self.quotes.items():
            if quote.symbol != key:
                raise ValueError(f"quotes[{key!r}] carries symbol {quote.symbol!r}")
        for key, quote in self.option_quotes.items():
            if quote.occ_symbol != key:
                raise ValueError(f"option_quotes[{key!r}] carries occ_symbol {quote.occ_symbol!r}")
        return self


class Position(ContractModel):
    symbol: Symbol
    asset_class: AssetClass
    quantity: DecimalStr
    market_value: DecimalStr
    sector: NonEmptyStr | None
    occ_symbol: OccSymbol | None
    delta: DecimalStr | None
    gamma: DecimalStr | None
    vega: DecimalStr | None

    @model_validator(mode="after")
    def _occ(self) -> Position:
        if self.asset_class == "equity_option" and self.occ_symbol is None:
            raise ValueError("an equity_option position requires occ_symbol")
        if self.asset_class == "equity" and self.occ_symbol is not None:
            raise ValueError("an equity position must not carry occ_symbol")
        return self


class PortfolioGreeks(ContractModel):
    delta: DecimalStr | None
    gamma: DecimalStr | None
    vega: DecimalStr | None
    short_gamma: DecimalStr | None = None
    long_gamma: DecimalStr | None = None
    short_vega: DecimalStr | None = None
    long_vega: DecimalStr | None = None


class PortfolioSnapshot(ContractModel):
    snapshot_id: NonEmptyStr
    as_of: Rfc3339
    equity: PositiveDecimalStr
    cash: DecimalStr
    buying_power: NonNegativeDecimalStr | None
    peak_equity: PositiveDecimalStr | None
    daily_pnl: DecimalStr | None
    positions: list[Position]
    greeks: PortfolioGreeks | None
    source: NonEmptyStr
    gross_exposure: NonNegativeDecimalStr | None = None
    net_exposure: DecimalStr | None = None
    margin_requirement: NonNegativeDecimalStr | None = None
    maintenance_excess: DecimalStr | None = None
    factor_exposures: dict[NonEmptyStr, DecimalStr] | None = None


class RecentOrder(ContractModel):
    proposal_id: Sha256Hex
    symbol: Symbol
    side: Side
    total_quantity: PositiveDecimalStr
    submitted_at: Rfc3339
    status: NonEmptyStr


class PolicyRef(ContractModel):
    policy_id: PolicyId
    version: SemVer
    hash: Sha256Hex


class PathState(ContractModel):
    """Running path-dependent state of the account (R-ERG-2)."""

    as_of: Rfc3339
    peak_equity: PositiveDecimalStr
    current_drawdown_pct: RatioStr
    consecutive_losses: Count
    days_under_water: Count
    daily_pnl_pct: DecimalStr | None = None
    realized_expectancy: DecimalStr | None = None
    sample_size: Count


class PendingIntent(ContractModel):
    agent_id: AgentId
    symbol: Symbol
    direction: Direction
    notional: DecimalStr
    proposed_at: Rfc3339
    model_provider: NonEmptyStr | None = None


class AggregateState(ContractModel):
    """Book-level exposure across every agent of the tenant (R-AGG-1..6)."""

    as_of: Rfc3339
    gross_exposure: NonNegativeDecimalStr
    net_exposure: DecimalStr
    exposure_pct_of_equity: RatioStr
    exposure_by_agent: dict[AgentId, DecimalStr] = Field(default_factory=dict)
    exposure_by_model_provider: dict[NonEmptyStr, DecimalStr] = Field(default_factory=dict)
    exposure_by_signal_source: dict[NonEmptyStr, DecimalStr] = Field(default_factory=dict)
    exposure_by_sector: dict[NonEmptyStr, DecimalStr] = Field(default_factory=dict)
    pending_intents: list[PendingIntent] = Field(default_factory=list)
    crowding_score: RatioStr | None = None
    days_to_liquidate_book: DecimalStr | None = None


class Calibration(ContractModel):
    claimed_confidence_mean: RatioStr
    realized_hit_rate: RatioStr
    sample_size: Count
    expectancy: DecimalStr | None = None


class AgentState(ContractModel):
    as_of: Rfc3339
    daily_notional_used: NonNegativeDecimalStr
    daily_order_count: Count
    open_positions: Count
    calibration: Calibration | None = None


class CalendarState(ContractModel):
    session: Session
    minutes_since_open: Count | None = None
    minutes_to_close: Count | None = None
    earnings_within_days: dict[Symbol, Count] = Field(default_factory=dict)
    macro_event_within_minutes: Count | None = None
    is_holiday_or_half_day: bool


class RiskContext(ContractModel):
    schema_version: SchemaVersion
    context_id: NonEmptyStr
    tenant_id: TenantId
    agent_id: AgentId
    evaluated_at: Rfc3339
    policy: PolicyRef
    market_snapshot: MarketSnapshot | None
    portfolio_snapshot: PortfolioSnapshot | None
    recent_orders: list[RecentOrder] = Field(default_factory=list)
    engine_version: NonEmptyStr
    path_state: PathState | None = None
    aggregate_state: AggregateState | None = None
    agent_state: AgentState | None = None
    response_level: ResponseLevel = 0
    calendar: CalendarState | None = None


__all__ = [
    "AgentState",
    "AggregateState",
    "CalendarState",
    "Calibration",
    "Count",
    "Direction",
    "MarketSnapshot",
    "OptionQuote",
    "PathState",
    "PendingIntent",
    "PolicyRef",
    "PortfolioGreeks",
    "PortfolioSnapshot",
    "Position",
    "Quote",
    "RecentOrder",
    "ResponseLevel",
    "RiskContext",
    "Session",
]
