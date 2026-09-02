"""Deterministic options risk evaluation.

A separate engine from the equity ``RiskEngine`` on purpose. The two measure
different things and share almost no arithmetic: equity risk is sized from
notional, options risk is sized from **maximum defined loss**. Merging them
would mean one function whose every branch had to be read twice.

Three properties hold throughout:

* **Nothing declared by the caller is authority.** Every figure is recomputed
  from strikes, sides, ratios, quantity and multiplier, and the declared value
  is then compared against a number it could not influence.
* **Nothing is fabricated.** No Greeks, no implied volatility, no probability
  of profit, no live quote. A value that is not supplied stays absent; it is
  never replaced by a flattering default.
* **Pure and deterministic.** No broker, no network, no database, no clock read
  from inside a calculation. The evaluation date is injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import MarketRiskSnapshot, PortfolioSnapshot, Side
from app.options.money import money_equal, to_money, to_ratio
from app.options.proposal import (
    LEG_COUNTS,
    MAX_LEGS,
    InvalidOptionEconomics,
    OptionEconomics,
    OptionStrategy,
    OptionTradeProposal,
    OptionType,
    ProfitBound,
    recompute_economics,
)

Severity = Literal["INFO", "WATCH", "HIGH", "BLOCK"]


class OptionRiskFlag(str, Enum):
    """Named reasons a structure is blocked or watched.

    Safety decisions are made from these, never from the numeric score.
    """

    # Blocking
    EXPIRED = "EXPIRED"
    DTE_BELOW_MINIMUM = "DTE_BELOW_MINIMUM"
    INVALID_ECONOMICS = "INVALID_ECONOMICS"
    ECONOMICS_MISMATCH = "ECONOMICS_MISMATCH"
    UNSUPPORTED_STRATEGY = "UNSUPPORTED_STRATEGY"
    STRUCTURE_INVALID = "STRUCTURE_INVALID"
    NAKED_SHORT = "NAKED_SHORT"
    LEG_LIMIT = "LEG_LIMIT"
    CONTRACT_LIMIT = "CONTRACT_LIMIT"
    MAX_LOSS_EQUITY_LIMIT = "MAX_LOSS_EQUITY_LIMIT"
    MAX_LOSS_BUYING_POWER_LIMIT = "MAX_LOSS_BUYING_POWER_LIMIT"
    PORTFOLIO_EQUITY_UNAVAILABLE = "PORTFOLIO_EQUITY_UNAVAILABLE"
    BUYING_POWER_UNAVAILABLE = "BUYING_POWER_UNAVAILABLE"
    LIQUIDITY_LOW = "LIQUIDITY_LOW"
    # Watch only
    SHORT_DTE = "SHORT_DTE"
    VOLATILITY_ELEVATED = "VOLATILITY_ELEVATED"
    DRAWDOWN_ELEVATED = "DRAWDOWN_ELEVATED"


# Deterministic additive weights, capped at 100. The score is a summary for
# display and ordering. It never decides anything: `blocked` is derived from
# whether any BLOCK-severity flag was raised, not from a threshold on this.
FLAG_WEIGHTS: dict[OptionRiskFlag, int] = {
    OptionRiskFlag.EXPIRED: 60,
    OptionRiskFlag.DTE_BELOW_MINIMUM: 40,
    OptionRiskFlag.INVALID_ECONOMICS: 60,
    OptionRiskFlag.ECONOMICS_MISMATCH: 50,
    OptionRiskFlag.UNSUPPORTED_STRATEGY: 60,
    OptionRiskFlag.STRUCTURE_INVALID: 60,
    OptionRiskFlag.NAKED_SHORT: 60,
    OptionRiskFlag.LEG_LIMIT: 30,
    OptionRiskFlag.CONTRACT_LIMIT: 25,
    OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT: 35,
    OptionRiskFlag.MAX_LOSS_BUYING_POWER_LIMIT: 35,
    OptionRiskFlag.PORTFOLIO_EQUITY_UNAVAILABLE: 50,
    OptionRiskFlag.BUYING_POWER_UNAVAILABLE: 30,
    OptionRiskFlag.LIQUIDITY_LOW: 30,
    OptionRiskFlag.SHORT_DTE: 10,
    OptionRiskFlag.VOLATILITY_ELEVATED: 10,
    OptionRiskFlag.DRAWDOWN_ELEVATED: 10,
}


@dataclass(frozen=True)
class OptionRiskPolicy:
    """V1 product-policy defaults.

    These are **policy, not market truth**. No published research or product
    requirement sets them; they are conservative starting values chosen so a
    single options position cannot quietly become a large share of the account.
    Every one is configurable, and they are all in this one place so a change is
    a change to policy rather than an edit scattered through the engine.
    """

    # A single structure may risk at most 5% of account equity...
    max_defined_loss_pct_equity: float = 0.05
    # ...and at most 10% of buying power.
    max_defined_loss_pct_buying_power: float = 0.10
    max_contracts: int = 20
    # Expiry must be at least one day out; same-day expiry is refused.
    min_days_to_expiry: int = 1
    # Inside a week, gamma and assignment risk rise sharply. Watch, not block.
    short_dte_watch_days: int = 7
    max_legs: int = MAX_LEGS
    allowed_strategies: frozenset[OptionStrategy] = frozenset(OptionStrategy)
    # Caller-supplied market context. Each applies only when the value is given.
    min_liquidity_score: float = 0.35
    volatility_watch_threshold: float = 0.80
    drawdown_watch_threshold: float = 0.25
    # Unverifiable portfolio data fails closed, matching the equity engine's
    # treatment of an unavailable daily P&L.
    block_when_buying_power_unavailable: bool = True


class OptionMarketContext(BaseModel):
    """Optional market context for the underlying, supplied by the caller.

    Every field is optional and stays absent when it was not supplied. An
    absent volatility is not zero volatility, and an absent liquidity score is
    not perfect liquidity.

    ``source`` is fixed: this backend has no options market-data feed, so these
    numbers are assertions from upstream, never observations.
    """

    model_config = ConfigDict(extra="forbid")

    underlying: str = Field(min_length=1, max_length=16)
    source: Literal["CALLER_SUPPLIED"] = "CALLER_SUPPLIED"
    annualized_volatility: float | None = Field(default=None, ge=0)
    max_drawdown_30d: float | None = Field(default=None, ge=0, le=1)
    liquidity_score: float | None = Field(default=None, ge=0, le=1)

    @classmethod
    def from_market_risk(cls, snapshot: MarketRiskSnapshot) -> "OptionMarketContext":
        """Adapt the existing caller-supplied equity market-risk snapshot."""

        return cls(
            underlying=snapshot.symbol,
            annualized_volatility=snapshot.annualized_volatility,
            max_drawdown_30d=snapshot.max_drawdown_30d,
            liquidity_score=snapshot.liquidity_score,
        )


class OptionRiskCheck(BaseModel):
    """One evaluated rule. Mirrors ``RiskRuleResult`` so it maps cleanly later."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    flag: OptionRiskFlag | None = None
    passed: bool
    severity: Severity
    message: str


