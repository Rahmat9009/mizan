"""The expected-value gate: a credit spread must expect to make money, or it is refused.

The reasoning behind every floor is in ``docs/EV-GATE.md``; this file pins the behaviour those floors
produce. Two things here are worth more than the rest:

* ``test_the_live_position_of_2026_09_03_is_refused`` replays the exact recorded numbers of the SPY
  760/765 put credit spread that was APPROVED and FILLED on the live paper account, and asserts the
  gate refuses it. That is the finding this lane exists to record, and it is pinned so that a future
  loosening of a floor cannot quietly un-refuse it.
* ``test_the_verdict_on_the_live_position_does_not_depend_on_the_floors`` sets every floor to its
  loosest legal value and shows the refusal survives. A gate whose verdict came from a well-chosen
  threshold would flip; this one does not, because the arithmetic itself is against the trade.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mizan import risk
from mizan.contracts import Policy, TradeProposal, dec, occ_symbol_for
from mizan.risk.expected_value import normal_cdf
from tests.fixtures import (
    FIXED_NOW,
    format_ts,
    make_context,
    make_market_snapshot,
    make_option_proposal,
    make_policy,
    make_proposal,
)

# The floors this build ships. Mirrored from policies/options-ev-gated.yaml, and NOT recomputed from
# it: a test that read the policy it is testing would go green if both were loosened together.
FLOORS = {"min_credit_to_width": "0.20", "min_pop": "0.55", "min_ev_to_max_loss": "0.05"}

EXPIRY = "2026-09-25"  # 23 days after FIXED_NOW
AS_OF = format_ts(FIXED_NOW)


def ev_policy(**floors: str) -> Policy:
    """A policy with an ``ev`` section and no ``options`` section, like options-ev-gated.yaml."""
    return make_policy(
        options=None,
        ev={**FLOORS, **floors},
        checks={"expected_value": {"enabled": True, "severity": "blocking"}},
    )


def spread(
    *,
    symbol: str = "SPY",
    short_strike: str,
    long_strike: str,
    contract_type: str = "put",
    short_side: str = "sell",
    long_side: str = "buy",
    quantity: str = "10",
    expiry: str = EXPIRY,
    limit_price: str = "1.00",
    marks: dict[str, str] | None = None,
) -> TradeProposal:
    """A two-leg vertical.

    ``marks`` gives each leg the limit price its own strike trades at, which is what a real proposal
    looks like and what keeps ``erroneous_order`` out of the way of these assertions. The gate itself
    reads the MARKS and never the limit price (see ``test_the_limit_price_cannot_move_the_verdict``),
    so this affects the realism of the fixture and nothing about the verdict under test.
    """
    payload = make_option_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    base = payload["legs"][0]
    payload["symbol"] = symbol
    payload["strategy"] = "bull_put_spread" if contract_type == "put" else "bear_call_spread"
    payload["legs"] = [
        {
            **base,
            "leg_index": 0,
            "side": long_side,
            "contract_type": contract_type,
            "strike": long_strike,
            "expiry": expiry,
            "quantity": quantity,
            "limit_price": (marks or {}).get(long_strike, limit_price),
        },
        {
            **base,
            "leg_index": 1,
            "side": short_side,
            "contract_type": contract_type,
            "strike": short_strike,
            "expiry": expiry,
            "quantity": quantity,
            "limit_price": (marks or {}).get(short_strike, limit_price),
        },
    ]
    return TradeProposal.build(**payload)


def market(
    *,
    symbol: str = "SPY",
    spot: str,
    strikes_marks: dict[str, str],
    contract_type: str = "put",
    expiry: str = EXPIRY,
    iv: str | None = None,
    drop: str | None = None,
):
    """A snapshot carrying the underlying quote and one option quote per strike."""
    quotes = {
        symbol: {
            "symbol": symbol,
            "price": spot,
            "bid": spot,
            "ask": spot,
            "as_of": AS_OF,
            "source": "test:quotes",
            "adv": "55000000",
            "spread_pct": "0.0004",
        }
    }
    option_quotes = {}
    for strike, mark in strikes_marks.items():
        if strike == drop:
            continue
        occ = occ_symbol_for(symbol, contract_type, expiry, strike)
        option_quotes[occ] = {
            "occ_symbol": occ,
            "mark": mark,
            "delta": None,
            "gamma": None,
            "vega": None,
            "theta": None,
            "as_of": AS_OF,
            "source": "test:options",
            "open_interest": 4200,
            "spread_pct": "0.02",
            "iv": iv,
        }
    return make_market_snapshot(
        quotes=quotes, option_quotes=option_quotes, sectors={}, source="test:market"
    )


def evaluate(proposal: TradeProposal, snapshot, policy: Policy):
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref, market_snapshot=snapshot)
    evaluation = risk.evaluate(proposal, context, policy)
    check = next(c for c in evaluation.checks if c.check_id == "expected_value")
    return evaluation, check


# ----------------------------------------------------------------------------------------------
# THE LIVE POSITION. Recorded inputs, recorded numbers, recorded refusal.
# ----------------------------------------------------------------------------------------------
LIVE_SPOT = "773.295"
LIVE_MARKS = {"765": "1.32", "760": "0.75"}  # short 765 put, long 760 put; credit 0.57 on width 5


def live_position() -> tuple[TradeProposal, object]:
    return (
        spread(short_strike="765", long_strike="760", quantity="10", marks=LIVE_MARKS),
        market(spot=LIVE_SPOT, strikes_marks=LIVE_MARKS),
    )


def test_the_live_position_of_2026_09_03_is_refused():
    """SPY 760/765 put credit spread, 10 contracts. APPROVED and FILLED on the live paper account at
    19:50:06Z on 2026-09-03 under `options-defined-risk`, which carries no expected-value floor.

    The marks are the ones in that decision's own market snapshot: 1.32 short, 0.75 long, on a width
    of 5. That is 0.57 of credit for 4.43 of risk -- the trade must be right 88.6% of the time simply
    to break even, and nothing in the record establishes that it is.
    """
    proposal, snapshot = live_position()
    evaluation, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.severity == "blocking"
    assert str(check.reason_code) == "REWARD_RISK_BELOW_MINIMUM"
    assert check.actual == "0.114", "credit 0.57 on width 5 is 11.4% credit-to-width"
    assert check.threshold == "0.2"
    assert evaluation.verdict == "REJECT"
    assert evaluation.recommended_quantity == "0"


def test_the_refusal_records_the_arithmetic_that_produced_it():
    """A refusal nobody can re-derive from the record is an assertion, not evidence."""
    proposal, snapshot = live_position()
    _, check = evaluate(proposal, snapshot, ev_policy())
    for fragment in ("width 5", "credit 0.57", "credit-to-width 0.114", "max loss 4.43", "0.886"):
        assert fragment in check.detail, f"{fragment!r} missing from: {check.detail}"
    assert check.data_source and check.snapshot_ts, "the evidence must name its source and its time"


def test_the_verdict_on_the_live_position_does_not_depend_on_the_floors():
    """The answer to "did you tune the floors until it failed?".

    Every floor is set to its loosest legal value -- zero, which admits any arithmetic at all. The
    trade is still refused, because at 11.4% credit-to-width there is no volatility for which this
    spread has non-negative expected value that the record also supports.
    """
    proposal, snapshot = live_position()
    loosest = ev_policy(min_credit_to_width="0", min_pop="0", min_ev_to_max_loss="0")
    evaluation, check = evaluate(proposal, snapshot, loosest)

    assert check.passed is False, "a zeroed floor must not rescue this trade"
    assert evaluation.verdict == "REJECT"
    # With the credit-to-width floor out of the way, it is the absent probability input that refuses:
    # a credit spread with no independent probability estimate has an EV of exactly zero before costs.
    assert str(check.reason_code) == "GREEKS_MISSING"
    assert "expected value of exactly zero" in check.detail


def test_the_live_position_passed_every_other_check_in_the_engine():
    """The point of the lane, stated as a test: this was not a trade the engine already stopped.

    Under a policy with no `ev` section every check passes and the verdict is PASS. The EV gate is the
    only control in this build that refuses it, which is why its absence was worth fixing.
    """
    proposal, snapshot = live_position()
    without_floors = make_policy(options=None)
    evaluation, check = evaluate(proposal, snapshot, without_floors)
    assert check.detail == "disabled by policy"
    assert evaluation.verdict == "PASS"


# ----------------------------------------------------------------------------------------------
# The floors, one at a time
# ----------------------------------------------------------------------------------------------
# spot 100, width 5, credit 1.50 => credit-to-width 0.30, max loss 3.50. iv 0.398 over 23 days puts
# one sigma at ~9.99, so the short strike's distance in points is very nearly its distance in sigmas.
PASSING_MARKS = {"90": "1.60", "85": "0.10"}
IV = "0.398"


def test_a_spread_that_clears_every_floor_passes():
    """z ~ 1.0 => POP ~ 0.8416 against a break-even of 0.70: a real edge, and it is allowed through."""
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv=IV)
    evaluation, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is True
    assert evaluation.verdict == "PASS"
    assert dec(check.actual) >= dec(FLOORS["min_ev_to_max_loss"])
    assert "EV" in check.detail and "POP" in check.detail


def test_credit_to_width_below_its_floor_is_refused():
    """Same distance, same volatility, only the credit is thinner: 0.75 on a width of 5 is 0.15."""
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks={"90": "0.85", "85": "0.10"}, iv=IV)
    _, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.actual == "0.15"
    assert check.threshold == "0.2"
    assert "credit-to-width below the floor" in check.detail


def test_probability_of_profit_below_its_floor_is_refused():
    """The short strike is one point away, not ten: z ~ 0.10, POP ~ 0.54. Below a coin flip's worth of
    edge, this is a directional bet wearing a credit spread's name."""
    proposal = spread(short_strike="99", long_strike="94")
    snapshot = market(spot="100", strikes_marks={"99": "1.60", "94": "0.10"}, iv=IV)
    _, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.threshold == FLOORS["min_pop"]
    assert dec(check.actual) < dec(FLOORS["min_pop"])
    assert "probability of profit below the floor" in check.detail


