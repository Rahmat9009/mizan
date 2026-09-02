"""Phase 3 tests: deterministic options risk evaluation.

Pure. No network, no broker, no database, no wall clock — the evaluation date
is injected into every call.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import MarketRiskSnapshot, PortfolioSnapshot
from app.options.money import money_equal, to_decimal, to_money, to_ratio
from app.options.proposal import (
    InvalidOptionEconomics,
    OptionLeg,
    OptionStrategy,
    OptionTradeProposal,
    OptionType,
    ProfitBound,
    recompute_economics,
)
from app.options.risk import (
    OptionMarketContext,
    OptionRiskEngine,
    OptionRiskFlag,
    OptionRiskPolicy,
)

EXPIRY = date(2026, 10, 16)
TODAY = date(2026, 9, 2)  # 44 days to expiry


def leg(symbol: str, side: str, option_type: OptionType, strike: float, expiry: date = EXPIRY):
    return OptionLeg(
        option_symbol=symbol, side=side, option_type=option_type, strike=strike, expiry=expiry
    )


def build(strategy, legs, premium, **overrides) -> OptionTradeProposal:
    payload = {
        "underlying": "AAPL",
        "strategy": strategy,
        "quantity": 1,
        "expiry": EXPIRY,
        "legs": legs,
        "estimated_net_premium_per_unit": premium,
        "strategy_confidence": 0.7,
        "thesis": "Risk fixture.",
        "invalidation_condition": "Fixture invalidation.",
    }
    payload.update(overrides)
    return OptionTradeProposal(**payload)


def long_call(**kw):
    return build(OptionStrategy.LONG_CALL,
                 [leg("AAPL261016C00230000", "BUY", OptionType.CALL, 230.0)], -4.20, **kw)


def long_put(**kw):
    return build(OptionStrategy.LONG_PUT,
                 [leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0)], -3.10, **kw)


def call_debit_spread(**kw):
    return build(OptionStrategy.VERTICAL_DEBIT_SPREAD,
                 [leg("AAPL261016C00230000", "BUY", OptionType.CALL, 230.0),
                  leg("AAPL261016C00240000", "SELL", OptionType.CALL, 240.0)], -4.00, **kw)


def put_debit_spread(**kw):
    return build(OptionStrategy.VERTICAL_DEBIT_SPREAD,
                 [leg("AAPL261016P00200000", "BUY", OptionType.PUT, 200.0),
                  leg("AAPL261016P00190000", "SELL", OptionType.PUT, 190.0)], -4.00, **kw)


def call_credit_spread(**kw):
    return build(OptionStrategy.VERTICAL_CREDIT_SPREAD,
                 [leg("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0),
                  leg("AAPL261016C00240000", "BUY", OptionType.CALL, 240.0)], 3.00, **kw)


def put_credit_spread(**kw):
    return build(OptionStrategy.VERTICAL_CREDIT_SPREAD,
                 [leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0),
                  leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0)], 3.00, **kw)


def iron_condor(**kw):
    return build(OptionStrategy.IRON_CONDOR,
                 [leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0),
                  leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0),
                  leg("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0),
                  leg("AAPL261016C00240000", "BUY", OptionType.CALL, 240.0)], 1.35, **kw)


def wide_put_condor(**kw):
    """Put wing 20 wide, call wing 10 wide. The put wing must size the risk."""

    return build(OptionStrategy.IRON_CONDOR,
                 [leg("AAPL261016P00180000", "BUY", OptionType.PUT, 180.0),
                  leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0),
                  leg("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0),
                  leg("AAPL261016C00240000", "BUY", OptionType.CALL, 240.0)], 1.35, **kw)


def portfolio(equity: float = 100_000.0, buying_power: float = 200_000.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=equity, cash=equity / 2, buying_power=buying_power, daily_pnl_pct=0.0
    )


def evaluate(proposal, *, pf=None, market=None, policy=None, as_of=TODAY):
    engine = OptionRiskEngine(policy)
    return engine.evaluate(proposal, pf or portfolio(), market, as_of=as_of)


def flags(report) -> set[str]:
    return {flag.value for flag in report.risk_flags}


# --------------------------------------------------------------------------
# Money arithmetic and tolerance policy
# --------------------------------------------------------------------------
def test_decimal_conversion_avoids_binary_float_drift() -> None:
    assert to_decimal(1.35) == Decimal("1.35")
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


def test_money_rounds_to_the_cent_half_away_from_zero() -> None:
    assert to_money(1.005) == Decimal("1.01")
    assert to_money(Decimal("2.344")) == Decimal("2.34")


def test_ratio_keeps_six_places() -> None:
    assert to_ratio(Decimal("1") / Decimal("3")) == Decimal("0.333333")


def test_absolute_tolerance_accepts_a_cent() -> None:
    assert money_equal(Decimal("1730.00"), Decimal("1730.01"))
    assert not money_equal(Decimal("100.00"), Decimal("100.50"))


def test_relative_tolerance_accepts_larger_sums() -> None:
    # 20.00 on 100000 is 0.02%, inside the 0.1% relative band.
    assert money_equal(Decimal("100000.00"), Decimal("100020.00"))
    # 500.00 on 100000 is 0.5%, outside it.
    assert not money_equal(Decimal("100000.00"), Decimal("100500.00"))


def test_money_equal_rejects_when_both_sides_are_zero_scaled() -> None:
    assert money_equal(Decimal("0"), Decimal("0"))


# --------------------------------------------------------------------------
# LONG_CALL
# --------------------------------------------------------------------------
def test_long_call_max_loss_is_the_debit() -> None:
    economics = recompute_economics(long_call())
    assert economics.max_loss == Decimal("420.00")
    assert economics.net_debit_per_share == Decimal("4.20")
    assert economics.net_credit_per_share is None


def test_long_call_profit_is_unbounded_with_no_number() -> None:
    economics = recompute_economics(long_call())
    assert economics.profit_bound is ProfitBound.UNBOUNDED
    assert economics.max_profit is None
    assert economics.max_profit_per_unit is None


def test_long_call_report_represents_unbounded_explicitly() -> None:
    report = evaluate(long_call())
    assert report.max_profit_bound is ProfitBound.UNBOUNDED
    assert report.recomputed_max_profit is None
    assert report.recomputed_max_loss == Decimal("420.00")


def test_long_call_quantity_scales_the_loss() -> None:
    assert recompute_economics(long_call(quantity=3)).max_loss == Decimal("1260.00")


def test_long_call_multiplier_scales_the_loss() -> None:
    assert recompute_economics(long_call(contract_multiplier=10)).max_loss == Decimal("42.00")


# --------------------------------------------------------------------------
# LONG_PUT
# --------------------------------------------------------------------------
def test_long_put_max_loss_is_the_debit() -> None:
    assert recompute_economics(long_put()).max_loss == Decimal("310.00")


def test_long_put_max_profit_uses_the_actual_long_strike() -> None:
    economics = recompute_economics(long_put())
    assert economics.profit_bound is ProfitBound.BOUNDED
    assert economics.max_profit == Decimal("18690.00")  # (190 - 3.10) * 100


def test_long_put_quantity_and_multiplier_scale() -> None:
    assert recompute_economics(long_put(quantity=4)).max_loss == Decimal("1240.00")
    assert recompute_economics(long_put(quantity=4)).max_profit == Decimal("74760.00")
    assert recompute_economics(long_put(contract_multiplier=10)).max_loss == Decimal("31.00")


# --------------------------------------------------------------------------
# VERTICAL_DEBIT_SPREAD
# --------------------------------------------------------------------------
def test_call_debit_spread_economics() -> None:
    economics = recompute_economics(call_debit_spread())
    assert economics.risk_width == Decimal("10")
    assert economics.max_loss == Decimal("400.00")
    assert economics.max_profit == Decimal("600.00")


def test_put_debit_spread_economics() -> None:
    economics = recompute_economics(put_debit_spread())
    assert economics.risk_width == Decimal("10")
    assert economics.max_loss == Decimal("400.00")
    assert economics.max_profit == Decimal("600.00")


def test_debit_spread_quantity_scales_both_sides() -> None:
    economics = recompute_economics(call_debit_spread(quantity=5))
    assert economics.max_loss == Decimal("2000.00")
    assert economics.max_profit == Decimal("3000.00")


def test_debit_spread_risk_is_not_stock_notional() -> None:
    """230 * 100 would be 23,000. The defined loss is 400."""

    assert recompute_economics(call_debit_spread()).max_loss == Decimal("400.00")


# --------------------------------------------------------------------------
# VERTICAL_CREDIT_SPREAD
# --------------------------------------------------------------------------
def test_call_credit_spread_economics() -> None:
    economics = recompute_economics(call_credit_spread())
    assert economics.risk_width == Decimal("10")
    assert economics.max_profit == Decimal("300.00")
    assert economics.max_loss == Decimal("700.00")


def test_put_credit_spread_economics() -> None:
    economics = recompute_economics(put_credit_spread())
    assert economics.max_profit == Decimal("300.00")
    assert economics.max_loss == Decimal("700.00")


def test_credit_spread_quantity_scales() -> None:
    economics = recompute_economics(put_credit_spread(quantity=3))
    assert economics.max_loss == Decimal("2100.00")
    assert economics.max_profit == Decimal("900.00")


# --------------------------------------------------------------------------
# IRON_CONDOR
# --------------------------------------------------------------------------
def test_symmetric_condor_economics() -> None:
    economics = recompute_economics(iron_condor())
    assert economics.risk_width == Decimal("10")
    assert economics.max_loss == Decimal("865.00")
    assert economics.max_profit == Decimal("135.00")


def test_asymmetric_condor_uses_the_wider_wing() -> None:
    economics = recompute_economics(wide_put_condor())
    assert economics.risk_width == Decimal("20")
    assert economics.max_loss == Decimal("1865.00")
    assert economics.max_profit == Decimal("135.00")


def test_asymmetric_condor_is_not_sized_by_the_narrow_wing() -> None:
    narrow = recompute_economics(iron_condor()).max_loss
    assert recompute_economics(wide_put_condor()).max_loss > narrow


def test_condor_quantity_scales() -> None:
    economics = recompute_economics(iron_condor(quantity=2))
    assert economics.max_loss == Decimal("1730.00")
    assert economics.max_profit == Decimal("270.00")


# --------------------------------------------------------------------------
# Declared economics
# --------------------------------------------------------------------------
def test_exactly_matching_declared_values_pass() -> None:
    report = evaluate(iron_condor(quantity=2, estimated_max_loss=1730.0,
                                  estimated_max_profit=270.0))
    assert report.economics_match is True
    assert report.blocked is False
    assert report.declared_max_loss == 1730.0


def test_declared_value_within_a_cent_passes() -> None:
    report = evaluate(iron_condor(quantity=2, estimated_max_loss=1730.01))
    assert report.economics_match is True


def test_declared_value_within_the_relative_band_passes() -> None:
    # 20 contracts: max loss 17,300. A 10.00 difference is 0.058%.
    report = evaluate(
        iron_condor(quantity=20, estimated_max_loss=17310.0),
        pf=portfolio(equity=1_000_000.0, buying_power=2_000_000.0),
    )
    assert report.economics_match is True


def test_declared_max_loss_too_low_is_blocked() -> None:
    report = evaluate(_bypass(iron_condor(quantity=2), estimated_max_loss=500.0))
    assert report.economics_match is False
    assert report.blocked is True
    assert OptionRiskFlag.ECONOMICS_MISMATCH.value in flags(report)


def test_declared_max_loss_too_high_is_blocked() -> None:
    report = evaluate(_bypass(iron_condor(quantity=2), estimated_max_loss=9000.0))
    assert report.economics_match is False
    assert report.blocked is True


def test_declared_bounded_max_profit_mismatch_is_blocked() -> None:
    report = evaluate(_bypass(iron_condor(quantity=2), estimated_max_profit=5000.0))
    assert report.economics_match is False
    assert report.blocked is True


def test_a_number_is_never_equivalent_to_unbounded() -> None:
    report = evaluate(_bypass(long_call(), estimated_max_profit=99999.0))
    assert report.economics_match is False
    assert report.blocked is True
    assert any("not\nequivalent" in r or "not equivalent" in r for r in report.reasons)


def test_absent_declared_values_leave_economics_matching() -> None:
    report = evaluate(iron_condor())
    assert report.economics_match is True
    assert report.declared_max_loss is None
    assert report.declared_max_profit is None


# --------------------------------------------------------------------------
# Premium validity
# --------------------------------------------------------------------------
def test_zero_premium_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="non-zero"):
        recompute_economics(_bypass(long_call(), estimated_net_premium_per_unit=0.0))


def test_credit_on_a_debit_strategy_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="positive debit"):
        recompute_economics(_bypass(long_call(), estimated_net_premium_per_unit=4.20))


def test_debit_on_a_credit_strategy_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="positive credit"):
        recompute_economics(_bypass(iron_condor(), estimated_net_premium_per_unit=-1.35))


def test_debit_at_or_above_the_width_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="cannot equal or exceed"):
        recompute_economics(
            _bypass(call_debit_spread(), estimated_net_premium_per_unit=-10.0)
        )


def test_credit_at_or_above_the_width_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="cannot equal or exceed"):
        recompute_economics(
            _bypass(put_credit_spread(), estimated_net_premium_per_unit=10.0)
        )


def test_credit_above_the_wider_condor_wing_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics):
        recompute_economics(_bypass(wide_put_condor(), estimated_net_premium_per_unit=20.0))


def test_long_put_debit_at_or_above_the_strike_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="cannot equal or exceed its strike"):
        recompute_economics(_bypass(long_put(), estimated_net_premium_per_unit=-190.0))


def test_invalid_economics_block_the_report_without_crashing() -> None:
    report = evaluate(_bypass(long_call(), estimated_net_premium_per_unit=0.0))
    assert report.blocked is True
    assert OptionRiskFlag.INVALID_ECONOMICS.value in flags(report)
    assert report.recomputed_max_loss is None
    assert report.recommended_quantity == 0


def test_non_positive_quantity_is_invalid() -> None:
    for bad in (0, -3):
        with pytest.raises(InvalidOptionEconomics, match="quantity must be positive"):
            recompute_economics(_bypass(iron_condor(), quantity=bad))


def test_non_positive_multiplier_is_invalid() -> None:
    with pytest.raises(InvalidOptionEconomics, match="multiplier must be positive"):
        recompute_economics(_bypass(iron_condor(), contract_multiplier=0))


# --------------------------------------------------------------------------
# Days to expiry
# --------------------------------------------------------------------------
def test_long_dated_expiry_is_clean() -> None:
    report = evaluate(iron_condor(), as_of=TODAY)
    assert report.days_to_expiry == 44
    assert not (flags(report) & {"SHORT_DTE", "DTE_BELOW_MINIMUM", "EXPIRED"})


def test_expiry_today_is_below_the_minimum() -> None:
    report = evaluate(iron_condor(), as_of=EXPIRY)
    assert report.days_to_expiry == 0
    assert report.blocked is True
    assert OptionRiskFlag.DTE_BELOW_MINIMUM.value in flags(report)


def test_expired_contracts_are_blocked() -> None:
    report = evaluate(iron_condor(), as_of=date(2026, 10, 17))
    assert report.days_to_expiry == -1
    assert report.blocked is True
    assert OptionRiskFlag.EXPIRED.value in flags(report)


def test_one_day_to_expiry_is_the_minimum_and_watches() -> None:
    report = evaluate(iron_condor(), as_of=date(2026, 10, 15))
    assert report.days_to_expiry == 1
    assert report.blocked is False
    assert OptionRiskFlag.SHORT_DTE.value in flags(report)


def test_seven_days_still_watches_and_eight_does_not() -> None:
    seven = evaluate(iron_condor(), as_of=date(2026, 10, 9))
    eight = evaluate(iron_condor(), as_of=date(2026, 10, 8))
    assert seven.days_to_expiry == 7
    assert OptionRiskFlag.SHORT_DTE.value in flags(seven)
    assert eight.days_to_expiry == 8
    assert OptionRiskFlag.SHORT_DTE.value not in flags(eight)


def test_the_evaluation_date_is_injected_not_read_from_the_clock() -> None:
    engine = OptionRiskEngine(today_provider=lambda: date(2026, 10, 17))
    report = engine.evaluate(iron_condor(), portfolio())
    assert report.as_of == date(2026, 10, 17)
    assert report.blocked is True


# --------------------------------------------------------------------------
# Capital at risk and policy limits
# --------------------------------------------------------------------------
def test_risk_percentages_are_computed_from_defined_loss() -> None:
    report = evaluate(iron_condor(quantity=2))
    assert report.risk_amount == Decimal("1730.00")
    assert report.risk_pct_equity == Decimal("0.017300")
    assert report.risk_pct_buying_power == Decimal("0.008650")


def test_risk_percentage_scales_with_quantity() -> None:
    one = evaluate(iron_condor(quantity=1)).risk_pct_equity
    two = evaluate(iron_condor(quantity=2)).risk_pct_equity
    assert two == one * 2


def test_defined_loss_within_the_equity_limit_passes() -> None:
    # 5 contracts of 865 = 4,325 on 100,000 equity = 4.325%, under 5%.
    report = evaluate(iron_condor(quantity=5))
    assert report.blocked is False
    assert OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT.value not in flags(report)


def test_defined_loss_above_the_equity_limit_reduces_rather_than_rejects() -> None:
    # 6 contracts of 865 = 5,190 = 5.19% of equity, over the 5% limit.
    # A position that is merely too large is a reduction, not a rejection --
    # the same treatment the equity RiskEngine gives max_trade_size.
    report = evaluate(iron_condor(quantity=6))
    assert OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT.value in flags(report)
    assert report.blocked is False
    assert report.recommended_quantity == 5


def test_equity_limit_exactly_at_threshold_passes() -> None:
    policy = OptionRiskPolicy(max_defined_loss_pct_equity=0.00865)
    report = evaluate(iron_condor(quantity=1), policy=policy)
    assert report.risk_pct_equity == Decimal("0.008650")
    assert OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT.value not in flags(report)


def test_buying_power_limit_reduces_independently_of_equity() -> None:
    # Generous equity, thin buying power: only the buying-power rule should fire.
    report = evaluate(
        iron_condor(quantity=2),
        pf=portfolio(equity=10_000_000.0, buying_power=10_000.0),
    )
    assert OptionRiskFlag.MAX_LOSS_BUYING_POWER_LIMIT.value in flags(report)
    assert OptionRiskFlag.MAX_LOSS_EQUITY_LIMIT.value not in flags(report)
    assert report.blocked is False
    assert report.recommended_quantity == 1  # 10% of 10,000 = 1,000; 1,000 // 865 = 1


def test_contract_limit_reduces_to_the_cap() -> None:
    report = evaluate(
        iron_condor(quantity=21),
        pf=portfolio(equity=100_000_000.0, buying_power=100_000_000.0),
    )
    assert OptionRiskFlag.CONTRACT_LIMIT.value in flags(report)
    assert report.blocked is False
    assert report.recommended_quantity == 20


def test_contract_limit_exactly_at_threshold_passes() -> None:
    report = evaluate(
        iron_condor(quantity=20),
        pf=portfolio(equity=100_000_000.0, buying_power=100_000_000.0),
    )
    assert OptionRiskFlag.CONTRACT_LIMIT.value not in flags(report)


def test_recommended_quantity_only_reduces_contracts() -> None:
    report = evaluate(iron_condor(quantity=10))
    # 5% of 100,000 = 5,000; 5,000 // 865 = 5 contracts.
    assert report.recommended_quantity == 5
    assert report.quantity == 10
    assert report.strategy is OptionStrategy.IRON_CONDOR
    assert report.leg_count == 4
    assert report.expiry == EXPIRY


def test_recommended_quantity_never_exceeds_the_request() -> None:
    report = evaluate(iron_condor(quantity=1))
    assert report.recommended_quantity == 1


def test_recommended_quantity_is_zero_when_blocked() -> None:
    report = evaluate(iron_condor(), as_of=date(2026, 10, 17))
    assert report.recommended_quantity == 0


def test_very_small_account_blocks_because_no_size_fits() -> None:
    # 5% of 1,000 is 50, far below one contract's 865 defined loss, so the
    # reduction reaches zero and only then does it block.
    report = evaluate(iron_condor(), pf=portfolio(equity=1_000.0, buying_power=1_000.0))
    assert report.recommended_quantity == 0
    assert report.blocked is True
    assert any("Risk-adjusted contract count is zero" in r for r in report.reasons)


# --------------------------------------------------------------------------
# Portfolio availability
# --------------------------------------------------------------------------
def test_zero_buying_power_is_unavailable_not_zero_percent() -> None:
    report = evaluate(iron_condor(), pf=portfolio(buying_power=0.0))
    assert report.risk_pct_buying_power is None
    assert OptionRiskFlag.BUYING_POWER_UNAVAILABLE.value in flags(report)
    assert report.blocked is True


def test_absent_buying_power_fails_closed() -> None:
    broken = PortfolioSnapshot.model_construct(
        equity=100_000.0, cash=50_000.0, buying_power=None, daily_pnl_pct=0.0,
        current_positions={}, positions=[], source="MANUAL",
    )
    report = evaluate(iron_condor(), pf=broken)
    assert report.risk_pct_buying_power is None
    assert report.blocked is True


def test_unavailable_buying_power_can_be_configured_to_watch() -> None:
    policy = OptionRiskPolicy(block_when_buying_power_unavailable=False)
    report = evaluate(iron_condor(), pf=portfolio(buying_power=0.0), policy=policy)
    assert OptionRiskFlag.BUYING_POWER_UNAVAILABLE.value in flags(report)
    assert report.blocked is False


def test_non_positive_equity_fails_closed() -> None:
    broken = PortfolioSnapshot.model_construct(
        equity=0.0, cash=0.0, buying_power=1_000.0, daily_pnl_pct=0.0,
        current_positions={}, positions=[], source="MANUAL",
    )
    report = evaluate(iron_condor(), pf=broken)
    assert report.blocked is True
    assert OptionRiskFlag.PORTFOLIO_EQUITY_UNAVAILABLE.value in flags(report)
    assert report.risk_pct_equity is None


# --------------------------------------------------------------------------
# Market context and provenance
# --------------------------------------------------------------------------
def test_absent_market_context_applies_no_context_rule() -> None:
    report = evaluate(iron_condor(), market=None)
    assert report.market_context_source is None
    assert report.liquidity_score is None
    assert report.annualized_volatility is None
    assert report.max_drawdown_30d is None
    assert not (flags(report) & {"LIQUIDITY_LOW", "VOLATILITY_ELEVATED", "DRAWDOWN_ELEVATED"})


def test_absent_individual_values_stay_absent() -> None:
    report = evaluate(iron_condor(), market=OptionMarketContext(underlying="AAPL"))
    assert report.liquidity_score is None
    assert report.annualized_volatility is None
    assert report.max_drawdown_30d is None
    assert report.blocked is False


def test_good_liquidity_passes() -> None:
    report = evaluate(iron_condor(),
                      market=OptionMarketContext(underlying="AAPL", liquidity_score=0.9))
    assert OptionRiskFlag.LIQUIDITY_LOW.value not in flags(report)


def test_low_liquidity_blocks() -> None:
    report = evaluate(iron_condor(),
                      market=OptionMarketContext(underlying="AAPL", liquidity_score=0.1))
    assert report.blocked is True
    assert OptionRiskFlag.LIQUIDITY_LOW.value in flags(report)


def test_elevated_volatility_watches_but_does_not_block() -> None:
    report = evaluate(iron_condor(),
                      market=OptionMarketContext(underlying="AAPL", annualized_volatility=0.95))
    assert report.blocked is False
    assert OptionRiskFlag.VOLATILITY_ELEVATED.value in flags(report)


def test_elevated_drawdown_watches_but_does_not_block() -> None:
    report = evaluate(iron_condor(),
                      market=OptionMarketContext(underlying="AAPL", max_drawdown_30d=0.4))
    assert report.blocked is False
    assert OptionRiskFlag.DRAWDOWN_ELEVATED.value in flags(report)


def test_provenance_is_always_caller_supplied() -> None:
    report = evaluate(iron_condor(),
                      market=OptionMarketContext(underlying="AAPL", liquidity_score=0.9))
    assert report.market_context_source == "CALLER_SUPPLIED"
    assert report.premium_source == "CALLER_SUPPLIED"


def test_market_risk_snapshot_is_adapted_and_still_caller_supplied() -> None:
    snapshot = MarketRiskSnapshot(
        symbol="AAPL", annualized_volatility=0.28, max_drawdown_30d=0.09, liquidity_score=0.88
    )
    report = evaluate(iron_condor(), market=snapshot)
    assert report.market_context_source == "CALLER_SUPPLIED"
    assert report.liquidity_score == 0.88
    assert report.annualized_volatility == 0.28


def test_an_unexpected_market_type_is_refused() -> None:
    with pytest.raises(TypeError, match="OptionMarketContext"):
        evaluate(iron_condor(), market={"liquidity_score": 0.9})


# --------------------------------------------------------------------------
# Structural defence against objects that bypassed validation
# --------------------------------------------------------------------------
def test_a_non_option_proposal_is_refused() -> None:
    with pytest.raises(TypeError, match="OptionTradeProposal"):
        OptionRiskEngine().evaluate("not a proposal", portfolio())


def test_naked_short_remains_rejected_even_when_bypassed() -> None:
    naked = _bypass(
        long_call(),
        legs=[leg("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0)],
        estimated_net_premium_per_unit=4.20,
    )
    report = evaluate(naked)
    assert report.blocked is True
    assert OptionRiskFlag.NAKED_SHORT.value in flags(report)


def test_unsupported_strategy_is_blocked_by_the_allowlist() -> None:
    policy = OptionRiskPolicy(
        allowed_strategies=frozenset({OptionStrategy.LONG_CALL, OptionStrategy.LONG_PUT})
    )
    report = evaluate(iron_condor(), policy=policy)
    assert report.blocked is True
    assert OptionRiskFlag.UNSUPPORTED_STRATEGY.value in flags(report)


def test_mixed_expiry_cannot_slip_through() -> None:
    other = date(2026, 11, 20)
    mixed = _bypass(
        put_credit_spread(),
        legs=[
            leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0),
            leg("AAPL261120P00200000", "SELL", OptionType.PUT, 200.0, expiry=other),
        ],
    )
    report = evaluate(mixed)
    assert report.blocked is True
    assert OptionRiskFlag.STRUCTURE_INVALID.value in flags(report)
    assert any("mixed expiries" in reason for reason in report.reasons)


def test_mixed_underlying_cannot_slip_through() -> None:
    mixed = _bypass(
        put_credit_spread(),
        legs=[
            leg("MSFT261016P00190000", "BUY", OptionType.PUT, 190.0),
            leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0),
        ],
    )
    report = evaluate(mixed)
    assert report.blocked is True
    assert OptionRiskFlag.STRUCTURE_INVALID.value in flags(report)


def test_wrong_leg_count_cannot_slip_through() -> None:
    broken = _bypass(
        iron_condor(),
        legs=[
            leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0),
            leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0),
        ],
    )
    report = evaluate(broken)
    assert report.blocked is True
    assert OptionRiskFlag.STRUCTURE_INVALID.value in flags(report)


def test_too_many_legs_is_blocked() -> None:
    broken = _bypass(
        iron_condor(),
        legs=list(iron_condor().legs)
        + [leg("AAPL261016C00250000", "BUY", OptionType.CALL, 250.0)],
    )
    report = evaluate(broken)
    assert report.blocked is True
    assert OptionRiskFlag.LEG_LIMIT.value in flags(report)


def test_non_unit_ratio_cannot_slip_through() -> None:
    broken = _bypass(
        put_credit_spread(),
        legs=[
            OptionLeg.model_construct(
                option_symbol="AAPL261016P00190000", side="BUY",
                option_type=OptionType.PUT, strike=190.0, expiry=EXPIRY,
                ratio=2, position_effect="OPEN",
            ),
            leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0),
        ],
    )
    report = evaluate(broken)
    assert report.blocked is True
    assert any("non-unit ratios" in reason for reason in report.reasons)


def test_a_blocked_structure_skips_economics_rather_than_crashing() -> None:
    broken = _bypass(iron_condor(), legs=[])
    report = evaluate(broken)
    assert report.blocked is True
    assert report.recomputed_max_loss is None


# --------------------------------------------------------------------------
# Score and report shape
# --------------------------------------------------------------------------
def test_a_clean_structure_scores_zero() -> None:
    assert evaluate(iron_condor()).risk_score == 0


def test_score_accumulates_and_caps_at_100() -> None:
    report = evaluate(
        iron_condor(quantity=25),
        pf=portfolio(equity=1_000.0, buying_power=1_000.0),
        market=OptionMarketContext(
            underlying="AAPL", liquidity_score=0.1, annualized_volatility=0.95,
            max_drawdown_30d=0.4,
        ),
        as_of=date(2026, 10, 17),
    )
    assert report.risk_score == 100
    assert report.blocked is True
    assert OptionRiskFlag.EXPIRED.value in flags(report)


def test_watch_flags_alone_do_not_block() -> None:
    report = evaluate(
        iron_condor(),
        market=OptionMarketContext(
            underlying="AAPL", annualized_volatility=0.95, max_drawdown_30d=0.4
        ),
        as_of=date(2026, 10, 15),
    )
    assert set(flags(report)) == {"SHORT_DTE", "VOLATILITY_ELEVATED", "DRAWDOWN_ELEVATED"}
    assert report.blocked is False
    assert report.risk_score == 30


def test_report_records_structure_identity_for_later_governor_use() -> None:
    report = evaluate(iron_condor(quantity=2))
    assert report.instrument_type == "option"
    assert report.underlying == "AAPL"
    assert report.multiplier == 100
    assert report.risk_width == Decimal("10.00")
    assert report.net_credit_per_unit == Decimal("1.35")
    assert report.net_debit_per_unit is None


def test_report_marks_underlying_concentration_unavailable() -> None:
    report = evaluate(iron_condor())
    assert report.underlying_concentration_available is False
    assert any("cannot be derived" in check.message for check in report.checks)


def test_every_check_carries_a_message() -> None:
    report = evaluate(iron_condor())
    assert report.checks
    assert all(check.message.strip() for check in report.checks)


def test_report_serializes_money_without_float_drift() -> None:
    payload = evaluate(iron_condor(quantity=2)).model_dump(mode="json")
    assert payload["recomputed_max_loss"] == "1730.00"
    assert payload["recomputed_max_profit"] == "270.00"
    assert payload["risk_pct_equity"] == "0.017300"


# --------------------------------------------------------------------------
# Fractional precision
# --------------------------------------------------------------------------
def test_fractional_premium_is_exact() -> None:
    economics = recompute_economics(
        build(OptionStrategy.VERTICAL_CREDIT_SPREAD,
              [leg("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0),
               leg("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0)], 1.73)
    )
    assert economics.max_profit == Decimal("173.00")
    assert economics.max_loss == Decimal("827.00")


def test_fractional_strikes_are_exact() -> None:
    economics = recompute_economics(
        build(OptionStrategy.VERTICAL_CREDIT_SPREAD,
              [leg("SPY261016P00512500", "BUY", OptionType.PUT, 512.5),
               leg("SPY261016P00515000", "SELL", OptionType.PUT, 515.0)], 0.83,
              underlying="SPY")
    )
    assert economics.risk_width == Decimal("2.5")
    assert economics.max_profit == Decimal("83.00")
    assert economics.max_loss == Decimal("167.00")


def test_repeated_evaluation_is_byte_identical() -> None:
    proposal = iron_condor(quantity=2)
    first = evaluate(proposal).model_dump(mode="json")
    second = evaluate(proposal).model_dump(mode="json")
    assert first == second


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------
def _bypass(proposal: OptionTradeProposal, **overrides) -> OptionTradeProposal:
    """Build a proposal that skipped model validation.

    The model makes these structures unconstructable. The engine must still
    refuse them, because it cannot assume its input arrived through validation.
    """

    payload = proposal.model_dump()
    payload.update(overrides)
    payload["legs"] = overrides.get("legs", list(proposal.legs))
    return OptionTradeProposal.model_construct(**payload)