class OptionRiskReport(BaseModel):
    """Deterministic options risk result.

    Carries what a later Governor integration needs without inheriting fields
    that only mean something for equities.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    instrument_type: Literal["option"] = "option"
    underlying: str
    strategy: OptionStrategy
    expiry: date
    quantity: int
    recommended_quantity: int
    leg_count: int
    multiplier: int

    premium_source: Literal["CALLER_SUPPLIED"] = "CALLER_SUPPLIED"
    net_premium_per_unit: Decimal | None = None
    net_debit_per_unit: Decimal | None = None
    net_credit_per_unit: Decimal | None = None
    risk_width: Decimal | None = None

    recomputed_max_loss: Decimal | None = None
    recomputed_max_profit: Decimal | None = None
    max_profit_bound: ProfitBound | None = None
    declared_max_loss: float | None = None
    declared_max_profit: float | None = None
    economics_match: bool

    risk_amount: Decimal | None = None
    risk_pct_equity: Decimal | None = None
    risk_pct_buying_power: Decimal | None = None
    underlying_concentration_available: bool = False

    days_to_expiry: int | None = None
    as_of: date

    market_context_source: Literal["CALLER_SUPPLIED"] | None = None
    liquidity_score: float | None = None
    annualized_volatility: float | None = None
    max_drawdown_30d: float | None = None

    blocked: bool
    risk_score: int = Field(ge=0, le=100)
    risk_flags: list[OptionRiskFlag] = Field(default_factory=list)
    checks: list[OptionRiskCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


@dataclass
class _Accumulator:
    """Collects checks as the engine walks its rules."""

    checks: list[OptionRiskCheck] = field(default_factory=list)
    flags: list[OptionRiskFlag] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False

    def record(
        self,
        rule: str,
        *,
        passed: bool,
        severity: Severity,
        message: str,
        flag: OptionRiskFlag | None = None,
    ) -> None:
        self.checks.append(
            OptionRiskCheck(
                rule=rule, flag=flag, passed=passed, severity=severity, message=message
            )
        )
        if passed or flag is None:
            return
        if flag not in self.flags:
            self.flags.append(flag)
        self.reasons.append(message)
        if severity == "BLOCK":
            self.blocked = True

    def score(self) -> int:
        return min(sum(FLAG_WEIGHTS.get(flag, 0) for flag in self.flags), 100)


class OptionRiskEngine:
    """Deterministic defined-risk evaluation for the five V1 strategies."""

    def __init__(
        self,
        policy: OptionRiskPolicy | None = None,
        *,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self.policy = policy or OptionRiskPolicy()
        self.today_provider = today_provider or date.today

    def evaluate(
        self,
        proposal: OptionTradeProposal,
        portfolio: PortfolioSnapshot,
        market: OptionMarketContext | MarketRiskSnapshot | None = None,
        *,
        as_of: date | None = None,
    ) -> OptionRiskReport:
        if not isinstance(proposal, OptionTradeProposal):
            raise TypeError("OptionRiskEngine requires an OptionTradeProposal.")

        evaluated_on = as_of or self.today_provider()
        context = _coerce_context(market)
        accumulator = _Accumulator()

        structure_ok = self._check_structure(proposal, accumulator)
        economics = self._check_economics(proposal, accumulator) if structure_ok else None
        self._check_expiry(proposal, evaluated_on, accumulator)
        equity, buying_power = self._check_portfolio(portfolio, accumulator)
        self._check_market_context(context, accumulator)

        risk_pct_equity: Decimal | None = None
        risk_pct_buying_power: Decimal | None = None
        recommended = 0

        if economics is not None:
            risk_pct_equity, risk_pct_buying_power = self._check_capital_at_risk(
                economics, equity, buying_power, accumulator
            )
            recommended = self._recommended_quantity(economics, equity, buying_power)
            if recommended <= 0 and not accumulator.blocked:
                accumulator.record(
                    "risk_adjusted_size",
                    passed=False,
                    severity="BLOCK",
                    message=(
                        "Risk-adjusted contract count is zero; no size of this structure "
                        "fits current options risk policy."
                    ),
                    flag=OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT,
                )

        accumulator.record(
            "underlying_concentration",
            passed=True,
            severity="INFO",
            message=(
                "Options exposure per underlying cannot be derived from the current "
                "portfolio snapshot; capital at risk is reported instead."
            ),
            flag=None,
        )

        if not accumulator.reasons:
            accumulator.reasons.append(
                "Defined-risk structure is within current options risk policy."
            )
        if accumulator.blocked:
            recommended = 0

        return OptionRiskReport(
            proposal_id=proposal.proposal_id,
            underlying=proposal.underlying,
            strategy=proposal.strategy,
            expiry=proposal.expiry,
            quantity=proposal.quantity,
            recommended_quantity=recommended,
            leg_count=len(proposal.legs),
            multiplier=proposal.contract_multiplier,
            net_premium_per_unit=(
                None if economics is None else to_money(economics.net_premium_per_share)
            ),
            net_debit_per_unit=(
                None
                if economics is None or economics.net_debit_per_share is None
                else to_money(economics.net_debit_per_share)
            ),
            net_credit_per_unit=(
                None
                if economics is None or economics.net_credit_per_share is None
                else to_money(economics.net_credit_per_share)
            ),
            risk_width=(
                None
                if economics is None or economics.risk_width is None
                else to_money(economics.risk_width)
            ),
            recomputed_max_loss=None if economics is None else economics.max_loss,
            recomputed_max_profit=None if economics is None else economics.max_profit,
            max_profit_bound=None if economics is None else economics.profit_bound,
            declared_max_loss=proposal.estimated_max_loss,
            declared_max_profit=proposal.estimated_max_profit,
            economics_match=OptionRiskFlag.ECONOMICS_MISMATCH not in accumulator.flags,
            risk_amount=None if economics is None else economics.max_loss,
            risk_pct_equity=risk_pct_equity,
            risk_pct_buying_power=risk_pct_buying_power,
            underlying_concentration_available=False,
            days_to_expiry=(proposal.expiry - evaluated_on).days,
            as_of=evaluated_on,
            market_context_source=None if context is None else context.source,
            liquidity_score=None if context is None else context.liquidity_score,
            annualized_volatility=None if context is None else context.annualized_volatility,
            max_drawdown_30d=None if context is None else context.max_drawdown_30d,
            blocked=accumulator.blocked,
            risk_score=accumulator.score(),
            risk_flags=list(accumulator.flags),
            checks=accumulator.checks,
            reasons=accumulator.reasons,
        )

    # -- rules -----------------------------------------------------------

    def _check_structure(
        self, proposal: OptionTradeProposal, accumulator: _Accumulator
    ) -> bool:
        """Re-verify what the model already enforces.

        The model makes a malformed structure unconstructable, but this engine
        must not assume its input arrived through validation. An object built by
        ``model_construct``, restored from a corrupted row, or produced by a
        future caller still has to fail closed here.
        """

        ok = True

        if proposal.strategy not in self.policy.allowed_strategies:
            accumulator.record(
                "strategy_allowlist",
                passed=False,
                severity="BLOCK",
                message=f"Strategy {proposal.strategy} is not permitted by current policy.",
                flag=OptionRiskFlag.UNSUPPORTED_STRATEGY,
            )
            ok = False
        else:
            accumulator.record(
                "strategy_allowlist",
                passed=True,
                severity="INFO",
                message=f"{proposal.strategy.value} is a permitted V1 strategy.",
            )

        leg_count = len(proposal.legs)
        expected = LEG_COUNTS.get(proposal.strategy)
        if leg_count > self.policy.max_legs:
            accumulator.record(
                "leg_limit",
                passed=False,
                severity="BLOCK",
                message=f"{leg_count} legs exceeds the {self.policy.max_legs}-leg limit.",
                flag=OptionRiskFlag.LEG_LIMIT,
            )
            ok = False
        elif expected is not None and leg_count != expected:
            accumulator.record(
                "leg_structure",
                passed=False,
                severity="BLOCK",
                message=(
                    f"{proposal.strategy.value} requires {expected} leg(s); "
                    f"{leg_count} were supplied."
                ),
                flag=OptionRiskFlag.STRUCTURE_INVALID,
            )
            ok = False
        else:
            accumulator.record(
                "leg_structure",
                passed=True,
                severity="INFO",
                message=f"{leg_count} leg(s), within the {self.policy.max_legs}-leg limit.",
            )

        if self._has_naked_short(proposal):
            accumulator.record(
                "naked_short",
                passed=False,
                severity="BLOCK",
                message="A short leg is not covered by a long leg of the same type.",
                flag=OptionRiskFlag.NAKED_SHORT,
            )
            ok = False
        else:
            accumulator.record(
                "naked_short",
                passed=True,
                severity="INFO",
                message="Every short leg is covered by a long leg of the same type.",
            )

        inconsistent = [
            leg.option_symbol for leg in proposal.legs if leg.expiry != proposal.expiry
        ]
        wrong_ratio = [leg.option_symbol for leg in proposal.legs if leg.ratio != 1]
        if inconsistent or wrong_ratio:
            detail = []
            if inconsistent:
                detail.append(f"mixed expiries on {', '.join(inconsistent)}")
            if wrong_ratio:
                detail.append(f"non-unit ratios on {', '.join(wrong_ratio)}")
            accumulator.record(
                "leg_consistency",
                passed=False,
                severity="BLOCK",
                message="Structure is inconsistent: " + "; ".join(detail) + ".",
                flag=OptionRiskFlag.STRUCTURE_INVALID,
            )
            ok = False
        else:
            accumulator.record(
                "leg_consistency",
                passed=True,
                severity="INFO",
                message="All legs share the proposal expiry at a 1:1 ratio.",
            )

        foreign = sorted({leg.root for leg in proposal.legs} - {proposal.underlying})
        if foreign:
            accumulator.record(
                "underlying_consistency",
                passed=False,
                severity="BLOCK",
                message=(
                    f"Legs reference {', '.join(foreign)} rather than the declared "
                    f"underlying {proposal.underlying}."
                ),
                flag=OptionRiskFlag.STRUCTURE_INVALID,
            )
            ok = False
        else:
            accumulator.record(
                "underlying_consistency",
                passed=True,
                severity="INFO",
                message=f"Every leg is written on {proposal.underlying}.",
            )

        return ok

    @staticmethod
    def _has_naked_short(proposal: OptionTradeProposal) -> bool:
        for option_type in (OptionType.CALL, OptionType.PUT):
            legs = [leg for leg in proposal.legs if leg.option_type == option_type]
            shorts = [leg for leg in legs if leg.side == Side.SELL]
            longs = [leg for leg in legs if leg.side == Side.BUY]
            if len(shorts) > len(longs):
                return True
        return False

    def _check_economics(
        self, proposal: OptionTradeProposal, accumulator: _Accumulator
    ) -> OptionEconomics | None:
        try:
            economics = recompute_economics(proposal)
        except (InvalidOptionEconomics, ValueError) as exc:
            accumulator.record(
                "economics",
                passed=False,
                severity="BLOCK",
                message=f"Economics could not be recomputed: {exc}",
                flag=OptionRiskFlag.INVALID_ECONOMICS,
            )
            return None

        accumulator.record(
            "economics",
            passed=True,
            severity="INFO",
            message=(
                f"Recomputed max loss {economics.max_loss} and max profit "
                f"{economics.max_profit if economics.max_profit is not None else 'UNBOUNDED'} "
                f"for {economics.quantity} contract(s) at multiplier {economics.multiplier}."
            ),
        )
        self._check_declared_values(proposal, economics, accumulator)
        return economics

    def _check_declared_values(
        self,
        proposal: OptionTradeProposal,
        economics: OptionEconomics,
        accumulator: _Accumulator,
    ) -> None:
        mismatches: list[str] = []

        if proposal.estimated_max_loss is not None and not money_equal(
            proposal.estimated_max_loss, economics.max_loss
        ):
            mismatches.append(
                f"declared max loss {proposal.estimated_max_loss:.2f} against recomputed "
                f"{economics.max_loss}"
            )

        if economics.profit_bound is ProfitBound.UNBOUNDED:
            if proposal.estimated_max_profit is not None:
                mismatches.append(
                    f"{proposal.strategy.value} has unbounded maximum profit, but "
                    f"{proposal.estimated_max_profit:.2f} was declared; a number is not "
                    "equivalent to unbounded"
                )
        elif proposal.estimated_max_profit is not None and not money_equal(
            proposal.estimated_max_profit, economics.max_profit
        ):
            mismatches.append(
                f"declared max profit {proposal.estimated_max_profit:.2f} against recomputed "
                f"{economics.max_profit}"
            )

        if mismatches:
            accumulator.record(
                "declared_economics",
                passed=False,
                severity="BLOCK",
                message="Declared economics disagree with the backend: "
                + "; ".join(mismatches)
                + ".",
                flag=OptionRiskFlag.ECONOMICS_MISMATCH,
            )
            return

        declared = [
            name
            for name, value in (
                ("max loss", proposal.estimated_max_loss),
                ("max profit", proposal.estimated_max_profit),
            )
            if value is not None
        ]
        accumulator.record(
            "declared_economics",
            passed=True,
            severity="INFO",
            message=(
                f"Declared {' and '.join(declared)} agree with the recomputed economics."
                if declared
                else "No declared economics were supplied; backend figures stand alone."
            ),
        )

    def _check_expiry(
        self, proposal: OptionTradeProposal, as_of: date, accumulator: _Accumulator
    ) -> None:
        days = (proposal.expiry - as_of).days

        if days < 0:
            accumulator.record(
                "expiry",
                passed=False,
                severity="BLOCK",
                message=f"Contracts expired {abs(days)} day(s) ago on {proposal.expiry}.",
                flag=OptionRiskFlag.EXPIRED,
            )
            return

        if days < self.policy.min_days_to_expiry:
            accumulator.record(
                "expiry",
                passed=False,
                severity="BLOCK",
                message=(
                    f"{days} day(s) to expiry is below the {self.policy.min_days_to_expiry}-day "
                    "minimum."
                ),
                flag=OptionRiskFlag.DTE_BELOW_MINIMUM,
            )
            return

        if days <= self.policy.short_dte_watch_days:
            accumulator.record(
                "expiry",
                passed=False,
                severity="WATCH",
                message=(
                    f"{days} day(s) to expiry is inside the "
                    f"{self.policy.short_dte_watch_days}-day short-dated window."
                ),
                flag=OptionRiskFlag.SHORT_DTE,
            )
            return

        accumulator.record(
            "expiry",
            passed=True,
            severity="INFO",
            message=f"{days} day(s) to expiry on {proposal.expiry}.",
        )

    def _check_portfolio(
        self, portfolio: PortfolioSnapshot, accumulator: _Accumulator
    ) -> tuple[Decimal | None, Decimal | None]:
        equity_raw = getattr(portfolio, "equity", None)
        equity = None
        if isinstance(equity_raw, (int, float, Decimal)) and Decimal(str(equity_raw)) > 0:
            equity = Decimal(str(equity_raw))
        if equity is None:
            accumulator.record(
                "portfolio_equity",
                passed=False,
                severity="BLOCK",
                message="Account equity is unavailable or non-positive; risk cannot be sized.",
                flag=OptionRiskFlag.PORTFOLIO_EQUITY_UNAVAILABLE,
            )
        else:
            accumulator.record(
                "portfolio_equity",
                passed=True,
                severity="INFO",
                message=f"Account equity {equity} is available for sizing.",
            )

        buying_power_raw = getattr(portfolio, "buying_power", None)
        buying_power = None
        if isinstance(buying_power_raw, (int, float, Decimal)) and Decimal(
            str(buying_power_raw)
        ) > 0:
            buying_power = Decimal(str(buying_power_raw))
        if buying_power is None:
            accumulator.record(
                "buying_power",
                passed=False,
                severity="BLOCK" if self.policy.block_when_buying_power_unavailable else "WATCH",
                message=(
                    "Buying power is unavailable or zero; the buying-power limit cannot be "
                    "verified. Unverifiable portfolio data fails closed."
                ),
                flag=OptionRiskFlag.BUYING_POWER_UNAVAILABLE,
            )
        else:
            accumulator.record(
                "buying_power",
                passed=True,
                severity="INFO",
                message=f"Buying power {buying_power} is available for sizing.",
            )

        return equity, buying_power

    def _check_capital_at_risk(
        self,
        economics: OptionEconomics,
        equity: Decimal | None,
        buying_power: Decimal | None,
        accumulator: _Accumulator,
    ) -> tuple[Decimal | None, Decimal | None]:
        risk = economics.max_loss
        pct_equity: Decimal | None = None
        pct_buying_power: Decimal | None = None

        if equity is not None:
            pct_equity = to_ratio(risk / equity)
            limit = Decimal(str(self.policy.max_defined_loss_pct_equity))
            if pct_equity > limit:
                accumulator.record(
                    "max_loss_pct_equity",
                    passed=False,
                    severity="HIGH",
                    message=(
                        f"Defined loss {risk} is {pct_equity:.4%} of equity; the limit is "
                        f"{limit:.2%}."
                    ),
                    flag=OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT,
                )
            else:
                accumulator.record(
                    "max_loss_pct_equity",
                    passed=True,
                    severity="INFO",
                    message=(
                        f"Defined loss {risk} is {pct_equity:.4%} of equity; limit {limit:.2%}."
                    ),
                )

        if buying_power is not None:
            pct_buying_power = to_ratio(risk / buying_power)
            limit = Decimal(str(self.policy.max_defined_loss_pct_buying_power))
            if pct_buying_power > limit:
                accumulator.record(
                    "max_loss_pct_buying_power",
                    passed=False,
                    severity="HIGH",
                    message=(
                        f"Defined loss {risk} is {pct_buying_power:.4%} of buying power; the "
                        f"limit is {limit:.2%}."
                    ),
                    flag=OptionRiskFlag.MAX_LOSS_BUYING_POWER_LIMIT,
                )
            else:
                accumulator.record(
                    "max_loss_pct_buying_power",
                    passed=True,
                    severity="INFO",
                    message=(
                        f"Defined loss {risk} is {pct_buying_power:.4%} of buying power; "
                        f"limit {limit:.2%}."
                    ),
                )

        if economics.quantity > self.policy.max_contracts:
            accumulator.record(
                "contract_limit",
                passed=False,
                severity="HIGH",
                message=(
                    f"{economics.quantity} contracts exceeds the "
                    f"{self.policy.max_contracts}-contract limit."
                ),
                flag=OptionRiskFlag.CONTRACT_LIMIT,
            )
        else:
            accumulator.record(
                "contract_limit",
                passed=True,
                severity="INFO",
                message=(
                    f"{economics.quantity} contract(s), within the "
                    f"{self.policy.max_contracts}-contract limit."
                ),
            )

        return pct_equity, pct_buying_power

    def _recommended_quantity(
        self,
        economics: OptionEconomics,
        equity: Decimal | None,
        buying_power: Decimal | None,
    ) -> int:
        """The largest contract count that satisfies every quantity-scaled limit.

        Only the number of contracts is ever reduced. Strikes, expiry, sides and
        the strategy itself are never touched.
        """

        per_unit = economics.max_loss_per_unit
        if per_unit <= 0:
            return 0

        allowed = min(economics.quantity, self.policy.max_contracts)
        if equity is not None:
            cap = equity * Decimal(str(self.policy.max_defined_loss_pct_equity))
            allowed = min(allowed, int(cap // per_unit))
        if buying_power is not None:
            cap = buying_power * Decimal(str(self.policy.max_defined_loss_pct_buying_power))
            allowed = min(allowed, int(cap // per_unit))
        return max(allowed, 0)

    def _check_market_context(
        self, context: OptionMarketContext | None, accumulator: _Accumulator
    ) -> None:
        if context is None:
            accumulator.record(
                "market_context",
                passed=True,
                severity="INFO",
                message="No market context was supplied; no context rule was applied.",
            )
            return

        if context.liquidity_score is None:
            accumulator.record(
                "liquidity",
                passed=True,
                severity="INFO",
                message="No liquidity score was supplied; the liquidity rule was not applied.",
            )
        elif context.liquidity_score < self.policy.min_liquidity_score:
            accumulator.record(
                "liquidity",
                passed=False,
                severity="BLOCK",
                message=(
                    f"Caller-supplied liquidity score {context.liquidity_score:.2f} is below "
                    f"the {self.policy.min_liquidity_score:.2f} minimum."
                ),
                flag=OptionRiskFlag.LIQUIDITY_LOW,
            )
        else:
            accumulator.record(
                "liquidity",
                passed=True,
                severity="INFO",
                message=(
                    f"Caller-supplied liquidity score {context.liquidity_score:.2f} meets the "
                    f"{self.policy.min_liquidity_score:.2f} minimum."
                ),
            )

        if context.annualized_volatility is None:
            accumulator.record(
                "volatility",
                passed=True,
                severity="INFO",
                message="No volatility was supplied; the volatility rule was not applied.",
            )
        elif context.annualized_volatility > self.policy.volatility_watch_threshold:
            accumulator.record(
                "volatility",
                passed=False,
                severity="WATCH",
                message=(
                    f"Caller-supplied annualized volatility "
                    f"{context.annualized_volatility:.2%} exceeds the "
                    f"{self.policy.volatility_watch_threshold:.2%} watch threshold."
                ),
                flag=OptionRiskFlag.VOLATILITY_ELEVATED,
            )
        else:
            accumulator.record(
                "volatility",
                passed=True,
                severity="INFO",
                message=(
                    f"Caller-supplied annualized volatility "
                    f"{context.annualized_volatility:.2%} is within the watch threshold."
                ),
            )

        if context.max_drawdown_30d is None:
            accumulator.record(
                "drawdown",
                passed=True,
                severity="INFO",
                message="No drawdown was supplied; the drawdown rule was not applied.",
            )
        elif context.max_drawdown_30d > self.policy.drawdown_watch_threshold:
            accumulator.record(
                "drawdown",
                passed=False,
                severity="WATCH",
                message=(
                    f"Caller-supplied 30-day drawdown {context.max_drawdown_30d:.2%} exceeds "
                    f"the {self.policy.drawdown_watch_threshold:.2%} watch threshold."
                ),
                flag=OptionRiskFlag.DRAWDOWN_ELEVATED,
            )
        else:
            accumulator.record(
                "drawdown",
                passed=True,
                severity="INFO",
                message=(
                    f"Caller-supplied 30-day drawdown {context.max_drawdown_30d:.2%} is within "
                    "the watch threshold."
                ),
            )


def _coerce_context(
    market: OptionMarketContext | MarketRiskSnapshot | None,
) -> OptionMarketContext | None:
    if market is None or isinstance(market, OptionMarketContext):
        return market
    if isinstance(market, MarketRiskSnapshot):
        return OptionMarketContext.from_market_risk(market)
    raise TypeError("market must be an OptionMarketContext, MarketRiskSnapshot, or None.")