def test_positive_probability_but_negative_expectancy_is_refused():
    """The case that makes this check worth having. POP ~ 0.69 clears the POP floor comfortably and
    still loses money, because the market's break-even is 0.70. A gate that stopped at "probability
    of profit is high" would approve this."""
    proposal = spread(short_strike="95", long_strike="90")
    snapshot = market(spot="100", strikes_marks={"95": "1.60", "90": "0.10"}, iv=IV)
    _, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert dec(check.actual) < dec(FLOORS["min_ev_to_max_loss"])
    assert dec(check.actual) < 0, "this spread's expected value is negative, not merely small"
    assert "expected value below the floor" in check.detail
    assert "per spread" in check.detail


def test_each_floor_binds_independently():
    """Loosening one floor must not silently disable another."""
    proposal = spread(short_strike="95", long_strike="90")
    snapshot = market(spot="100", strikes_marks={"95": "1.60", "90": "0.10"}, iv=IV)
    _, still_refused = evaluate(proposal, snapshot, ev_policy(min_credit_to_width="0", min_pop="0"))
    assert still_refused.passed is False
    assert "expected value below the floor" in still_refused.detail


# ----------------------------------------------------------------------------------------------
# Fail closed: E2 applied to this check's own inputs
# ----------------------------------------------------------------------------------------------
def test_a_missing_option_mark_blocks_and_never_assumes_a_price():
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv=IV, drop="85")
    evaluation, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.severity == "blocking"
    assert str(check.reason_code) == "PRICE_MISSING"
    assert evaluation.verdict == "REJECT"
    assert evaluation.data_complete is False


