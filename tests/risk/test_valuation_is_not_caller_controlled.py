"""Security property (findings F-1 / F-2): nothing the proposing agent asserts is authority.

In the legacy engine the caller-supplied ``estimated_price`` was the only valuation price, so 1,000
shares at a claimed one cent passed every notional rule and was approved and authorized end to end.
The new engine values the order from ``context.market_snapshot`` only. These tests state that as a
property of the engine rather than as a happy-path unit case, including a structural one: the whole
``mizan/risk`` package reads ``limit_price`` in exactly one function, the check whose job is to compare
the agent's limit against the market.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mizan import risk
from mizan.contracts import dec
from tests.fixtures import make_market_snapshot, make_policy, make_portfolio_snapshot, make_proposal

RISK_PACKAGE = Path(risk.__file__).resolve().parent


def _legs(quantity: str, limit_price: str | None, order_type: str = "limit"):
    return [
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": quantity,
            "limit_price": limit_price,
            "order_type": order_type,
        }
    ]


def test_caller_supplied_limit_price_cannot_be_used_as_valuation(context_for, codes):
    """1,000 shares at a claimed $0.01 is a $228,500 order, and is refused as one."""
    policy = make_policy()
    poisoned = make_proposal(legs=_legs("1000", "0.01"))
    assert poisoned.notional_estimate == dec("10")  # what the agent's own numbers claim

    evaluation = risk.evaluate(poisoned, context_for(policy), policy)

    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"
    assert evaluation.original_notional == "228500"  # 1000 x the 228.5 quote, not 1000 x 0.01
    assert {
        "CAPITAL_THRESHOLD_EXCEEDED",
        "INSUFFICIENT_BUYING_POWER",
        "POSITION_LIMIT_EXCEEDED",
    } <= codes(evaluation)


def test_an_absurd_limit_price_is_also_caught_as_an_erroneous_order(context_for, codes, check_of):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(legs=_legs("1", "0.01")), context_for(policy), policy)
    assert "ERRONEOUS_PRICE_DEVIATION" in codes(evaluation)
    check = check_of(evaluation, "erroneous_order")
    assert (check.passed, check.severity) == (False, "blocking")


def test_the_limit_price_never_changes_the_valuation_of_the_same_order(context_for, check_of):
    """Three limits, one quote: capital_threshold sees the same notional every time."""
    policy = make_policy()
    context = context_for(policy)
    actuals = set()
    for limit_price, order_type in (("0.01", "limit"), ("228.50", "limit"), (None, "market")):
        proposal = make_proposal(legs=_legs("10", limit_price, order_type))
        evaluation = risk.evaluate(proposal, context, policy)
        actuals.add(check_of(evaluation, "capital_threshold").actual)
    assert actuals == {"2285"}


def test_a_market_order_is_valued_and_evaluated_like_any_other(context_for):
    policy = make_policy()
    evaluation = risk.evaluate(make_proposal(legs=_legs("10", None, "market")), context_for(policy), policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.original_notional == "2285"


def test_market_risk_inputs_come_from_the_snapshot_not_the_agent(institutional_context_for):
    """The same proposal is refused or allowed purely by what the context's quote says (F-2)."""
    from tests.fixtures import make_institutional_policy

    policy = make_institutional_policy(
        liquidity={
            "max_pct_of_adv": "0.01",
            "max_option_spread_pct": "0.1",
            "min_option_open_interest": 100,
            "max_estimated_impact_bps": "25",
        }
    )
    proposal = make_proposal(invalidation={"level": "224", "direction": "below", "target": "240"})
    snapshot = make_market_snapshot().model_dump(mode="json")

    def with_adv(adv: str):
        quotes = {symbol: dict(quote) for symbol, quote in snapshot["quotes"].items()}
        quotes["AAPL"]["adv"] = adv
        return institutional_context_for(policy, market_snapshot=make_market_snapshot(quotes=quotes))

    assert risk.evaluate(proposal, with_adv("1000"), policy).verdict == "PASS"
    assert risk.evaluate(proposal, with_adv("999"), policy).verdict == "REJECT"


def test_a_richer_portfolio_snapshot_cannot_be_supplied_by_the_proposal(context_for):
    """Portfolio figures come from the context; the proposal has no field that could carry them."""
    proposal_fields = set(type(make_proposal()).model_fields)
    assert not proposal_fields & {"equity", "buying_power", "positions", "portfolio", "adv", "volatility"}
    policy = make_policy()
    rich = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="1000000"))
    poor = context_for(policy, portfolio_snapshot=make_portfolio_snapshot(buying_power="100"))
    assert risk.evaluate(make_proposal(), rich, policy).verdict == "PASS"
    assert risk.evaluate(make_proposal(), poor, policy).verdict == "REJECT"


def test_only_the_erroneous_order_check_reads_the_agent_limit_price():
    """A structural guard: valuation lives in one place, and the limit price is not part of it."""
    readers: set[str] = set()
    for path in sorted(RISK_PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr == "limit_price":
                    readers.add(node.name)
                if isinstance(inner, ast.Constant) and inner.value == "limit_price":
                    readers.add(node.name)
    assert readers == {"erroneous_order"}, readers
