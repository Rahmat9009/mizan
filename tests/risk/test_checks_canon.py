"""The seventeen Risk-Canon checks of Addendum 1 section C: pass, breach, missing state, boundary.

Every one of these reads state that arrives on the RiskContext (ADR-0006), so each also has a
missing-state case: with the section enabled and the state absent, the check must BLOCK rather than
assume zero (Hard Rule E2 / R-RUIN-4).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mizan import risk
from mizan.contracts import format_ts
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    OPTION_EXPIRY,
    OPTION_OCC,
    OPTION_STRIKE,
    make_agent_state,
    make_aggregate_state,
    make_calendar,
    make_institutional_policy,
    make_market_snapshot,
    make_option_proposal,
    make_path_state,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

INVALIDATION = {"level": "224", "direction": "below", "target": "240"}


def _proposal(**overrides):
    """The fixture equity proposal with the invalidation the institutional policy requires."""
    fields = {"invalidation": INVALIDATION}
    fields.update(overrides)
    return make_proposal(**fields)


def _option_proposal(**overrides):
    fields = {"invalidation": INVALIDATION}
    fields.update(overrides)
    return make_option_proposal(**fields)


def _sell_call(quantity: str = "5"):
    return _option_proposal(
        strategy="custom",
        legs=[
            {
                "leg_index": 0,
                "side": "sell",
                "contract_type": "call",
                "strike": OPTION_STRIKE,
                "expiry": OPTION_EXPIRY,
                "quantity": quantity,
                "limit_price": "1.85",
                "order_type": "limit",
            }
        ],
    )


def _closing_sale():
    return make_proposal(
        intent="close",
        strategy="long_equity",
        legs=[
            {
                "leg_index": 0,
                "side": "sell",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ],
    )


def _pending(agent_id: str, symbol: str = "AAPL", seconds_ago: int = 40, direction: str = "long"):
    return {
        "agent_id": agent_id,
        "symbol": symbol,
        "direction": direction,
        "notional": "1000",
        "proposed_at": format_ts(FIXED_NOW - timedelta(seconds=seconds_ago)),
        "model_provider": "featherless",
    }


# ------------------------------------------------------------------------------------------------
# response_level_gate (R-GRAD)
# ------------------------------------------------------------------------------------------------
def test_response_level_zero_is_a_no_op(institutional_context_for, check_of):
    policy = make_institutional_policy()
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy, response_level=0), policy)
    assert evaluation.verdict == "PASS"
    assert check_of(evaluation, "response_level_gate").detail == "response level 0"


@pytest.mark.parametrize(("level", "expected"), [(1, "7"), (2, "5"), (3, "2")])
def test_levels_one_to_three_scale_new_risk_by_the_ladder_multiplier(
    level, expected, institutional_context_for, codes
):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, response_level=level)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REDUCE"
    assert evaluation.recommended_quantity == expected
    assert "SIZE_REDUCED_TO_POLICY_CAP" in codes(evaluation)


@pytest.mark.parametrize("level", [4, 5])
def test_levels_four_and_five_halt_everything_including_closes(level, institutional_context_for, codes):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, response_level=level)
    for proposal in (_proposal(), _closing_sale()):
        evaluation = risk.evaluate(proposal, context, policy)
        assert evaluation.verdict == "REJECT"
        assert "RESPONSE_LEVEL_HALT" in codes(evaluation)


def test_closing_a_position_is_exempt_from_levels_one_to_three(institutional_context_for, check_of):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, response_level=3)
    evaluation = risk.evaluate(_closing_sale(), context, policy)
    check = check_of(evaluation, "response_level_gate")
    assert check.passed is True
    assert evaluation.recommended_quantity == "10"


def test_an_adjustment_that_lowers_exposure_is_not_scaled_by_the_ladder(
    institutional_context_for, check_of
):
    """Levels 1-3 restrain NEW risk (Addendum 1 C); an adjust that reduces exposure adds none."""
    policy = make_institutional_policy()
    context = institutional_context_for(policy, response_level=2)
    reducing = make_proposal(
        intent="adjust",
        invalidation=INVALIDATION,
        legs=[
            {
                "leg_index": 0,
                "side": "sell",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ],
    )
    evaluation = risk.evaluate(reducing, context, policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.recommended_quantity == "10"
    assert check_of(evaluation, "response_level_gate").passed is True


def test_a_level_with_new_risk_forbidden_rejects_an_opening_order(institutional_context_for, codes):
    ladder = make_institutional_policy().response_ladder.model_dump(mode="json")
    ladder["levels"][0]["new_risk_allowed"] = False
    policy = make_institutional_policy(response_ladder=ladder)
    context = institutional_context_for(policy, response_level=1)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "RESPONSE_LEVEL_RESTRICTS_NEW_RISK" in codes(evaluation)


def test_a_raised_level_with_no_ladder_entry_refuses_new_risk(context_for, codes):
    policy = make_policy()
    assert policy.response_ladder is None
    evaluation = risk.evaluate(make_proposal(), context_for(policy, response_level=2), policy)
    assert evaluation.verdict == "REJECT"
    assert "RESPONSE_LEVEL_RESTRICTS_NEW_RISK" in codes(evaluation)


def test_the_response_level_gate_cannot_be_disabled():
    with pytest.raises(Exception):  # noqa: B017 - the contract raises; which type is L0's business
        make_policy(checks={"response_level_gate": {"enabled": False, "severity": "blocking"}})


# ------------------------------------------------------------------------------------------------
# agent_budget
# ------------------------------------------------------------------------------------------------
def _budget(**overrides):
    base = {
        "max_daily_notional": "50000",
        "max_daily_orders": 20,
        "max_open_positions": 10,
        "allowed_symbols": ["AAPL", "MSFT", "SPY"],
        "active_hours_utc": ["13:30", "20:00"],
    }
    base.update(overrides)
    return make_institutional_policy(agent_budgets={AGENT_ID: base})


def test_agent_budget_passes_inside_every_limit(institutional_context_for, check_of):
    policy = _budget()
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy), policy)
    assert check_of(evaluation, "agent_budget").passed is True


def test_agent_budget_passes_when_no_budget_is_configured_for_the_agent(context_for, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    check = check_of(evaluation, "agent_budget")
    assert check.passed is True and AGENT_ID in check.detail


def test_agent_budget_refuses_a_symbol_outside_the_allow_list(institutional_context_for, codes):
    policy = _budget(allowed_symbols=["MSFT"])
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "AGENT_SYMBOL_NOT_ALLOWED" in codes(evaluation)


def test_agent_budget_refuses_an_order_outside_active_hours(institutional_context_for, codes):
    policy = _budget(active_hours_utc=["13:30", "17:00"])  # the fixture clock is 17:40 UTC
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy), policy)
    assert "AGENT_OUTSIDE_ACTIVE_HOURS" in codes(evaluation)


def test_agent_budget_counts_this_order_against_the_daily_order_count(institutional_context_for, codes):
    at_limit = _budget(max_daily_orders=4)  # three used, this is the fourth
    over = _budget(max_daily_orders=3)
    context_at = institutional_context_for(at_limit)
    assert risk.evaluate(_proposal(), context_at, at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "AGENT_DAILY_ORDERS_EXCEEDED" in codes(evaluation)


def test_agent_budget_counts_this_position_against_the_open_position_limit(
    institutional_context_for, codes
):
    at_limit = _budget(max_open_positions=2)  # one open, this would be the second
    over = _budget(max_open_positions=1)
    assert risk.evaluate(_proposal(), institutional_context_for(at_limit), at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "AGENT_OPEN_POSITIONS_EXCEEDED" in codes(evaluation)


def test_agent_budget_caps_the_remaining_daily_notional(institutional_context_for, codes, check_of):
    at_limit = _budget(max_daily_notional="14685")  # 12400 used + 2285 this order
    over = _budget(max_daily_notional="14684")
    assert risk.evaluate(_proposal(), institutional_context_for(at_limit), at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "AGENT_DAILY_NOTIONAL_EXCEEDED" in codes(evaluation)
    assert check_of(evaluation, "agent_budget").recommended_quantity == "0"


def test_agent_budget_blocks_when_the_agent_state_is_missing(institutional_context_for, codes, check_of):
    policy = _budget()
    context = institutional_context_for(policy, agent_state=None)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "AGENT_STATE_MISSING" in codes(evaluation)
    assert check_of(evaluation, "agent_budget").severity == "blocking"
    assert evaluation.data_complete is False


def test_agent_budget_needs_no_state_when_no_stateful_limit_is_set(institutional_context_for, check_of):
    policy = _budget(max_daily_notional=None, max_daily_orders=None, max_open_positions=None)
    context = institutional_context_for(policy, agent_state=None)
    assert check_of(risk.evaluate(_proposal(), context, policy), "agent_budget").passed is True


# ------------------------------------------------------------------------------------------------
# invalidation_defined / reward_risk / risk_per_trade (R-TRADE, R-KELLY)
# ------------------------------------------------------------------------------------------------
def test_invalidation_is_required_when_the_policy_says_so(institutional_context_for, codes):
    policy = make_institutional_policy()
    evaluation = risk.evaluate(make_proposal(), institutional_context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "INVALIDATION_MISSING" in codes(evaluation)


def test_invalidation_is_not_required_to_close(institutional_context_for, check_of):
    policy = make_institutional_policy()
    evaluation = risk.evaluate(_closing_sale(), institutional_context_for(policy), policy)
    assert check_of(evaluation, "invalidation_defined").passed is True


def test_invalidation_present_passes(institutional_context_for, check_of):
    policy = make_institutional_policy()
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy), policy)
    assert check_of(evaluation, "invalidation_defined").passed is True


def _trade(**overrides):
    base = {
        "max_risk_per_trade_pct": "0.01",
        "min_reward_risk": "2",
        "require_invalidation": True,
        "confidence_haircut": "0.25",
        "kelly_fraction_cap": "0.25",
    }
    base.update(overrides)
    return make_institutional_policy(trade=base)


def test_reward_risk_passes_at_exactly_the_minimum(institutional_context_for, check_of):
    """Entry 228.5, invalidation 224 (risk 4.5), target 237.5 (reward 9): exactly 2:1."""
    policy = _trade()
    proposal = _proposal(invalidation={"level": "224", "direction": "below", "target": "237.5"})
    check = check_of(risk.evaluate(proposal, institutional_context_for(policy), policy), "reward_risk")
    assert (check.passed, check.actual, check.threshold) == (True, "2", "2")


def test_reward_risk_fails_just_below_the_minimum(institutional_context_for, codes):
    policy = _trade()
    proposal = _proposal(invalidation={"level": "224", "direction": "below", "target": "237.49"})
    evaluation = risk.evaluate(proposal, institutional_context_for(policy), policy)
    assert evaluation.verdict == "REJECT"
    assert "REWARD_RISK_BELOW_MINIMUM" in codes(evaluation)


def test_reward_risk_refuses_to_guess_without_a_target(institutional_context_for, codes, check_of):
    policy = _trade()
    proposal = _proposal(invalidation={"level": "224", "direction": "below", "target": None})
    evaluation = risk.evaluate(proposal, institutional_context_for(policy), policy)
    check = check_of(evaluation, "reward_risk")
    assert (check.passed, check.reason_code) == (False, "INVALIDATION_MISSING")
    assert "INVALIDATION_MISSING" in codes(evaluation)


def test_risk_per_trade_measures_capital_at_risk_to_the_invalidation_level(
    institutional_context_for, check_of
):
    """10 shares risking 4.5 each is 45 of capital; the budget is 1% of 100,000."""
    policy = _trade()
    check = check_of(
        risk.evaluate(_proposal(), institutional_context_for(policy), policy), "risk_per_trade"
    )
    assert (check.passed, check.actual, check.threshold) == (True, "45", "1000")


def test_risk_per_trade_passes_at_exactly_the_budget_and_fails_one_step_over(
    institutional_context_for, codes
):
    at_limit = _trade(max_risk_per_trade_pct="0.00045")  # budget 45, at risk 45
    over = _trade(max_risk_per_trade_pct="0.00044")
    assert risk.evaluate(_proposal(), institutional_context_for(at_limit), at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "RISK_PER_TRADE_EXCEEDED" in codes(evaluation)


def test_without_an_invalidation_level_the_whole_position_is_the_risk(institutional_context_for, check_of):
    policy = _trade(require_invalidation=False, min_reward_risk=None)
    check = check_of(
        risk.evaluate(make_proposal(), institutional_context_for(policy), policy), "risk_per_trade"
    )
    assert (check.passed, check.actual) == (False, "2285")


def test_a_stated_confidence_can_only_shrink_the_budget(institutional_context_for):
    """A claim is never authority: with the haircut applied the same order no longer fits."""
    policy = _trade(max_risk_per_trade_pct="0.0005", confidence_haircut="0.25")  # budget 50, at risk 45
    context = institutional_context_for(policy)
    assert risk.evaluate(_proposal(), context, policy).verdict == "PASS"
    claimed = _proposal(confidence="0.5")  # 0.5 - 0.25 = a quarter of the budget: 12.5
    assert risk.evaluate(claimed, context, policy).verdict == "REJECT"


def test_a_confidence_claim_does_nothing_when_no_haircut_is_configured(institutional_context_for):
    policy = _trade(max_risk_per_trade_pct="0.0005", confidence_haircut="0")
    context = institutional_context_for(policy)
    plain = risk.evaluate(_proposal(), context, policy)
    claimed = risk.evaluate(_proposal(confidence="0.5"), context, policy)
    assert plain.verdict == claimed.verdict == "PASS"


def test_risk_per_trade_blocks_without_a_portfolio(institutional_context_for, check_of):
    policy = _trade()
    context = institutional_context_for(policy, portfolio_snapshot=None)
    check = check_of(risk.evaluate(_proposal(), context, policy), "risk_per_trade")
    assert (check.passed, check.reason_code) == (False, "PORTFOLIO_STATE_MISSING")


# ------------------------------------------------------------------------------------------------
# drawdown_size_scaling / consecutive_loss_review (R-ERG)
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("drawdown", "expected"), [("0.0476", "10"), ("0.05", "10"), ("0.0999", "10")]
)
def test_size_is_untouched_until_a_scaling_step_is_reached(
    drawdown, expected, institutional_context_for
):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, path_state=make_path_state(current_drawdown_pct=drawdown))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.recommended_quantity == expected
    assert evaluation.verdict == "PASS"


@pytest.mark.parametrize(("drawdown", "expected"), [("0.1", "5"), ("0.14", "5"), ("0.15", "2")])
def test_size_scales_down_with_drawdown(drawdown, expected, institutional_context_for, codes):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, path_state=make_path_state(current_drawdown_pct=drawdown))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REDUCE"
    assert evaluation.recommended_quantity == expected
    assert "SIZE_SCALED_BY_DRAWDOWN" in codes(evaluation)


def test_drawdown_scaling_is_a_warning_not_a_refusal(institutional_context_for, check_of):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, path_state=make_path_state(current_drawdown_pct="0.1"))
    check = check_of(risk.evaluate(_proposal(), context, policy), "drawdown_size_scaling")
    assert (check.passed, check.severity, check.recommended_quantity) == (False, "warning", "5")


def test_drawdown_scaling_blocks_without_path_state(institutional_context_for, codes, check_of):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, path_state=None)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "PATH_STATE_MISSING" in codes(evaluation)
    assert check_of(evaluation, "drawdown_size_scaling").severity == "blocking"
    assert evaluation.verdict == "REJECT"


def test_a_losing_streak_stops_at_the_review_threshold(institutional_context_for, codes):
    policy = make_institutional_policy()
    below = institutional_context_for(policy, path_state=make_path_state(consecutive_losses=4))
    assert risk.evaluate(_proposal(), below, policy).verdict == "PASS"
    at_limit = institutional_context_for(policy, path_state=make_path_state(consecutive_losses=5))
    evaluation = risk.evaluate(_proposal(), at_limit, policy)
    assert evaluation.verdict == "REJECT"
    assert "CONSECUTIVE_LOSS_REVIEW" in codes(evaluation)


def test_days_under_water_has_its_own_code(institutional_context_for, codes):
    policy = make_institutional_policy()
    below = institutional_context_for(policy, path_state=make_path_state(days_under_water=29))
    assert risk.evaluate(_proposal(), below, policy).verdict == "PASS"
    context = institutional_context_for(policy, path_state=make_path_state(days_under_water=30))
    assert "DAYS_UNDER_WATER_EXCEEDED" in codes(risk.evaluate(_proposal(), context, policy))


# ------------------------------------------------------------------------------------------------
# aggregate / correlated intent / concentration by provider and source (R-AGG)
# ------------------------------------------------------------------------------------------------
def _aggregate(**overrides):
    base = {
        "max_portfolio_exposure_pct": "0.6",
        "max_correlated_intent_agents": 3,
        "correlated_intent_window_seconds": 300,
        "max_exposure_per_model_provider_pct": "0.4",
        "max_exposure_per_signal_source_pct": "0.4",
        "crowding_score_threshold": "0.8",
        "max_days_to_liquidate_book": "3",
    }
    base.update(overrides)
    return make_institutional_policy(aggregate=base)


def test_aggregate_exposure_passes_at_exactly_the_limit(institutional_context_for, check_of):
    policy = _aggregate(max_portfolio_exposure_pct="0.2289")
    check = check_of(
        risk.evaluate(_proposal(), institutional_context_for(policy), policy), "aggregate_exposure"
    )
    assert (check.passed, check.actual) == (True, "0.2289")


def test_aggregate_exposure_fails_one_step_over(institutional_context_for, codes):
    policy = _aggregate(max_portfolio_exposure_pct="0.2288")
    evaluation = risk.evaluate(_proposal(), institutional_context_for(policy), policy)
    assert "AGGREGATE_EXPOSURE_EXCEEDED" in codes(evaluation)


def test_aggregate_checks_block_without_aggregate_state(institutional_context_for, codes):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, aggregate_state=None)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "AGGREGATE_STATE_MISSING" in codes(evaluation)
    assert evaluation.data_complete is False


def test_correlated_intent_counts_this_agent_and_the_pending_ones(institutional_context_for, check_of):
    policy = _aggregate(max_correlated_intent_agents=3)
    two_others = make_aggregate_state(pending_intents=[_pending("agent-b"), _pending("agent-c")])
    context = institutional_context_for(policy, aggregate_state=two_others)
    check = check_of(risk.evaluate(_proposal(), context, policy), "correlated_intent")
    assert (check.passed, check.actual, check.threshold) == (True, "3", "3")


def test_correlated_intent_rejects_one_agent_too_many(institutional_context_for, codes):
    policy = _aggregate(max_correlated_intent_agents=3)
    three_others = make_aggregate_state(
        pending_intents=[_pending("agent-b"), _pending("agent-c"), _pending("agent-d")]
    )
    context = institutional_context_for(policy, aggregate_state=three_others)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "CORRELATED_INTENT_DETECTED" in codes(evaluation)


def test_correlated_intent_ignores_intents_outside_the_window_or_the_other_way(
    institutional_context_for, check_of
):
    policy = _aggregate(max_correlated_intent_agents=1)
    state = make_aggregate_state(
        pending_intents=[
            _pending("agent-b", seconds_ago=301),
            _pending("agent-c", direction="short"),
            _pending("agent-d", symbol="MSFT"),
        ]
    )
    context = institutional_context_for(policy, aggregate_state=state)
    check = check_of(risk.evaluate(_proposal(), context, policy), "correlated_intent")
    assert (check.passed, check.actual) == (True, "1")


def test_model_provider_concentration_uses_the_proposal_model_provider(
    institutional_context_for, codes
):
    at_limit = _aggregate(max_exposure_per_model_provider_pct="0.2289")
    over = _aggregate(max_exposure_per_model_provider_pct="0.2288")
    assert risk.evaluate(_proposal(), institutional_context_for(at_limit), at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "MODEL_PROVIDER_CONCENTRATION_EXCEEDED" in codes(evaluation)


def test_signal_source_concentration_uses_the_declared_sources(institutional_context_for, codes, check_of):
    over = _aggregate(max_exposure_per_signal_source_pct="0.2288")
    sourced = _proposal(signal_sources=["vendor:polygon"])
    evaluation = risk.evaluate(sourced, institutional_context_for(over), over)
    assert "SIGNAL_SOURCE_CONCENTRATION_EXCEEDED" in codes(evaluation)
    unsourced = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert check_of(unsourced, "signal_source_concentration").passed is True


# ------------------------------------------------------------------------------------------------
# liquidity (R-LIQ)
# ------------------------------------------------------------------------------------------------
def _liquidity(**overrides):
    base = {
        "max_pct_of_adv": "0.01",
        "max_option_spread_pct": "0.1",
        "min_option_open_interest": 100,
        "max_estimated_impact_bps": "25",
    }
    base.update(overrides)
    return make_institutional_policy(liquidity=base)


def _quotes(**aapl):
    snapshot = make_market_snapshot().model_dump(mode="json")
    snapshot["quotes"]["AAPL"].update(aapl)
    return make_market_snapshot(quotes=snapshot["quotes"])


def _option_quotes(**leg):
    snapshot = make_market_snapshot().model_dump(mode="json")
    snapshot["option_quotes"][OPTION_OCC].update(leg)
    return make_market_snapshot(option_quotes=snapshot["option_quotes"])


def test_adv_participation_passes_at_exactly_the_limit(institutional_context_for, check_of):
    policy = _liquidity(max_pct_of_adv="0.01")
    context = institutional_context_for(policy, market_snapshot=_quotes(adv="1000"))
    check = check_of(risk.evaluate(_proposal(), context, policy), "liquidity_adv")
    assert (check.passed, check.actual) == (True, "0.01")


def test_adv_participation_fails_one_share_of_volume_short(institutional_context_for, codes, check_of):
    policy = _liquidity(max_pct_of_adv="0.01")
    context = institutional_context_for(policy, market_snapshot=_quotes(adv="999"))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "ADV_PARTICIPATION_EXCEEDED" in codes(evaluation)
    assert check_of(evaluation, "liquidity_adv").recommended_quantity == "0"


def test_missing_adv_blocks_rather_than_assuming_liquidity(institutional_context_for, codes, check_of):
    policy = _liquidity()
    context = institutional_context_for(policy, market_snapshot=_quotes(adv=None))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "LIQUIDITY_DATA_MISSING" in codes(evaluation)
    assert check_of(evaluation, "liquidity_adv").severity == "blocking"
    assert evaluation.data_complete is False


def test_estimated_impact_is_half_the_quoted_spread_in_basis_points(institutional_context_for, check_of):
    at_limit = _liquidity(max_estimated_impact_bps="25")
    context = institutional_context_for(at_limit, market_snapshot=_quotes(spread_pct="0.005"))
    check = check_of(risk.evaluate(_proposal(), context, at_limit), "liquidity_adv")
    assert check.passed is True


def test_estimated_impact_beyond_the_limit_is_refused(institutional_context_for, codes):
    policy = _liquidity(max_estimated_impact_bps="25")
    context = institutional_context_for(policy, market_snapshot=_quotes(spread_pct="0.006"))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "ESTIMATED_IMPACT_EXCEEDED" in codes(evaluation)


def test_option_liquidity_passes_at_exactly_the_spread_and_open_interest_limits(
    institutional_context_for, check_of
):
    policy = _liquidity(max_option_spread_pct="0.027", min_option_open_interest=4200)
    check = check_of(
        risk.evaluate(_option_proposal(), institutional_context_for(policy), policy), "option_liquidity"
    )
    assert check.passed is True


def test_option_liquidity_refuses_a_spread_one_step_too_wide(institutional_context_for, codes):
    policy = _liquidity(max_option_spread_pct="0.026")
    evaluation = risk.evaluate(_option_proposal(), institutional_context_for(policy), policy)
    assert "OPTION_SPREAD_TOO_WIDE" in codes(evaluation)


def test_option_liquidity_refuses_open_interest_one_contract_too_thin(institutional_context_for, codes):
    policy = _liquidity(min_option_open_interest=4201)
    evaluation = risk.evaluate(_option_proposal(), institutional_context_for(policy), policy)
    assert "OPTION_OPEN_INTEREST_TOO_LOW" in codes(evaluation)


def test_option_liquidity_blocks_when_open_interest_is_unknown(institutional_context_for, check_of):
    policy = _liquidity(min_option_open_interest=100)
    context = institutional_context_for(policy, market_snapshot=_option_quotes(open_interest=None))
    check = check_of(risk.evaluate(_option_proposal(), context, policy), "option_liquidity")
    assert (check.passed, check.reason_code) == (False, "LIQUIDITY_DATA_MISSING")


# ------------------------------------------------------------------------------------------------
# time controls (R-TIME)
# ------------------------------------------------------------------------------------------------
def _time(**overrides):
    base = {
        "earnings_blackout_days_before": 2,
        "earnings_blackout_days_after": 1,
        "macro_event_blackout_minutes": 30,
        "no_trade_first_minutes": 15,
        "no_trade_last_minutes": 10,
        "max_overnight_exposure_pct": "0.5",
    }
    base.update(overrides)
    return make_institutional_policy(time=base)


def test_earnings_blackout_starts_exactly_at_the_configured_distance(institutional_context_for, codes):
    policy = _time(earnings_blackout_days_before=2)
    outside = institutional_context_for(policy, calendar=make_calendar(earnings_within_days={"AAPL": 3}))
    assert risk.evaluate(_proposal(), outside, policy).verdict == "PASS"
    inside = institutional_context_for(policy, calendar=make_calendar(earnings_within_days={"AAPL": 2}))
    evaluation = risk.evaluate(_proposal(), inside, policy)
    assert evaluation.verdict == "REJECT"
    assert "EARNINGS_BLACKOUT" in codes(evaluation)


def test_macro_blackout_applies_up_to_and_including_the_window(institutional_context_for, codes):
    policy = _time(macro_event_blackout_minutes=30)
    outside = institutional_context_for(policy, calendar=make_calendar(macro_event_within_minutes=31))
    assert risk.evaluate(_proposal(), outside, policy).verdict == "PASS"
    inside = institutional_context_for(policy, calendar=make_calendar(macro_event_within_minutes=30))
    assert "MACRO_EVENT_BLACKOUT" in codes(risk.evaluate(_proposal(), inside, policy))


def test_time_checks_block_without_a_calendar(institutional_context_for, codes, check_of):
    policy = make_institutional_policy()
    context = institutional_context_for(policy, calendar=None)
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert "CALENDAR_MISSING" in codes(evaluation)
    assert check_of(evaluation, "time_blackout").severity == "blocking"
    assert evaluation.data_complete is False


@pytest.mark.parametrize("session", ["pre", "after", "closed"])
def test_orders_outside_the_regular_session_are_restricted(session, institutional_context_for, codes):
    policy = _time()
    context = institutional_context_for(policy, calendar=make_calendar(session=session))
    evaluation = risk.evaluate(_proposal(), context, policy)
    assert evaluation.verdict == "REJECT"
    assert "SESSION_WINDOW_RESTRICTED" in codes(evaluation)


def test_the_opening_window_closes_at_exactly_the_configured_minute(institutional_context_for, codes):
    policy = _time(no_trade_first_minutes=15)
    inside = institutional_context_for(policy, calendar=make_calendar(minutes_since_open=14))
    assert "SESSION_WINDOW_RESTRICTED" in codes(risk.evaluate(_proposal(), inside, policy))
    at_edge = institutional_context_for(policy, calendar=make_calendar(minutes_since_open=15))
    assert risk.evaluate(_proposal(), at_edge, policy).verdict == "PASS"


def test_the_closing_window_opens_at_exactly_the_configured_minute(institutional_context_for, codes):
    policy = _time(no_trade_last_minutes=10)
    inside = institutional_context_for(policy, calendar=make_calendar(minutes_to_close=9))
    assert "SESSION_WINDOW_RESTRICTED" in codes(risk.evaluate(_proposal(), inside, policy))
    at_edge = institutional_context_for(policy, calendar=make_calendar(minutes_to_close=10))
    assert risk.evaluate(_proposal(), at_edge, policy).verdict == "PASS"


def test_an_unknown_minute_to_close_blocks_when_the_window_is_configured(
    institutional_context_for, check_of
):
    policy = _time(no_trade_last_minutes=10)
    context = institutional_context_for(policy, calendar=make_calendar(minutes_to_close=None))
    check = check_of(risk.evaluate(_proposal(), context, policy), "session_window")
    assert (check.passed, check.reason_code) == (False, "CALENDAR_MISSING")


def test_overnight_exposure_is_capped_when_the_policy_says_so(institutional_context_for, codes):
    at_limit = _time(max_overnight_exposure_pct="0.2289")
    over = _time(max_overnight_exposure_pct="0.2288")
    assert risk.evaluate(_proposal(), institutional_context_for(at_limit), at_limit).verdict == "PASS"
    evaluation = risk.evaluate(_proposal(), institutional_context_for(over), over)
    assert "OVERNIGHT_EXPOSURE_EXCEEDED" in codes(evaluation)


# ------------------------------------------------------------------------------------------------
# short gamma / short vega (R-OPT-1)
# ------------------------------------------------------------------------------------------------
def _short_options_policy(**options):
    base = {
        "max_portfolio_delta": "5000",
        "max_portfolio_gamma": "5000",
        "max_portfolio_vega": "5000",
        "min_days_to_expiry": 7,
        "max_days_to_expiry": 45,
        "max_short_gamma": "10.5",
        "max_short_vega": "71",
    }
    base.update(options)
    return make_policy(options=base)


def test_short_gamma_passes_at_exactly_the_limit(context_for, check_of):
    policy = _short_options_policy(max_short_gamma="10.5")
    check = check_of(
        risk.evaluate(_sell_call(), context_for(policy), policy), "options_short_gamma_limit"
    )
    assert (check.passed, check.actual, check.threshold) == (True, "10.5", "10.5")


def test_short_gamma_fails_one_step_over(context_for, codes):
    policy = _short_options_policy(max_short_gamma="10.4")
    evaluation = risk.evaluate(_sell_call(), context_for(policy), policy)
    assert "OPTIONS_SHORT_GAMMA_LIMIT_EXCEEDED" in codes(evaluation)


def test_short_vega_fails_one_step_over(context_for, codes):
    policy = _short_options_policy(max_short_vega="70")
    evaluation = risk.evaluate(_sell_call(), context_for(policy), policy)
    assert "OPTIONS_SHORT_VEGA_LIMIT_EXCEEDED" in codes(evaluation)


def test_a_long_option_never_breaches_a_short_greek_limit(context_for, check_of):
    policy = _short_options_policy(max_short_gamma="0", max_short_vega="0")
    evaluation = risk.evaluate(_option_proposal(), context_for(policy), policy)
    assert check_of(evaluation, "options_short_gamma_limit").passed is True
    assert check_of(evaluation, "options_short_vega_limit").passed is True


def test_short_greeks_add_to_what_the_book_is_already_short(context_for, check_of):
    policy = _short_options_policy(max_short_gamma="30")
    greeks = {"delta": "50", "gamma": "-20", "vega": "0", "short_gamma": "-20"}
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(greeks=greeks))
    check = check_of(risk.evaluate(_sell_call(), context, policy), "options_short_gamma_limit")
    assert (check.passed, check.actual) == (False, "30.5")


def test_short_greek_limits_block_without_greeks(context_for, check_of):
    policy = _short_options_policy()
    context = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(greeks=None))
    check = check_of(risk.evaluate(_sell_call(), context, policy), "options_short_gamma_limit")
    assert (check.passed, check.reason_code) == (False, "GREEKS_MISSING")


def test_an_agent_state_snapshot_is_only_needed_when_a_budget_exists(context_for, check_of):
    """A policy with no agent_budgets never demands agent state, whatever the context carries."""
    policy = make_policy()
    context = context_for(policy, agent_state=make_agent_state())
    assert check_of(risk.evaluate(make_proposal(), context, policy), "agent_budget").passed is True