def test_a_missing_underlying_quote_blocks():
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(symbol="SPY", spot="100", strikes_marks=PASSING_MARKS, iv=IV)
    payload = snapshot.model_dump(mode="json")
    payload.pop("snapshot_id")
    payload["quotes"] = {}
    _, check = evaluate(proposal, make_market_snapshot(**payload), ev_policy())

    assert check.passed is False
    assert str(check.reason_code) == "PRICE_MISSING"
    assert "distance to the short strike" in check.detail


def test_a_missing_volatility_blocks_rather_than_guessing_a_probability():
    """The live data tier's actual condition: marks but no IV, because OPRA is not signed.

    This must never degrade to "assume it is fine". Without an independent probability the expected
    value of a credit spread is exactly zero before costs, so absence is not a gap in the evidence --
    it IS the finding.
    """
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv=None)
    evaluation, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.severity == "blocking"
    assert str(check.reason_code) == "GREEKS_MISSING"
    assert evaluation.verdict == "REJECT"
    assert evaluation.data_complete is False
    assert "no implied volatility" in check.detail


def test_an_expiry_that_has_arrived_blocks():
    """Zero remaining days means no distribution to integrate; it must not divide by zero either."""
    today = FIXED_NOW.date().isoformat()
    proposal = spread(short_strike="90", long_strike="85", expiry=today)
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv=IV, expiry=today)
    _, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.severity == "blocking"
    assert "no remaining" in check.detail


def test_a_non_positive_volatility_blocks():
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv="0")
    _, check = evaluate(proposal, snapshot, ev_policy())

    assert check.passed is False
    assert check.severity == "blocking"
    assert "non-positive volatility" in check.detail


