"""Purity and determinism: the properties Hard Rule A1 (replay) and E1 (reasoning is audit-only) rest on.

Same inputs must produce byte-identical output, whatever order the inputs happened to be spelled in and
whatever free text the agent attached. These are properties over many inputs, not single cases: a check
that reads a mapping in insertion order or splices a string into a decision would pass a happy-path unit
test and fail here.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizan import risk
from mizan.contracts.canonical import canonical_json
from tests.fixtures import (
    injection_reasoning,
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
DETERMINISM = settings(
    max_examples=120,
    deadline=None,
    database=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@DETERMINISM
@given(
    quantity=st.integers(min_value=1, max_value=400),
    buying_power=st.integers(min_value=0, max_value=500000),
    equity=st.integers(min_value=1000, max_value=900000),
    response_level=st.integers(min_value=0, max_value=5),
    drawdown_thousandths=st.integers(min_value=0, max_value=999),
    consecutive_losses=st.integers(min_value=0, max_value=9),
    with_invalidation=st.booleans(),
)
def test_evaluate_is_deterministic_for_any_input(
    quantity,
    buying_power,
    equity,
    response_level,
    drawdown_thousandths,
    consecutive_losses,
    with_invalidation,
    institutional_context_for,
):
    policy = make_institutional_policy()
    proposal = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": str(quantity),
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ],
        invalidation=INVALIDATION if with_invalidation else None,
    )
    context = institutional_context_for(
        policy,
        portfolio_snapshot=make_portfolio_snapshot(
            buying_power=str(buying_power), equity=str(equity), peak_equity=str(equity + 1)
        ),
        path_state=make_path_state(
            current_drawdown_pct=f"0.{drawdown_thousandths:03d}", consecutive_losses=consecutive_losses
        ),
        response_level=response_level,
    )
    first = risk.evaluate(proposal, context, policy)
    second = risk.evaluate(proposal, context, policy)
    assert canonical_json(first) == canonical_json(second)
    assert first.evaluation_id == second.evaluation_id
    assert first.verdict in {"PASS", "REDUCE", "REJECT"}


@DETERMINISM
@given(seed=st.integers(min_value=0, max_value=10**6))
def test_repeated_evaluation_never_drifts(seed, context_for):
    policy = make_policy()
    proposal = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": str(seed % 50 + 1),
                "limit_price": "228.50",
                "order_type": "limit",
            }
        ]
    )
    context = context_for(policy)
    baseline = canonical_json(risk.evaluate(proposal, context, policy))
    for _ in range(3):
        assert canonical_json(risk.evaluate(proposal, context, policy)) == baseline


def _reversed_market_snapshot():
    """The same market snapshot with every mapping built in the opposite insertion order."""
    dumped = make_market_snapshot().model_dump(mode="json")
    return make_market_snapshot(
        quotes={key: dumped["quotes"][key] for key in reversed(list(dumped["quotes"]))},
        option_quotes={key: dumped["option_quotes"][key] for key in reversed(list(dumped["option_quotes"]))},
        sectors={key: dumped["sectors"][key] for key in reversed(list(dumped["sectors"]))},
    )


def _reversed_aggregate_state():
    dumped = make_aggregate_state().model_dump(mode="json")
    reverse = {
        field: {key: dumped[field][key] for key in reversed(list(dumped[field]))}
        for field in (
            "exposure_by_agent",
            "exposure_by_model_provider",
            "exposure_by_signal_source",
            "exposure_by_sector",
        )
    }
    return make_aggregate_state(**reverse)


def test_reordering_snapshot_keys_does_not_change_the_evaluation(institutional_context_for):
    policy = make_institutional_policy()
    proposal = make_proposal(invalidation=INVALIDATION)
    plain = institutional_context_for(policy)
    reordered = institutional_context_for(
        policy,
        market_snapshot=_reversed_market_snapshot(),
        aggregate_state=_reversed_aggregate_state(),
        calendar=make_calendar(earnings_within_days={"MSFT": 40, "AAPL": 28}),
    )
    assert canonical_json(risk.evaluate(proposal, plain, policy)) == canonical_json(
        risk.evaluate(proposal, reordered, policy)
    )


def test_position_order_does_not_change_the_evaluation(context_for):
    policy = make_policy()
    positions = [
        {
            "symbol": "MSFT",
            "asset_class": "equity",
            "quantity": "50",
            "market_value": "20605",
            "sector": "Technology",
            "occ_symbol": None,
            "delta": "50",
            "gamma": "0",
            "vega": "0",
        },
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "quantity": "10",
            "market_value": "2285",
            "sector": "Technology",
            "occ_symbol": None,
            "delta": "10",
            "gamma": "0",
            "vega": "0",
        },
    ]
    forward = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(positions=positions))
    backward = context_for(
        policy, portfolio_snapshot=make_portfolio_snapshot(positions=list(reversed(positions)))
    )
    assert canonical_json(risk.evaluate(make_proposal(), forward, policy)) == canonical_json(
        risk.evaluate(make_proposal(), backward, policy)
    )


def test_the_evaluation_is_identical_with_and_without_injected_reasoning(context_for):
    policy = make_policy()
    context = context_for(policy)
    clean = make_proposal(reasoning="")
    poisoned = make_proposal(reasoning=injection_reasoning())
    assert clean.proposal_id == poisoned.proposal_id
    clean_evaluation = risk.evaluate(clean, context, policy)
    poisoned_evaluation = risk.evaluate(poisoned, context, policy)
    assert canonical_json(clean_evaluation) == canonical_json(poisoned_evaluation)
    assert injection_reasoning() not in canonical_json(poisoned_evaluation)


def test_injected_reasoning_cannot_turn_a_rejection_into_an_approval(context_for, codes):
    policy = make_policy(restricted={"symbols": ["AAPL"], "strategies": []})
    context = context_for(policy)
    poisoned = make_proposal(reasoning=injection_reasoning())
    evaluation = risk.evaluate(poisoned, context, policy)
    assert evaluation.verdict == "REJECT"
    assert "RESTRICTED_SYMBOL" in codes(evaluation)


@DETERMINISM
@given(text=st.text(max_size=200))
def test_no_free_text_from_the_agent_changes_the_verdict(text, context_for):
    policy = make_policy()
    context = context_for(policy)
    baseline = canonical_json(risk.evaluate(make_proposal(reasoning=""), context, policy))
    assert canonical_json(risk.evaluate(make_proposal(reasoning=text), context, policy)) == baseline


def test_option_evaluations_are_deterministic_too(context_for):
    policy = make_policy()
    context = context_for(policy)
    proposal = make_option_proposal()
    first = risk.evaluate(proposal, context, policy)
    second = risk.evaluate(proposal, context, policy)
    assert canonical_json(first) == canonical_json(second)


def test_no_binary_fraction_ever_appears_in_an_evaluation(institutional_context_for):
    """Every money, quantity and ratio in the output is a decimal STRING; only counts are numbers."""
    policy = make_institutional_policy()
    proposal = make_proposal(invalidation=INVALIDATION)
    evaluation = risk.evaluate(proposal, institutional_context_for(policy), policy)

    offenders: list[str] = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif value is not None and not isinstance(value, (str, bool, int)):
            offenders.append(f"{path}: {type(value).__name__}")

    walk(evaluation.model_dump(mode="json"), "$")
    assert not offenders, offenders
