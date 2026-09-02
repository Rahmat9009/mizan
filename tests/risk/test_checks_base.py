"""The nineteen base checks: the passing case, the breach, the missing datum and the exact boundary.

The boundary cases are the point of most of these: a limit that rejects the order sitting exactly on it
is a different (and wrong) product from one that permits it, and only a test says which one this is.
Where a scenario necessarily disturbs another check (a two-leg spread has no quotes in the fixture
snapshot, for instance), the assertion is on that check's own CheckResult rather than on the verdict.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mizan import risk
from mizan.contracts import format_ts
from tests.fixtures import (
    AAPL_PRICE,
    FIXED_NOW,
    FIXED_NOW_STR,
    OPTION_EXPIRY,
    OPTION_OCC,
    OPTION_STRIKE,
    killer_demo_context,
    killer_demo_policy,
    make_context,
    make_market_snapshot,
    make_option_proposal,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

WINDOW_POLICY_CHECKS = {"duplicate_order": {"enabled": True, "severity": "blocking", "window_seconds": 60}}


def _recent_order(**overrides):
    base = {
        "proposal_id": "0" * 64,
        "symbol": "AAPL",
        "side": "buy",
        "total_quantity": "10",
        "submitted_at": format_ts(FIXED_NOW - timedelta(seconds=30)),
        "status": "accepted",
    }
    base.update(overrides)
    return base


def _spread_legs():
    """Two option legs on AAPL: the fixture snapshot quotes only one of them, which is the point."""
    return [
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": "call",
            "strike": OPTION_STRIKE,
            "expiry": OPTION_EXPIRY,
            "quantity": "5",
            "limit_price": "1.85",
            "order_type": "limit",
        },
        {
            "leg_index": 1,
            "side": "sell",
            "contract_type": "call",
            "strike": "240",
            "expiry": OPTION_EXPIRY,
            "quantity": "5",
            "limit_price": "0.55",
            "order_type": "limit",
        },
    ]


# ------------------------------------------------------------------------------------------------
# market_data_presence / portfolio_state_presence / proposal_expiry (always on)
# ------------------------------------------------------------------------------------------------
def test_market_data_presence_passes_with_a_quote(context_for, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "market_data_presence")
    assert check.passed is True
    assert check.data_source == "alpaca:paper"


def test_market_data_presence_blocks_without_a_snapshot(context_for, check_of, codes):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy, market_snapshot=None), policy)
    check = check_of(evaluation, "market_data_presence")
    assert (check.passed, check.severity) == (False, "blocking")
    assert "MARKET_DATA_MISSING" in codes(evaluation)
    assert evaluation.verdict == "REJECT"


def test_market_data_presence_blocks_without_a_quote_for_the_symbol(context_for, codes):
    policy = make_policy()
    context = context_for(policy, market_snapshot=make_market_snapshot(quotes={}))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "PRICE_MISSING" in codes(evaluation)
    assert evaluation.verdict == "REJECT"


def test_market_data_presence_blocks_an_option_leg_without_an_option_quote(context_for, codes):
    policy = make_policy()
    context = context_for(policy, market_snapshot=make_market_snapshot(option_quotes={}))
    evaluation = risk.evaluate(make_option_proposal(), context, policy)
    assert "PRICE_MISSING" in codes(evaluation)
    assert evaluation.verdict == "REJECT"


def test_portfolio_state_presence_blocks_without_a_snapshot(context_for, check_of, codes):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy, portfolio_snapshot=None), policy)
    check = check_of(evaluation, "portfolio_state_presence")
    assert (check.passed, check.severity) == (False, "blocking")
    assert "PORTFOLIO_STATE_MISSING" in codes(evaluation)


def test_proposal_expiry_passes_one_microsecond_before_the_deadline(context_for, check_of):
    policy = make_policy()
    proposal = make_proposal(
        created_at=format_ts(FIXED_NOW - timedelta(minutes=5)),
        expires_at=format_ts(FIXED_NOW + timedelta(microseconds=1)),
    )
    evaluation = risk.evaluate(proposal, context_for(policy), policy)
    assert check_of(evaluation, "proposal_expiry").passed is True


def test_proposal_expiry_blocks_at_the_deadline_itself(context_for, codes):
    policy = make_policy()
    proposal = make_proposal(
        created_at=format_ts(FIXED_NOW - timedelta(minutes=5)), expires_at=FIXED_NOW_STR
    )
    evaluation = risk.evaluate(proposal, context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "PROPOSAL_EXPIRED" in codes(evaluation)


# ------------------------------------------------------------------------------------------------
# restricted_symbol / restricted_strategy / leg_limit
# ------------------------------------------------------------------------------------------------
def test_restricted_symbol_rejects_a_listed_name(context_for, codes):
    policy = make_policy(restricted={"symbols": ["AAPL"], "strategies": []})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "RESTRICTED_SYMBOL" in codes(evaluation)


def test_restricted_symbol_passes_a_name_that_is_not_listed(context_for, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert check_of(evaluation, "restricted_symbol").passed is True


def test_restricted_strategy_rejects_a_listed_strategy(context_for, codes):
    policy = make_policy(restricted={"symbols": [], "strategies": ["long_equity"]})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "RESTRICTED_STRATEGY" in codes(evaluation)


def test_leg_limit_passes_at_exactly_the_limit_and_fails_one_over(context_for, check_of):
    proposal = make_option_proposal(strategy="bull_call_spread", legs=_spread_legs())
    at_limit = make_policy(order={"max_notional": "10000.00", "max_quantity": "20", "max_legs": 2})
    over = make_policy(order={"max_notional": "10000.00", "max_quantity": "20", "max_legs": 1})
    assert check_of(risk.evaluate(proposal, context_for(at_limit), at_limit), "leg_limit").passed is True
    failed = check_of(risk.evaluate(proposal, context_for(over), over), "leg_limit")
    assert (failed.passed, failed.reason_code, failed.threshold, failed.actual) == (
        False,
        "LEG_LIMIT_EXCEEDED",
        "1",
        "2",
    )


# ------------------------------------------------------------------------------------------------
# position_limit / capital_threshold
# ------------------------------------------------------------------------------------------------
def test_position_limit_passes_at_exactly_the_limit(context_for, check_of):
    policy = make_policy(order={"max_notional": "10000.00", "max_quantity": "10", "max_legs": 4})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "PASS"
    assert check_of(evaluation, "position_limit").passed is True


def test_position_limit_fails_one_unit_over(context_for, codes, check_of):
    policy = make_policy(order={"max_notional": "10000.00", "max_quantity": "9", "max_legs": 4})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "POSITION_LIMIT_EXCEEDED" in codes(evaluation)
    assert check_of(evaluation, "position_limit").threshold == "9"


def test_position_limit_reduces_to_the_cap_when_configured_as_a_warning(context_for):
    policy = make_policy(
        order={"max_notional": "10000.00", "max_quantity": "6", "max_legs": 4},
        checks={"position_limit": {"enabled": True, "severity": "warning"}},
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REDUCE"
    assert evaluation.recommended_quantity == "6"


def test_capital_threshold_passes_at_exactly_the_order_notional(context_for, check_of):
    policy = make_policy(order={"max_notional": "2285", "max_quantity": "20", "max_legs": 4})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "PASS"
    assert check_of(evaluation, "capital_threshold").actual == "2285"


def test_capital_threshold_fails_one_cent_under(context_for, codes):
    policy = make_policy(order={"max_notional": "2284.99", "max_quantity": "20", "max_legs": 4})
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "CAPITAL_THRESHOLD_EXCEEDED" in codes(evaluation)


def test_capital_threshold_blocks_when_the_order_cannot_be_valued(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, market_snapshot=make_market_snapshot(quotes={}))
    check = check_of(risk.evaluate(make_proposal(), context, policy), "capital_threshold")
    assert (check.passed, check.severity, check.reason_code) == (False, "blocking", "PRICE_MISSING")


# ------------------------------------------------------------------------------------------------
# buying_power_sufficiency / buying_power_utilization
# ------------------------------------------------------------------------------------------------
def test_buying_power_sufficiency_passes_with_exactly_enough(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="2285"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert check_of(evaluation, "buying_power_sufficiency").passed is True


def test_buying_power_sufficiency_fails_one_cent_short(context_for, codes, check_of):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="2284.99"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "INSUFFICIENT_BUYING_POWER" in codes(evaluation)
    assert check_of(evaluation, "buying_power_sufficiency").recommended_quantity == "0"


def test_buying_power_missing_blocks_and_is_not_zero(context_for, codes, check_of):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power=None))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "BUYING_POWER_MISSING" in codes(evaluation)
    assert "INSUFFICIENT_BUYING_POWER" not in codes(evaluation)
    assert check_of(evaluation, "buying_power_sufficiency").severity == "blocking"
    assert evaluation.data_complete is False


def test_buying_power_utilization_passes_at_exactly_the_limit(context_for, check_of):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.15",
            "max_sector_concentration_pct": "0.25",
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "1",
        }
    )
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="2285"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    check = check_of(evaluation, "buying_power_utilization")
    assert (check.passed, check.actual) == (True, "1")


def test_buying_power_utilization_fails_just_over_the_limit(context_for, codes):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.15",
            "max_sector_concentration_pct": "0.25",
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.99",
        }
    )
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="2285"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "BUYING_POWER_UTILIZATION_EXCEEDED" in codes(evaluation)


# ------------------------------------------------------------------------------------------------
# concentration_limit / sector_concentration / drawdown_limit
# ------------------------------------------------------------------------------------------------
def test_concentration_limit_passes_at_exactly_the_projected_share(context_for, check_of):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.02285",
            "max_sector_concentration_pct": None,
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.80",
        }
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "concentration_limit")
    assert (check.passed, check.actual) == (True, "0.02285")
    assert evaluation.verdict == "PASS"


def test_concentration_limit_reduces_when_the_share_is_one_step_too_large(context_for, codes):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.02284",
            "max_sector_concentration_pct": None,
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.80",
        }
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert evaluation.verdict == "REDUCE"  # the fixture policy configures this check as a warning
    assert evaluation.recommended_quantity == "9"
    assert "CONCENTRATION_LIMIT_EXCEEDED" in codes(evaluation)


def test_concentration_counts_what_is_already_held_in_the_symbol(context_for, check_of):
    policy = make_policy()
    held = make_portfolio_snapshot(
        positions=[
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "quantity": "50",
                "market_value": "11425",
                "sector": "Technology",
                "occ_symbol": None,
                "delta": "50",
                "gamma": "0",
                "vega": "0",
            }
        ]
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy, portfolio_snapshot=held), policy)
    assert check_of(evaluation, "concentration_limit").actual == "0.1371"  # (11425 + 2285) / 100000


def test_sector_concentration_passes_at_exactly_the_limit(context_for, check_of):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.15",
            "max_sector_concentration_pct": "0.2289",
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.80",
        }
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "sector_concentration")
    assert (check.passed, check.actual) == (True, "0.2289")


def test_sector_concentration_fails_one_step_over(context_for, codes):
    policy = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.15",
            "max_sector_concentration_pct": "0.2288",
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.80",
        }
    )
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert "SECTOR_CONCENTRATION_EXCEEDED" in codes(evaluation)


def test_sector_concentration_blocks_when_the_sector_is_unknown(context_for, codes, check_of):
    policy = make_policy()
    context = context_for(policy, market_snapshot=make_market_snapshot(sectors={}))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    check = check_of(evaluation, "sector_concentration")
    assert (check.passed, check.severity, check.reason_code) == (False, "blocking", "SECTOR_DATA_MISSING")
    assert "SECTOR_DATA_MISSING" in codes(evaluation)
    assert evaluation.data_complete is False


def test_drawdown_limit_passes_at_exactly_the_limit(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(peak_equity="125000"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    check = check_of(evaluation, "drawdown_limit")
    assert (check.passed, check.actual, check.threshold) == (True, "0.2", "0.2")


def test_drawdown_limit_blocks_past_the_limit(context_for, codes):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(peak_equity="130000"))
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "DRAWDOWN_LIMIT_BREACHED" in codes(evaluation)


def test_drawdown_limit_blocks_when_peak_equity_is_unknown(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(peak_equity=None))
    check = check_of(risk.evaluate(make_proposal(), context, policy), "drawdown_limit")
    assert (check.passed, check.severity, check.reason_code) == (
        False,
        "blocking",
        "PORTFOLIO_STATE_MISSING",
    )


# ------------------------------------------------------------------------------------------------
# duplicate_order / erroneous_order
# ------------------------------------------------------------------------------------------------
def test_duplicate_order_rejects_the_same_order_inside_the_window(context_for, codes):
    policy = make_policy(checks=WINDOW_POLICY_CHECKS)
    context = context_for(policy, recent_orders=[_recent_order()])
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "DUPLICATE_ORDER" in codes(evaluation)


def test_duplicate_order_passes_at_the_edge_of_the_window(context_for, check_of):
    policy = make_policy(checks=WINDOW_POLICY_CHECKS)
    just_outside = _recent_order(submitted_at=format_ts(FIXED_NOW - timedelta(seconds=61)))
    context = context_for(policy, recent_orders=[just_outside])
    assert check_of(risk.evaluate(make_proposal(), context, policy), "duplicate_order").passed is True


def test_duplicate_order_ignores_a_different_symbol_or_size(context_for, check_of):
    policy = make_policy(checks=WINDOW_POLICY_CHECKS)
    others = [_recent_order(symbol="MSFT"), _recent_order(total_quantity="7"), _recent_order(side="sell")]
    context = context_for(policy, recent_orders=others)
    assert check_of(risk.evaluate(make_proposal(), context, policy), "duplicate_order").passed is True


def test_erroneous_order_accepts_a_limit_exactly_at_the_deviation_threshold(context_for, check_of):
    policy = make_policy()
    proposal = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "274.2",  # exactly 20% above the 228.5 quote
                "order_type": "limit",
            }
        ]
    )
    check = check_of(risk.evaluate(proposal, context_for(policy), policy), "erroneous_order")
    assert (check.passed, check.reason_code) == (True, None)


def test_erroneous_order_rejects_a_limit_beyond_the_deviation_threshold(context_for, codes):
    policy = make_policy()
    proposal = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "274.21",
                "order_type": "limit",
            }
        ]
    )
    evaluation = risk.evaluate(proposal, context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "ERRONEOUS_PRICE_DEVIATION" in codes(evaluation)


def test_erroneous_order_accepts_a_quantity_exactly_at_the_multiple(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, recent_orders=[_recent_order(total_quantity="2", symbol="AAPL")])
    check = check_of(risk.evaluate(make_proposal(), context, policy), "erroneous_order")
    assert check.passed is True


def test_erroneous_order_rejects_a_quantity_far_beyond_recent_orders(context_for, codes, check_of):
    policy = make_policy()
    context = context_for(policy, recent_orders=[_recent_order(total_quantity="1", symbol="AAPL")])
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert "ERRONEOUS_QUANTITY_DEVIATION" in codes(evaluation)
    assert check_of(evaluation, "erroneous_order").actual == "10"


def test_erroneous_order_blocks_without_market_data(context_for, check_of):
    policy = make_policy()
    context = context_for(policy, market_snapshot=None)
    check = check_of(risk.evaluate(make_proposal(), context, policy), "erroneous_order")
    assert (check.passed, check.reason_code) == (False, "MARKET_DATA_MISSING")


# ------------------------------------------------------------------------------------------------
# days_to_expiry and the portfolio greeks
# ------------------------------------------------------------------------------------------------
def _options_policy(**options):
    base = {
        "max_portfolio_delta": "500",
        "max_portfolio_gamma": "100",
        "max_portfolio_vega": "300",
        "min_days_to_expiry": 7,
        "max_days_to_expiry": 45,
    }
    base.update(options)
    return killer_demo_policy(options=base)


@pytest.mark.parametrize("minimum", [7, 23])
def test_days_to_expiry_passes_down_to_the_exact_minimum(minimum, check_of):
    policy = _options_policy(min_days_to_expiry=minimum)
    evaluation = risk.evaluate(make_option_proposal(), killer_demo_context(policy=policy), policy)
    assert check_of(evaluation, "days_to_expiry").passed is True


def test_days_to_expiry_blocks_one_day_short_of_the_minimum(codes):
    policy = _options_policy(min_days_to_expiry=24)
    evaluation = risk.evaluate(make_option_proposal(), killer_demo_context(policy=policy), policy)
    assert "DTE_BELOW_MINIMUM" in codes(evaluation)
    assert evaluation.verdict == "REJECT"


def test_days_to_expiry_blocks_one_day_past_the_maximum(codes):
    policy = _options_policy(min_days_to_expiry=1, max_days_to_expiry=22)
    evaluation = risk.evaluate(make_option_proposal(), killer_demo_context(policy=policy), policy)
    assert "DTE_ABOVE_MAXIMUM" in codes(evaluation)


def test_days_to_expiry_is_not_applied_to_equity(context_for, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "days_to_expiry")
    assert (check.passed, check.detail) == (True, "not an options proposal")


def test_delta_limit_passes_at_exactly_the_projected_delta(check_of):
    policy = _options_policy(max_portfolio_delta="386")
    evaluation = risk.evaluate(
        make_option_proposal(legs=_twenty_contract_legs()), killer_demo_context(policy=policy), policy
    )
    check = check_of(evaluation, "options_delta_limit")
    assert (check.passed, check.actual, check.threshold) == (True, "386", "386")


def test_delta_limit_fails_one_unit_below_the_projection(codes):
    policy = _options_policy(max_portfolio_delta="385")
    evaluation = risk.evaluate(
        make_option_proposal(legs=_twenty_contract_legs()), killer_demo_context(policy=policy), policy
    )
    assert "OPTIONS_DELTA_LIMIT_EXCEEDED" in codes(evaluation)


def test_gamma_and_vega_limits_bite_on_the_same_projection(codes):
    policy = _options_policy(max_portfolio_gamma="41", max_portfolio_vega="283")
    evaluation = risk.evaluate(
        make_option_proposal(legs=_twenty_contract_legs()), killer_demo_context(policy=policy), policy
    )
    assert {"OPTIONS_GAMMA_LIMIT_EXCEEDED", "OPTIONS_VEGA_LIMIT_EXCEEDED"} <= codes(evaluation)


def test_greeks_missing_from_the_portfolio_blocks(check_of, codes):
    policy = _options_policy()
    context = killer_demo_context(policy=policy, portfolio_snapshot=make_portfolio_snapshot(greeks=None))
    evaluation = risk.evaluate(make_option_proposal(), context, policy)
    check = check_of(evaluation, "options_delta_limit")
    assert (check.passed, check.severity, check.reason_code) == (False, "blocking", "GREEKS_MISSING")
    assert evaluation.data_complete is False


def test_greeks_missing_from_the_option_quote_blocks(check_of):
    policy = _options_policy()
    quotes = make_market_snapshot().model_dump(mode="json")["option_quotes"]
    quotes[OPTION_OCC]["delta"] = None
    context = killer_demo_context(policy=policy, market_snapshot=make_market_snapshot(option_quotes=quotes))
    check = check_of(
        risk.evaluate(make_option_proposal(), context, policy), "options_delta_limit"
    )
    assert (check.passed, check.reason_code) == (False, "GREEKS_MISSING")


def _twenty_contract_legs():
    return [
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": "call",
            "strike": OPTION_STRIKE,
            "expiry": OPTION_EXPIRY,
            "quantity": "20",
            "limit_price": "1.85",
            "order_type": "limit",
        }
    ]


def test_the_quote_price_is_what_values_the_order(context_for, check_of):
    """A sanity anchor for the valuation rule: the fixture quote, not the limit, sets the notional."""
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    assert AAPL_PRICE == "228.5"
    assert check_of(evaluation, "capital_threshold").actual == "2285"


def test_a_context_without_recent_orders_never_reports_a_duplicate(check_of):
    policy = make_policy(checks=WINDOW_POLICY_CHECKS)
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref, recent_orders=[])
    assert check_of(risk.evaluate(make_proposal(), context, policy), "duplicate_order").passed is True