# ----------------------------------------------------------------------------------------------
# Scope: what this check declines to price, and says so
# ----------------------------------------------------------------------------------------------
def test_an_equity_proposal_is_out_of_scope():
    policy = ev_policy()
    _, check = evaluate(make_proposal(), make_market_snapshot(), policy)
    assert check.passed is True
    assert "not an options proposal" in check.detail


def test_a_closing_order_is_never_gated():
    """A control that blocks an exit strands the position it exists to protect."""
    proposal = spread(short_strike="765", long_strike="760")
    closing = TradeProposal.build(
        **{
            k: v
            for k, v in proposal.model_dump(mode="json").items()
            if k not in ("proposal_id", "total_quantity")
        }
        | {"intent": "close"}
    )
    evaluation, check = evaluate(closing, market(spot=LIVE_SPOT, strikes_marks=LIVE_MARKS), ev_policy())
    assert check.passed is True
    assert "closing a position" in check.detail
    assert evaluation.verdict == "PASS"


def test_a_single_leg_option_is_out_of_scope_and_says_the_leg_count():
    _, check = evaluate(make_option_proposal(), make_market_snapshot(), ev_policy())
    assert check.passed is True
    assert "not a two-leg vertical" in check.detail
    assert check.actual == "1"


def test_a_debit_spread_is_out_of_scope():
    """Buying the 90 and selling the 85 for a net debit: defined risk, but not this arithmetic."""
    proposal = spread(short_strike="85", long_strike="90")
    snapshot = market(spot="100", strikes_marks={"90": "1.60", "85": "0.10"}, iv=IV)
    _, check = evaluate(proposal, snapshot, ev_policy())
    assert check.passed is True
    assert "DEBIT" in check.detail


def test_a_tenant_without_ev_floors_gets_no_blocking_check():
    """The opt-in property. A policy with no `ev` section must not inherit a control it never asked
    for -- and must not report a blocking pass for one either."""
    policy = make_policy(options=None)
    assert policy.ev is None
    assert "expected_value" not in policy.enabled_checks
    _, check = evaluate(*live_position(), policy)
    assert (check.passed, check.severity, check.detail) == (True, "info", "disabled by policy")


# ----------------------------------------------------------------------------------------------
# The arithmetic itself
# ----------------------------------------------------------------------------------------------
def test_the_identity_that_the_whole_check_rests_on():
    """EV / width == POP - (1 - credit_to_width), so `1 - credit_to_width` is the market's break-even.

    Asserted numerically rather than trusted: if this identity is wrong, every floor in section 4 of
    docs/EV-GATE.md is justified by an equation that does not hold.
    """
    for r_str, pop_str in (("0.30", "0.84"), ("0.114", "0.788"), ("0.5", "0.5"), ("0.25", "0.99")):
        r, pop, width = dec(r_str), dec(pop_str), Decimal(5)
        credit = width * r
        ev = pop * credit - (Decimal(1) - pop) * (width - credit)
        assert ev / width == pop - (Decimal(1) - r), f"identity fails at r={r_str}, pop={pop_str}"


def test_a_fairly_priced_credit_spread_has_exactly_zero_expected_value():
    """The reason a missing volatility input is a refusal and not a shrug."""
    for r_str in ("0.05", "0.114", "0.20", "0.35", "0.50"):
        r, width = dec(r_str), Decimal(5)
        credit, pop = width * r, Decimal(1) - r
        assert pop * credit - (Decimal(1) - pop) * (width - credit) == 0


@pytest.mark.parametrize(
    ("z", "expected"),
    [("0", "0.5"), ("1", "0.8413"), ("-1", "0.1587"), ("1.96", "0.9750"), ("0.8", "0.7881")],
)
def test_normal_cdf_matches_the_published_table(z: str, expected: str):
    assert abs(normal_cdf(dec(z)) - dec(expected)) < Decimal("0.0001")


def test_normal_cdf_is_symmetric_monotone_and_bounded():
    previous = Decimal(-1)
    for step in range(-50, 51):
        z = Decimal(step) / Decimal(10)
        value = normal_cdf(z)
        assert Decimal(0) <= value <= Decimal(1), f"Phi({z}) escaped [0, 1]"
        assert value >= previous, f"Phi is not monotone at {z}"
        assert abs(value + normal_cdf(-z) - Decimal(1)) < Decimal("0.0000001")
        previous = value
    assert normal_cdf(Decimal(100)) == Decimal(1)
    assert normal_cdf(Decimal(-100)) == Decimal(0)


