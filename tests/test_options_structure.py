"""Phase 1 tests: OCC parsing, structural validity, payoff math, fingerprinting.

Everything here is pure. No database, no broker, no clock.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.models import InstrumentType, TradeProposal
from app.options import (
    OccSymbolError,
    OptionLeg,
    OptionStrategy,
    OptionTradeProposal,
    OptionType,
    parse_occ_symbol,
    structure_fingerprint,
    structure_payload,
)
from app.proposals import instrument_tag, parse_trade_proposal, parse_trade_proposal_json

EXPIRY = date(2026, 10, 16)

# Legs used across the suite. Symbols encode expiry 26-10-16 and their strike.
LONG_PUT_190 = ("AAPL261016P00190000", "BUY", OptionType.PUT, 190.0)
SHORT_PUT_200 = ("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0)
SHORT_CALL_230 = ("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0)
LONG_CALL_240 = ("AAPL261016C00240000", "BUY", OptionType.CALL, 240.0)
LONG_CALL_230 = ("AAPL261016C00230000", "BUY", OptionType.CALL, 230.0)
SHORT_PUT_190 = ("AAPL261016P00190000", "SELL", OptionType.PUT, 190.0)


def leg(spec: tuple, *, expiry: date = EXPIRY, **overrides) -> OptionLeg:
    symbol, side, option_type, strike = spec
    payload = {
        "option_symbol": symbol,
        "side": side,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry,
    }
    payload.update(overrides)
    return OptionLeg(**payload)


def proposal(strategy: OptionStrategy, legs: list[OptionLeg], premium: float, **overrides):
    payload = {
        "underlying": "AAPL",
        "strategy": strategy,
        "quantity": 1,
        "expiry": EXPIRY,
        "legs": legs,
        "estimated_net_premium_per_unit": premium,
        "strategy_confidence": 0.7,
        "thesis": "Structural test fixture.",
        "invalidation_condition": "Fixture invalidation.",
    }
    payload.update(overrides)
    return OptionTradeProposal(**payload)


def long_call(**kw):
    return proposal(OptionStrategy.LONG_CALL, [leg(LONG_CALL_230)], -4.20, **kw)


def long_put(**kw):
    return proposal(OptionStrategy.LONG_PUT, [leg(LONG_PUT_190)], -3.10, **kw)


def call_debit_spread(**kw):
    return proposal(
        OptionStrategy.VERTICAL_DEBIT_SPREAD,
        [leg(LONG_CALL_230), leg(("AAPL261016C00240000", "SELL", OptionType.CALL, 240.0))],
        -4.00,
        **kw,
    )


def put_credit_spread(**kw):
    return proposal(
        OptionStrategy.VERTICAL_CREDIT_SPREAD,
        [leg(LONG_PUT_190), leg(SHORT_PUT_200)],
        3.00,
        **kw,
    )


def iron_condor(**kw):
    return proposal(
        OptionStrategy.IRON_CONDOR,
        [leg(LONG_PUT_190), leg(SHORT_PUT_200), leg(SHORT_CALL_230), leg(LONG_CALL_240)],
        1.35,
        **kw,
    )


# --------------------------------------------------------------------------
# OCC symbol parsing
# --------------------------------------------------------------------------
def test_occ_symbol_decodes_every_field() -> None:
    parsed = parse_occ_symbol("AAPL261016C00230000")
    assert parsed.root == "AAPL"
    assert parsed.expiry == date(2026, 10, 16)
    assert parsed.option_type == "CALL"
    assert parsed.strike == 230.0


def test_occ_symbol_decodes_fractional_strike() -> None:
    assert parse_occ_symbol("SPY261016P00512500").strike == 512.5


def test_occ_symbol_accepts_short_and_numeric_roots() -> None:
    assert parse_occ_symbol("F261016C00012000").root == "F"
    assert parse_occ_symbol("AAPL1261016C00012000").root == "AAPL1"


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL261016X00230000",  # not C or P
        "AAPL2610C00230000",  # short date
        "AAPL261016C0023000",  # 7-digit strike
        "AAPL261016C002300000",  # 9-digit strike
        "TOOLONGX261016C00230000",  # 7-character root
        "aapl261016c00230000!",  # junk
        "",
    ],
)
def test_malformed_occ_symbols_are_rejected(symbol: str) -> None:
    with pytest.raises(OccSymbolError):
        parse_occ_symbol(symbol)


def test_occ_symbol_with_impossible_date_is_rejected() -> None:
    with pytest.raises(OccSymbolError):
        parse_occ_symbol("AAPL260230C00230000")  # 30 February


def test_occ_symbol_with_zero_strike_is_rejected() -> None:
    with pytest.raises(OccSymbolError):
        parse_occ_symbol("AAPL261016C00000000")


# --------------------------------------------------------------------------
# Leg self-consistency: declared fields must match the symbol
# --------------------------------------------------------------------------
def test_leg_strike_must_match_its_symbol() -> None:
    with pytest.raises(ValidationError, match="strikes at 230"):
        leg(("AAPL261016C00230000", "BUY", OptionType.CALL, 225.0))


def test_leg_option_type_must_match_its_symbol() -> None:
    with pytest.raises(ValidationError, match="is a CALL"):
        leg(("AAPL261016C00230000", "BUY", OptionType.PUT, 230.0))


def test_leg_expiry_must_match_its_symbol() -> None:
    with pytest.raises(ValidationError, match="expires 2026-10-16"):
        leg(LONG_CALL_230, expiry=date(2026, 11, 20))


def test_leg_ratio_other_than_one_is_rejected() -> None:
    with pytest.raises(ValidationError, match="only 1:1 legs"):
        leg(LONG_CALL_230, ratio=2)


def test_leg_symbol_is_normalized_to_uppercase() -> None:
    assert leg(("aapl261016c00230000", "BUY", OptionType.CALL, 230.0)).option_symbol == (
        "AAPL261016C00230000"
    )


# --------------------------------------------------------------------------
# The five supported strategies construct and price correctly
# --------------------------------------------------------------------------
def test_long_call_max_loss_is_the_premium_and_profit_is_unbounded() -> None:
    call = long_call()
    assert call.max_loss_per_unit == pytest.approx(420.0)
    assert call.max_profit_per_unit is None
    assert call.max_profit_total is None


def test_long_put_max_loss_is_premium_and_profit_is_capped_by_strike() -> None:
    put = long_put()
    assert put.max_loss_per_unit == pytest.approx(310.0)
    assert put.max_profit_per_unit == pytest.approx((190.0 - 3.10) * 100)


def test_debit_spread_risk_is_the_debit_not_the_notional() -> None:
    spread = call_debit_spread()
    assert spread.risk_width == pytest.approx(10.0)
    assert spread.max_loss_per_unit == pytest.approx(400.0)
    assert spread.max_profit_per_unit == pytest.approx(600.0)


def test_credit_spread_risk_is_width_less_credit() -> None:
    spread = put_credit_spread()
    assert spread.risk_width == pytest.approx(10.0)
    assert spread.max_loss_per_unit == pytest.approx(700.0)
    assert spread.max_profit_per_unit == pytest.approx(300.0)


def test_iron_condor_risk_uses_the_widest_wing() -> None:
    condor = iron_condor()
    assert condor.risk_width == pytest.approx(10.0)
    assert condor.max_loss_per_unit == pytest.approx(865.0)
    assert condor.max_profit_per_unit == pytest.approx(135.0)


def test_totals_scale_with_contract_count() -> None:
    condor = iron_condor(quantity=3)
    assert condor.max_loss_total == pytest.approx(2595.0)
    assert condor.max_profit_total == pytest.approx(405.0)


def test_asymmetric_condor_wings_take_the_wider_side() -> None:
    condor = proposal(
        OptionStrategy.IRON_CONDOR,
        [
            leg(("AAPL261016P00180000", "BUY", OptionType.PUT, 180.0)),
            leg(SHORT_PUT_200),
            leg(SHORT_CALL_230),
            leg(LONG_CALL_240),
        ],
        1.35,
    )
    assert condor.risk_width == pytest.approx(20.0)
    assert condor.max_loss_per_unit == pytest.approx(1865.0)


def test_non_standard_multiplier_is_honoured() -> None:
    call = long_call(contract_multiplier=10)
    assert call.max_loss_per_unit == pytest.approx(42.0)


# --------------------------------------------------------------------------
# Structural rejections
# --------------------------------------------------------------------------
def test_naked_short_call_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Naked short CALL"):
        proposal(
            OptionStrategy.LONG_CALL,
            [leg(("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0))],
            2.00,
        )


def test_naked_short_put_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Naked short PUT"):
        proposal(OptionStrategy.LONG_PUT, [leg(SHORT_PUT_190)], 2.00)


def test_two_shorts_against_one_long_is_rejected_as_unbalanced() -> None:
    with pytest.raises(ValidationError, match="Unbalanced short PUT legs: 2 short against 1 long"):
        proposal(
            OptionStrategy.IRON_CONDOR,
            [
                leg(LONG_PUT_190),
                leg(SHORT_PUT_200),
                leg(("AAPL261016P00210000", "SELL", OptionType.PUT, 210.0)),
                leg(LONG_CALL_240),
            ],
            1.35,
        )


def test_a_short_only_vertical_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Naked short PUT"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [leg(SHORT_PUT_200), leg(SHORT_PUT_190)],
            3.00,
        )


def test_wrong_leg_count_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires exactly 4 leg"):
        proposal(OptionStrategy.IRON_CONDOR, [leg(LONG_PUT_190), leg(SHORT_PUT_200)], 1.35)


def test_more_than_four_legs_is_rejected() -> None:
    with pytest.raises(ValidationError):
        proposal(
            OptionStrategy.IRON_CONDOR,
            [
                leg(LONG_PUT_190),
                leg(SHORT_PUT_200),
                leg(SHORT_CALL_230),
                leg(LONG_CALL_240),
                leg(("AAPL261016C00250000", "BUY", OptionType.CALL, 250.0)),
            ],
            1.35,
        )


def test_duplicate_leg_symbols_are_rejected() -> None:
    with pytest.raises(ValidationError, match="distinct option contract"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [leg(LONG_PUT_190), leg(("AAPL261016P00190000", "SELL", OptionType.PUT, 190.0))],
            3.00,
        )


def test_mixed_expiries_are_rejected() -> None:
    other = date(2026, 11, 20)
    with pytest.raises(ValidationError, match="single-expiry"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [
                leg(LONG_PUT_190),
                leg(("AAPL261120P00200000", "SELL", OptionType.PUT, 200.0), expiry=other),
            ],
            3.00,
        )


def test_leg_on_a_different_underlying_is_rejected() -> None:
    with pytest.raises(ValidationError, match="written on MSFT"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [leg(("MSFT261016P00190000", "BUY", OptionType.PUT, 190.0)), leg(SHORT_PUT_200)],
            3.00,
        )


def test_identical_strikes_in_a_vertical_are_rejected() -> None:
    # Same type, expiry, and strike is by definition the same contract, so this
    # lands on the distinct-contract rule rather than the strike-width rule.
    with pytest.raises(ValidationError, match="distinct option contract"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [
                leg(("AAPL261016P00200000", "BUY", OptionType.PUT, 200.0)),
                leg(("AAPL261016P00200000", "SELL", OptionType.PUT, 200.0)),
            ],
            3.00,
        )


def test_mismatched_leg_types_in_a_vertical_are_rejected() -> None:
    # A two-leg structure with one call and one put always leaves one side
    # uncovered, so it is caught as a naked short before the same-type rule.
    with pytest.raises(ValidationError, match="Naked short PUT"):
        proposal(
            OptionStrategy.VERTICAL_DEBIT_SPREAD,
            [leg(LONG_CALL_230), leg(SHORT_PUT_200)],
            -4.00,
        )


def test_call_debit_spread_with_inverted_strikes_is_rejected() -> None:
    with pytest.raises(ValidationError, match="debit spread must buy the lower strike"):
        proposal(
            OptionStrategy.VERTICAL_DEBIT_SPREAD,
            [leg(LONG_CALL_240), leg(("AAPL261016C00230000", "SELL", OptionType.CALL, 230.0))],
            -4.00,
        )


def test_put_credit_spread_with_inverted_strikes_is_rejected() -> None:
    with pytest.raises(ValidationError, match="credit spread must sell the higher strike"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [
                leg(("AAPL261016P00200000", "BUY", OptionType.PUT, 200.0)),
                leg(("AAPL261016P00190000", "SELL", OptionType.PUT, 190.0)),
            ],
            3.00,
        )


def test_iron_condor_with_crossed_strikes_is_rejected() -> None:
    with pytest.raises(ValidationError, match="strikes ordered long put"):
        proposal(
            OptionStrategy.IRON_CONDOR,
            [
                leg(("AAPL261016P00210000", "BUY", OptionType.PUT, 210.0)),
                leg(SHORT_PUT_200),
                leg(SHORT_CALL_230),
                leg(LONG_CALL_240),
            ],
            1.35,
        )


def test_iron_condor_needs_two_calls_and_two_puts() -> None:
    # Balanced longs and shorts, but every leg is a call: no put wing exists.
    with pytest.raises(ValidationError, match="two call legs and two put legs"):
        proposal(
            OptionStrategy.IRON_CONDOR,
            [
                leg(LONG_CALL_230),
                leg(("AAPL261016C00240000", "SELL", OptionType.CALL, 240.0)),
                leg(("AAPL261016C00250000", "BUY", OptionType.CALL, 250.0)),
                leg(("AAPL261016C00260000", "SELL", OptionType.CALL, 260.0)),
            ],
            1.35,
        )


def test_unsupported_strategy_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        proposal("CALENDAR_SPREAD", [leg(LONG_CALL_230)], -4.00)


def test_zero_premium_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-zero credit"):
        proposal(OptionStrategy.LONG_CALL, [leg(LONG_CALL_230)], 0.0)


def test_debit_strategy_with_a_credit_premium_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be opened for a net debit"):
        proposal(OptionStrategy.LONG_CALL, [leg(LONG_CALL_230)], 4.20)


def test_credit_strategy_with_a_debit_premium_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be opened for a net credit"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [leg(LONG_PUT_190), leg(SHORT_PUT_200)],
            -3.00,
        )


def test_credit_larger_than_the_wing_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot equal or exceed"):
        proposal(
            OptionStrategy.VERTICAL_CREDIT_SPREAD,
            [leg(LONG_PUT_190), leg(SHORT_PUT_200)],
            12.00,
        )


def test_debit_larger_than_the_width_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot equal or exceed"):
        proposal(
            OptionStrategy.VERTICAL_DEBIT_SPREAD,
            [leg(LONG_CALL_230), leg(("AAPL261016C00240000", "SELL", OptionType.CALL, 240.0))],
            -11.00,
        )


def test_long_put_debit_above_its_strike_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot equal or exceed its strike"):
        proposal(OptionStrategy.LONG_PUT, [leg(LONG_PUT_190)], -200.0)


def test_zero_and_negative_quantity_are_rejected() -> None:
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            iron_condor(quantity=bad)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        iron_condor(delta=0.3)


# --------------------------------------------------------------------------
# Declared max loss / profit are cross-checked, never trusted
# --------------------------------------------------------------------------
def test_matching_declared_values_are_accepted() -> None:
    condor = iron_condor(quantity=2, estimated_max_loss=1730.0, estimated_max_profit=270.0)
    assert condor.max_loss_total == pytest.approx(1730.0)


def test_wrong_declared_max_loss_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Declared max loss"):
        iron_condor(quantity=2, estimated_max_loss=500.0)


def test_wrong_declared_max_profit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Declared max profit"):
        iron_condor(quantity=2, estimated_max_profit=9999.0)


def test_declaring_a_finite_profit_for_an_unbounded_payoff_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unbounded maximum profit"):
        long_call(estimated_max_profit=5000.0)


# --------------------------------------------------------------------------
# Fingerprint: binds structure, ignores quantity and leg order
# --------------------------------------------------------------------------
def test_fingerprint_is_stable_across_identical_structures() -> None:
    assert structure_fingerprint(iron_condor()) == structure_fingerprint(iron_condor())


def test_fingerprint_ignores_quantity_so_reduce_stays_valid() -> None:
    assert structure_fingerprint(iron_condor(quantity=1)) == structure_fingerprint(
        iron_condor(quantity=7)
    )


def test_fingerprint_ignores_leg_order() -> None:
    reordered = proposal(
        OptionStrategy.IRON_CONDOR,
        [leg(LONG_CALL_240), leg(SHORT_CALL_230), leg(SHORT_PUT_200), leg(LONG_PUT_190)],
        1.35,
    )
    assert structure_fingerprint(reordered) == structure_fingerprint(iron_condor())


def test_fingerprint_changes_when_a_strike_changes() -> None:
    moved = proposal(
        OptionStrategy.IRON_CONDOR,
        [
            leg(("AAPL261016P00180000", "BUY", OptionType.PUT, 180.0)),
            leg(SHORT_PUT_200),
            leg(SHORT_CALL_230),
            leg(LONG_CALL_240),
        ],
        1.35,
    )
    assert structure_fingerprint(moved) != structure_fingerprint(iron_condor())


def test_fingerprint_changes_when_the_expiry_changes() -> None:
    other = date(2026, 11, 20)
    later = proposal(
        OptionStrategy.IRON_CONDOR,
        [
            leg(("AAPL261120P00190000", "BUY", OptionType.PUT, 190.0), expiry=other),
            leg(("AAPL261120P00200000", "SELL", OptionType.PUT, 200.0), expiry=other),
            leg(("AAPL261120C00230000", "SELL", OptionType.CALL, 230.0), expiry=other),
            leg(("AAPL261120C00240000", "BUY", OptionType.CALL, 240.0), expiry=other),
        ],
        1.35,
        expiry=other,
    )
    assert structure_fingerprint(later) != structure_fingerprint(iron_condor())


def test_fingerprint_changes_when_a_leg_is_removed() -> None:
    assert structure_fingerprint(put_credit_spread()) != structure_fingerprint(iron_condor())


def test_fingerprint_changes_when_a_side_flips() -> None:
    flipped = proposal(
        OptionStrategy.VERTICAL_DEBIT_SPREAD,
        [
            leg(("AAPL261016P00190000", "SELL", OptionType.PUT, 190.0)),
            leg(("AAPL261016P00200000", "BUY", OptionType.PUT, 200.0)),
        ],
        -3.00,
    )
    assert structure_fingerprint(flipped) != structure_fingerprint(put_credit_spread())


def test_fingerprint_changes_when_the_strategy_changes() -> None:
    assert structure_fingerprint(long_call()) != structure_fingerprint(
        proposal(OptionStrategy.VERTICAL_DEBIT_SPREAD,
                 [leg(LONG_CALL_230),
                  leg(("AAPL261016C00240000", "SELL", OptionType.CALL, 240.0))],
                 -4.00)
    )


def test_equity_and_option_fingerprints_never_collide() -> None:
    equity = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=1,
        estimated_price=250.0,
        strategy_confidence=0.8,
        thesis="t",
        invalidation_condition="i",
    )
    assert structure_fingerprint(equity) != structure_fingerprint(long_call())
    assert structure_payload(equity)["instrument_type"] == "equity"


def test_fingerprint_payload_records_what_was_bound() -> None:
    payload = structure_payload(iron_condor())
    assert payload["strategy"] == "IRON_CONDOR"
    assert payload["expiry"] == "2026-10-16"
    assert len(payload["legs"]) == 4
    assert "quantity" not in payload


# --------------------------------------------------------------------------
# Equity backward compatibility
# --------------------------------------------------------------------------
def test_equity_proposal_defaults_to_the_equity_instrument_type() -> None:
    equity = TradeProposal(
        symbol="AAPL",
        side="BUY",
        quantity=10,
        estimated_price=250.0,
        strategy_confidence=0.8,
        thesis="t",
        invalidation_condition="i",
    )
    assert equity.instrument_type == InstrumentType.EQUITY.value


def test_stored_json_without_an_instrument_type_still_reads_as_equity() -> None:
    legacy = (
        '{"proposal_id":"faisal-aapl-001","symbol":"AAPL","side":"BUY","quantity":10,'
        '"estimated_price":250.0,"strategy_confidence":0.82,"thesis":"t",'
        '"invalidation_condition":"i","created_at":"2026-09-01T12:00:00Z"}'
    )
    parsed = parse_trade_proposal_json(legacy)
    assert isinstance(parsed, TradeProposal)
    assert parsed.symbol == "AAPL"
    assert parsed.instrument_type == "equity"


def test_union_routes_an_explicit_option_payload() -> None:
    parsed = parse_trade_proposal(iron_condor().model_dump())
    assert isinstance(parsed, OptionTradeProposal)
    assert parsed.strategy == OptionStrategy.IRON_CONDOR


def test_union_routes_an_explicit_equity_payload() -> None:
    parsed = parse_trade_proposal(
        {
            "instrument_type": "equity",
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 1,
            "estimated_price": 250.0,
            "strategy_confidence": 0.8,
            "thesis": "t",
            "invalidation_condition": "i",
        }
    )
    assert isinstance(parsed, TradeProposal)


def test_instrument_tag_defaults_to_equity_for_untagged_payloads() -> None:
    assert instrument_tag({"symbol": "AAPL"}) == "equity"
    assert instrument_tag({"instrument_type": "option"}) == "option"


def test_option_proposal_round_trips_through_json() -> None:
    original = iron_condor(quantity=2)
    restored = parse_trade_proposal_json(original.model_dump_json())
    assert isinstance(restored, OptionTradeProposal)
    assert structure_fingerprint(restored) == structure_fingerprint(original)
    assert restored.max_loss_total == pytest.approx(original.max_loss_total)
