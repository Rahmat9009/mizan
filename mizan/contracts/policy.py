"""Policy: the versioned, hashed rule set a tenant governs its agents with.

``FailClosed.on_missing_*`` are ``Literal[True]``: the contract cannot express turning them off. ``environment``
does not exist here or anywhere else -- there is only paper.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, ValidationInfo, model_validator

from mizan.contracts._base import ContractModel, build_hashed, hash_check_skipped
from mizan.contracts.canonical import policy_hash_for
from mizan.contracts.risk_context import PolicyRef
from mizan.contracts.trade_proposal import STRATEGIES, Strategy
from mizan.contracts.types import (
    HHMM,
    AgentId,
    DecimalStr,
    NonEmptyStr,
    NonNegativeDecimalStr,
    PolicyId,
    PositiveDecimalStr,
    RatioStr,
    SchemaVersion,
    SemVer,
    Sha256Hex,
    StrictTrue,
    Symbol,
    TenantId,
    dec,
)

CHECK_IDS: tuple[str, ...] = (
    # always-on, cannot be disabled
    "market_data_presence",
    "portfolio_state_presence",
    "proposal_expiry",
    # base checks
    "restricted_symbol",
    "restricted_strategy",
    "leg_limit",
    "position_limit",
    "capital_threshold",
    "buying_power_sufficiency",
    "buying_power_utilization",
    "concentration_limit",
    "sector_concentration",
    "drawdown_limit",
    "duplicate_order",
    "erroneous_order",
    "days_to_expiry",
    "options_delta_limit",
    "options_gamma_limit",
    "options_vega_limit",
    # Addendum 1 -- appended, order preserved
    "response_level_gate",
    "agent_budget",
    "invalidation_defined",
    "reward_risk",
    "risk_per_trade",
    "drawdown_size_scaling",
    "consecutive_loss_review",
    "aggregate_exposure",
    "correlated_intent",
    "model_provider_concentration",
    "signal_source_concentration",
    "crowding",
    "liquidity_adv",
    "option_liquidity",
    "time_blackout",
    "session_window",
    "options_short_gamma_limit",
    "options_short_vega_limit",
    "assignment_risk",
    "pin_risk",
    "stress_scenarios",
    "absorbing_barrier",
    "factor_exposure",
    "book_liquidation_time",
    # REQ-35. APPENDED, never inserted: CHECK_INDEX is positional and BASE_CHECK_IDS slices the first
    # 19, so putting this anywhere else would silently renumber every existing check.
    "account_capability",
    # F-31 / R-OPT-3. APPENDED like account_capability, for the same reason: CHECK_INDEX is positional.
    "structure_valid",
)
BASE_CHECK_IDS: tuple[str, ...] = CHECK_IDS[:19]
ALWAYS_ON_CHECKS: tuple[str, ...] = (
    "market_data_presence",
    "portfolio_state_presence",
    "proposal_expiry",
    "response_level_gate",
)
CHECK_INDEX: dict[str, int] = {check_id: index for index, check_id in enumerate(CHECK_IDS)}

# The optional policy section each check reads. ``None`` = always available (no section, or a required section).
CHECK_SECTIONS: dict[str, str | None] = {
    "account_capability": "account",
    # No section: a defined-risk rule needs no configuration to be correct, and making it optional
    # would let a policy switch off the thing that makes max loss computable at all.
    "structure_valid": None,
    "market_data_presence": None,
    "portfolio_state_presence": None,
    "proposal_expiry": None,
    "restricted_symbol": None,
    "restricted_strategy": None,
    "leg_limit": None,
    "position_limit": None,
    "capital_threshold": None,
    "buying_power_sufficiency": None,
    "buying_power_utilization": None,
    "concentration_limit": None,
    "sector_concentration": None,
    "drawdown_limit": None,
    "duplicate_order": None,
    "erroneous_order": None,
    "days_to_expiry": "options",
    "options_delta_limit": "options",
    "options_gamma_limit": "options",
    "options_vega_limit": "options",
    "response_level_gate": None,
    "agent_budget": None,
    "invalidation_defined": "trade",
    "reward_risk": "trade",
    "risk_per_trade": "trade",
    "drawdown_size_scaling": "path",
    "consecutive_loss_review": "path",
    "aggregate_exposure": "aggregate",
    "correlated_intent": "aggregate",
    "model_provider_concentration": "aggregate",
    "signal_source_concentration": "aggregate",
    "crowding": "aggregate",
    "liquidity_adv": "liquidity",
    "option_liquidity": "liquidity",
    "time_blackout": "time",
    "session_window": "time",
    "options_short_gamma_limit": "options",
    "options_short_vega_limit": "options",
    "assignment_risk": "options",
    "pin_risk": "options",
    "stress_scenarios": "tail",
    "absorbing_barrier": "tail",
    "factor_exposure": "factor",
    "book_liquidation_time": "aggregate",
}
assert set(CHECK_SECTIONS) == set(CHECK_IDS)


def _check_id(value: str) -> str:
    if value not in CHECK_INDEX:
        raise ValueError(f"unknown check_id {value!r}")
    return value


CheckIdStr = Annotated[str, AfterValidator(_check_id)]
CheckSeverity = Literal["blocking", "warning"]
AuthorityCeiling = Literal["reduce_or_reject"]
KELLY_FRACTION_CAP_MAX = "0.5"


class OrderLimits(ContractModel):
    max_notional: PositiveDecimalStr
    max_quantity: PositiveDecimalStr
    max_legs: int = Field(ge=1, le=4)


class PortfolioLimits(ContractModel):
    max_single_symbol_pct: RatioStr
    max_sector_concentration_pct: RatioStr | None
    max_drawdown_pct: RatioStr
    max_buying_power_utilization: RatioStr


class OptionsLimits(ContractModel):
    max_portfolio_delta: NonNegativeDecimalStr
    max_portfolio_gamma: NonNegativeDecimalStr
    max_portfolio_vega: NonNegativeDecimalStr
    min_days_to_expiry: int = Field(ge=0)
    max_days_to_expiry: int = Field(ge=1)
    max_short_gamma: NonNegativeDecimalStr | None = None
    max_long_gamma: NonNegativeDecimalStr | None = None
    max_short_vega: NonNegativeDecimalStr | None = None
    max_long_vega: NonNegativeDecimalStr | None = None
    undefined_risk_requires_approval: StrictTrue = True
    assignment_risk_check: bool = True
    pin_risk_buffer_pct: RatioStr | None = None

    @model_validator(mode="after")
    def _dte(self) -> OptionsLimits:
        if self.max_days_to_expiry <= self.min_days_to_expiry:
            raise ValueError("max_days_to_expiry must be greater than min_days_to_expiry")
        return self


class AccountPolicy(ContractModel):
    """What the tenant requires of the BROKER ACCOUNT before any order may be placed (REQ-35).

    Separate from every other section because it constrains the account rather than the order: the same
    proposal is fine on one account and forbidden on another, and that is a fact about permission, not
    about risk.
    """

    require_active: bool = True
    min_options_trading_level: int | None = Field(default=None, ge=0, le=3)
    require_shorting_enabled_for_short_legs: bool = True


class Restricted(ContractModel):
    symbols: list[Symbol] = Field(default_factory=list)
    strategies: list[Strategy] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Restricted:
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("restricted.symbols must not contain duplicates")
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("restricted.strategies must not contain duplicates")
        return self


class CheckConfig(ContractModel):
    enabled: bool = True
    severity: CheckSeverity = "blocking"
    window_seconds: int | None = Field(default=None, ge=1)
    price_deviation_threshold: RatioStr | None = None
    quantity_deviation_threshold: PositiveDecimalStr | None = None


class AdvisoryConfig(ContractModel):
    enabled: bool
    profile: NonEmptyStr
    authority_ceiling: AuthorityCeiling


class AuthorizationConfig(ContractModel):
    ttl_seconds: int = Field(default=15, ge=5, le=30)


class FailClosed(ContractModel):
    on_missing_market_data: StrictTrue = True
    on_missing_portfolio_state: StrictTrue = True
    on_engine_degraded: StrictTrue = True
    on_advisory_unavailable: bool = False


class TradeLimits(ContractModel):
    max_risk_per_trade_pct: RatioStr | None = None
    min_reward_risk: PositiveDecimalStr | None = None
    require_invalidation: bool = False
    confidence_haircut: RatioStr = "0"
    kelly_fraction_cap: RatioStr | None = None

    @model_validator(mode="after")
    def _kelly(self) -> TradeLimits:
        if self.kelly_fraction_cap is not None and dec(self.kelly_fraction_cap) > dec(KELLY_FRACTION_CAP_MAX):
            raise ValueError(f"kelly_fraction_cap must not exceed {KELLY_FRACTION_CAP_MAX} (R-KELLY-1)")
        return self


class DrawdownScalingStep(ContractModel):
    drawdown_pct: RatioStr
    size_multiplier: RatioStr


class PathPolicy(ContractModel):
    size_scaling_by_drawdown: list[DrawdownScalingStep]
    max_consecutive_losses_before_review: int | None = Field(default=None, ge=1)
    max_days_under_water: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _monotone(self) -> PathPolicy:
        steps = self.size_scaling_by_drawdown
        for previous, current in zip(steps, steps[1:], strict=False):
            if dec(current.drawdown_pct) <= dec(previous.drawdown_pct):
                raise ValueError("size_scaling_by_drawdown must be strictly ascending in drawdown_pct")
            if dec(current.size_multiplier) > dec(previous.size_multiplier):
                raise ValueError("size_scaling_by_drawdown multipliers must be non-increasing (size scales DOWN)")
        return self


class AggregatePolicy(ContractModel):
    max_portfolio_exposure_pct: RatioStr
    max_correlated_intent_agents: int = Field(ge=1)
    correlated_intent_window_seconds: int = Field(ge=1)
    max_exposure_per_model_provider_pct: RatioStr | None = None
    max_exposure_per_signal_source_pct: RatioStr | None = None
    crowding_score_threshold: RatioStr | None = None
    max_days_to_liquidate_book: PositiveDecimalStr | None = None


class AgentBudget(ContractModel):
    max_daily_notional: PositiveDecimalStr | None = None
    max_daily_orders: int | None = Field(default=None, ge=1)
    max_open_positions: int | None = Field(default=None, ge=0)
    allowed_symbols: list[Symbol] | None = None
    active_hours_utc: list[HHMM] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def _hours(self) -> AgentBudget:
        if self.active_hours_utc is not None and self.active_hours_utc[0] == self.active_hours_utc[1]:
            raise ValueError("active_hours_utc start and end must differ")
        if self.allowed_symbols is not None and len(set(self.allowed_symbols)) != len(self.allowed_symbols):
            raise ValueError("allowed_symbols must not contain duplicates")
        return self


class ResponseTrigger(ContractModel):
    daily_loss_pct: RatioStr | None = None
    drawdown_pct: RatioStr | None = None

    @model_validator(mode="after")
    def _some_trigger(self) -> ResponseTrigger:
        if self.daily_loss_pct is None and self.drawdown_pct is None:
            raise ValueError("a response-ladder trigger needs daily_loss_pct or drawdown_pct")
        return self


class ResponseLevelSpec(ContractModel):
    level: int = Field(ge=1, le=5)
    trigger: ResponseTrigger
    size_multiplier: RatioStr
    new_risk_allowed: bool


class ResponseLadder(ContractModel):
    levels: list[ResponseLevelSpec] = Field(min_length=1)
    de_escalation_requires_human: StrictTrue = True

    @model_validator(mode="after")
    def _monotone(self) -> ResponseLadder:
        for previous, current in zip(self.levels, self.levels[1:], strict=False):
            if current.level <= previous.level:
                raise ValueError("response_ladder.levels must be strictly ascending in level")
            if dec(current.size_multiplier) > dec(previous.size_multiplier):
                raise ValueError("response_ladder size multipliers must be non-increasing with level")
        return self


class LiquidityPolicy(ContractModel):
    max_pct_of_adv: RatioStr
    max_option_spread_pct: RatioStr | None = None
    min_option_open_interest: int | None = Field(default=None, ge=0)
    max_estimated_impact_bps: NonNegativeDecimalStr | None = None


class TimePolicy(ContractModel):
    earnings_blackout_days_before: int = Field(ge=0)
    earnings_blackout_days_after: int = Field(ge=0)
    macro_event_blackout_minutes: int = Field(ge=0)
    no_trade_first_minutes: int = Field(ge=0)
    no_trade_last_minutes: int = Field(ge=0)
    max_overnight_exposure_pct: RatioStr | None = None


class TailPolicy(ContractModel):
    absorbing_barrier_equity_floor: PositiveDecimalStr | None = None
    stress_scenarios: list[NonEmptyStr] = Field(default_factory=list)
    max_loss_under_worst_scenario_pct: RatioStr | None = None
    expected_shortfall_99_limit: NonNegativeDecimalStr | None = None


class FactorPolicy(ContractModel):
    model_version: NonEmptyStr
    max_beta: NonNegativeDecimalStr | None = None
    max_sector_exposure_pct: RatioStr | None = None
    min_effective_independent_bets: int | None = Field(default=None, ge=1)
    max_factor_exposure: dict[NonEmptyStr, DecimalStr] = Field(default_factory=dict)


class Policy(ContractModel):
    schema_version: SchemaVersion
    policy_id: PolicyId
    policy_version: SemVer
    policy_hash: Sha256Hex
    tenant_id: TenantId
    order: OrderLimits
    portfolio: PortfolioLimits
    options: OptionsLimits | None
    restricted: Restricted
    checks: dict[CheckIdStr, CheckConfig]
    advisory: AdvisoryConfig
    authorization: AuthorizationConfig
    fail_closed: FailClosed
    trade: TradeLimits | None = None
    path: PathPolicy | None = None
    aggregate: AggregatePolicy | None = None
    agent_budgets: dict[AgentId, AgentBudget] = Field(default_factory=dict)
    response_ladder: ResponseLadder | None = None
    account: AccountPolicy | None = None
    liquidity: LiquidityPolicy | None = None
    time: TimePolicy | None = None
    tail: TailPolicy | None = None
    factor: FactorPolicy | None = None

    @model_validator(mode="after")
    def _rules(self, info: ValidationInfo) -> Policy:
        for check_id, config in self.checks.items():
            if check_id in ALWAYS_ON_CHECKS:
                if not config.enabled:
                    raise ValueError(f"check {check_id} is always-on and cannot be disabled")
                if config.severity != "blocking":
                    raise ValueError(f"check {check_id} is always-on and must be blocking")
            section = CHECK_SECTIONS[check_id]
            if config.enabled and section is not None and getattr(self, section) is None:
                raise ValueError(f"check {check_id} is enabled but policy section {section!r} is null")
        if not hash_check_skipped(info) and self.policy_hash != policy_hash_for(self):
            raise ValueError("policy_hash does not match the canonical hash of the policy content")
        return self

    @property
    def ref(self) -> PolicyRef:
        return PolicyRef(policy_id=self.policy_id, version=self.policy_version, hash=self.policy_hash)

    def check_config(self, check_id: str) -> CheckConfig:
        """The effective configuration of a check: an absent key means ``CheckConfig()`` (enabled, blocking)."""
        _check_id(check_id)
        return self.checks.get(check_id, CheckConfig())

    def is_check_enabled(self, check_id: str) -> bool:
        """Enabled by its CheckConfig *and* backed by its policy section (``None`` section => disabled)."""
        if check_id in ALWAYS_ON_CHECKS:
            return True
        section = CHECK_SECTIONS[check_id]
        if section is not None and getattr(self, section) is None:
            return False
        return self.check_config(check_id).enabled

    @property
    def enabled_checks(self) -> tuple[str, ...]:
        return tuple(check_id for check_id in CHECK_IDS if self.is_check_enabled(check_id))

    @classmethod
    def build(cls, **fields: Any) -> Policy:
        """Construct a policy, computing ``policy_hash`` from the normalised content."""
        return build_hashed(cls, "policy_hash", policy_hash_for, fields)


__all__ = [
    "ALWAYS_ON_CHECKS",
    "BASE_CHECK_IDS",
    "CHECK_IDS",
    "CHECK_INDEX",
    "CHECK_SECTIONS",
    "KELLY_FRACTION_CAP_MAX",
    "STRATEGIES",
    "AdvisoryConfig",
    "AgentBudget",
    "AggregatePolicy",
    "AuthorityCeiling",
    "AuthorizationConfig",
    "CheckConfig",
    "CheckIdStr",
    "CheckSeverity",
    "DrawdownScalingStep",
    "FactorPolicy",
    "FailClosed",
    "LiquidityPolicy",
    "OptionsLimits",
    "OrderLimits",
    "PathPolicy",
    "Policy",
    "PortfolioLimits",
    "ResponseLadder",
    "ResponseLevelSpec",
    "ResponseTrigger",
    "Restricted",
    "TailPolicy",
    "TimePolicy",
    "TradeLimits",
]