def test_the_whole_path_is_decimal_and_never_float():
    """INV-15 / Hard Rule A6, asserted on the returned values rather than trusted to review."""
    proposal = spread(short_strike="90", long_strike="85")
    snapshot = market(spot="100", strikes_marks=PASSING_MARKS, iv=IV)
    _, check = evaluate(proposal, snapshot, ev_policy())
    assert isinstance(normal_cdf(dec("0.8")), Decimal)
    for value in (check.threshold, check.actual):
        assert value is None or isinstance(value, str), "a recorded number is a DecimalStr, never a float"
    assert "e-" not in (check.detail or "").lower(), "no float repr leaked into the evidence"


# ----------------------------------------------------------------------------------------------
# Adversarial: the agent must not be able to choose its own verdict
# ----------------------------------------------------------------------------------------------
def test_the_limit_price_cannot_move_the_verdict():
    """F-1 applied here: valuation comes from the marks. An agent claiming an enormous credit in its
    own limit price must not thereby clear a floor the market's prices do not clear."""
    honest = spread(short_strike="765", long_strike="760", marks=LIVE_MARKS)
    inflated = spread(short_strike="765", long_strike="760", limit_price="999")
    snapshot = market(spot=LIVE_SPOT, strikes_marks=LIVE_MARKS)
    _, from_honest = evaluate(honest, snapshot, ev_policy())
    _, from_inflated = evaluate(inflated, snapshot, ev_policy())

    assert from_inflated.passed is False
    assert (from_honest.actual, from_honest.threshold) == (from_inflated.actual, from_inflated.threshold)


def test_the_floors_cannot_be_configured_negative():
    """A negative EV floor would configure a gate that permits a trade the engine has just computed to
    lose money. The contract refuses to express it, so no policy can ship it."""
    with pytest.raises(Exception):  # noqa: B017 - the refusal type is pydantic's to choose
        ev_policy(min_ev_to_max_loss="-1")


def test_the_same_inputs_always_produce_the_same_verdict():
    """Hard Rule A1 at the check level."""
    proposal, snapshot = live_position()
    policy = ev_policy()
    first = evaluate(proposal, snapshot, policy)[1].model_dump(mode="json")
    for _ in range(3):
        assert evaluate(proposal, snapshot, policy)[1].model_dump(mode="json") == first


# ----------------------------------------------------------------------------------------------
# INV-25 / INV-26 for this check specifically
# ----------------------------------------------------------------------------------------------
def test_this_check_is_observed_failing_which_is_what_invariant_25_demands():
    """INV-25 does not reach `expected_value` through the shared battery, because no fixture policy
    carries an `ev` section. It is observed failing here instead, on a constructible input -- and on
    the real one, which is stronger than a constructed one."""
    _, check = evaluate(*live_position(), ev_policy())
    assert check.passed is False and check.severity == "blocking"
    assert check.reason_code is not None, "a failed check must carry a reason code (A4)"


@pytest.mark.parametrize(
    "case",
    [
        "live", "passing", "thin_credit", "no_iv", "single_leg", "equity", "debit", "missing_mark",
    ],
)
def test_no_path_ever_reports_a_blocking_pass_without_evidence(case: str):
    """INV-26 for this check: a pass carrying nothing is indistinguishable from a check that never ran."""
    policy = ev_policy()
    if case == "live":
        proposal, snapshot = live_position()
    elif case == "passing":
        proposal, snapshot = spread(short_strike="90", long_strike="85"), market(
            spot="100", strikes_marks=PASSING_MARKS, iv=IV
        )
    elif case == "thin_credit":
        proposal, snapshot = spread(short_strike="90", long_strike="85"), market(
            spot="100", strikes_marks={"90": "0.85", "85": "0.10"}, iv=IV
        )
    elif case == "no_iv":
        proposal, snapshot = spread(short_strike="90", long_strike="85"), market(
            spot="100", strikes_marks=PASSING_MARKS, iv=None
        )
    elif case == "single_leg":
        proposal, snapshot = make_option_proposal(), make_market_snapshot()
    elif case == "equity":
        proposal, snapshot = make_proposal(), make_market_snapshot()
    elif case == "debit":
        proposal, snapshot = spread(short_strike="85", long_strike="90"), market(
            spot="100", strikes_marks={"90": "1.60", "85": "0.10"}, iv=IV
        )
    else:
        proposal, snapshot = spread(short_strike="90", long_strike="85"), market(
            spot="100", strikes_marks=PASSING_MARKS, iv=IV, drop="85"
        )

    _, check = evaluate(proposal, snapshot, policy)
    has_evidence = any(
        (check.threshold, check.actual, check.data_source, check.snapshot_ts)
    ) or bool((check.detail or "").strip())
    assert has_evidence, f"{case}: a blocking pass with no evidence at all"
