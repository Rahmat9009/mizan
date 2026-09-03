"""``scripts/position_monitor.py`` is a shipped surface, so it is tested like one.

Two things carry the weight here.

The first is arithmetic a judge will read off the screen and act on: days to expiry, and how far the
underlying is from a short strike. Those are the numbers that say "this is fine" or "this is about to
hurt", and getting the sign wrong on a short put would say the opposite of the truth.

The second is the guarantee in the docstring: this monitor REPORTS ONLY. Mizan has no cancel,
replace or close broker path (Hard Rule B4), and the test below reads the script's own source to
prove no such call was quietly added to it - a promise in a docstring is worth what the code under it
does.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "position_monitor.py"


def _load():
    """Import the script by path; ``scripts/`` is not a package and must not become one."""
    spec = importlib.util.spec_from_file_location("mizan_position_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = _load()


# -- OCC parsing -----------------------------------------------------------------------------
def test_occ_symbol_splits_into_underlying_expiry_type_and_strike() -> None:
    parsed = monitor.parse_occ("SPY260908P00737000")
    assert parsed == {
        "underlying": "SPY",
        "expiry": date(2026, 9, 8),
        "contract_type": "put",
        "strike": Decimal("737"),
    }


def test_occ_strike_keeps_its_fractional_part() -> None:
    """A 737.5 strike must not round to 737: the distance to it is the whole point of the report."""
    assert monitor.parse_occ("SPY260908P00737500")["strike"] == Decimal("737.5")


@pytest.mark.parametrize("symbol", ["SPY", "AAPL", "", "SPY260908X00737000", "SPY26098P00737000"])
def test_non_option_symbols_parse_to_none_rather_than_a_guess(symbol: str) -> None:
    """An equity has no expiry and no strike. Absence is reported, never invented."""
    assert monitor.parse_occ(symbol) is None


# -- days to expiry --------------------------------------------------------------------------
def test_days_to_expiry_counts_calendar_days() -> None:
    assert monitor.days_to_expiry(date(2026, 9, 10), today=date(2026, 9, 3)) == 7


def test_expiry_today_is_called_zero_dte_not_one_day() -> None:
    assert monitor.expiry_words(monitor.days_to_expiry(date(2026, 9, 3), today=date(2026, 9, 3))) == (
        "0DTE - expires today"
    )


def test_a_past_expiry_says_expired_rather_than_a_negative_number() -> None:
    assert monitor.expiry_words(monitor.days_to_expiry(date(2026, 9, 1), today=date(2026, 9, 3))) == (
        "EXPIRED 2 day(s) ago"
    )


# -- distance from the short strike ----------------------------------------------------------
def test_a_short_put_is_threatened_by_a_fall() -> None:
    result = monitor.strike_distance(
        spot=Decimal("745"), strike=Decimal("738"), contract_type="put"
    )
    assert result["direction"] == "down"
    assert result["points"] == Decimal("7")
    assert result["breached"] is False


def test_a_short_call_is_threatened_by_a_rise() -> None:
    result = monitor.strike_distance(
        spot=Decimal("745"), strike=Decimal("775"), contract_type="call"
    )
    assert result["direction"] == "up"
    assert result["points"] == Decimal("30")
    assert result["breached"] is False


def test_a_short_put_below_spot_is_breached() -> None:
    result = monitor.strike_distance(
        spot=Decimal("730"), strike=Decimal("738"), contract_type="put"
    )
    assert result["breached"] is True
    assert result["points"] == Decimal("8")


def test_a_short_call_below_spot_is_breached() -> None:
    result = monitor.strike_distance(
        spot=Decimal("790"), strike=Decimal("775"), contract_type="call"
    )
    assert result["breached"] is True


def test_distance_is_unknown_rather_than_zero_when_the_underlying_was_not_quoted() -> None:
    """E2 in miniature: an unquoted underlying must not become a strike sitting exactly at spot."""
    result = monitor.strike_distance(spot=None, strike=Decimal("738"), contract_type="put")
    assert result["points"] is None
    assert result["percent"] is None
    assert result["breached"] is None


# -- the rendered report ---------------------------------------------------------------------
class _Position:
    """The shape ``alpaca-py`` hands back. Attribute access only, values as strings."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def _vertical() -> list[dict]:
    """A real defined-risk shape: long the 737 put, short the 738 put, both expiring 2026-09-08."""
    return [
        monitor._position_row(
            _Position(
                symbol="SPY260908P00737000",
                asset_class="us_option",
                side="long",
                qty="5",
                avg_entry_price="0.02",
                current_price="0.03",
                market_value="15",
                cost_basis="10",
                unrealized_pl="5",
                unrealized_plpc="0.5",
            )
        ),
        monitor._position_row(
            _Position(
                symbol="SPY260908P00738000",
                asset_class="us_option",
                side="short",
                qty="-5",
                avg_entry_price="0.04",
                current_price="0.06",
                market_value="-30",
                cost_basis="-20",
                unrealized_pl="-10",
                unrealized_plpc="-0.5",
            )
        ),
    ]


def test_a_short_leg_is_recognised_from_a_negative_quantity() -> None:
    long_leg, short_leg = _vertical()
    assert long_leg["is_short"] is False
    assert short_leg["is_short"] is True
    assert short_leg["strike"] == Decimal("738")
    assert short_leg["contract_type"] == "put"


def test_the_report_names_expiry_pnl_and_the_short_strike_distance() -> None:
    report = monitor.render(
        _vertical(),
        {"SPY": Decimal("745.20")},
        account_id="5b61edf2-7440-4d4a-9c5a-186a4f262ab0",
        today=date(2026, 9, 3),
        as_of_label="2026-09-03T19:00:00Z (live)",
    )
    assert "5 days" in report                      # 2026-09-08 minus 2026-09-03
    assert "SPY260908P00738000" in report          # every position appears
    assert "SPY260908P00737000" in report
    assert "short put strike 738.00" in report     # the short leg is singled out
    assert "7.20 points" in report                 # 745.20 - 738
    assert "down from spot" in report              # a short put is threatened by a fall
    assert "intact" in report
    assert "-5.00" in report                       # net unrealised P&L: +5 and -10
    assert "SHORT" in report and "long" in report


def test_the_report_says_a_breached_strike_is_breached() -> None:
    report = monitor.render(
        _vertical(),
        {"SPY": Decimal("730")},
        account_id="acct",
        today=date(2026, 9, 3),
        as_of_label="live",
    )
    assert "BREACHED" in report


def test_an_unquoted_underlying_produces_no_number_at_all() -> None:
    report = monitor.render(
        _vertical(),
        {"SPY": None},
        account_id="acct",
        today=date(2026, 9, 3),
        as_of_label="live",
    )
    assert "distance unknown" in report
    assert "no substitute price is used" in report


def test_no_open_positions_is_reported_not_treated_as_an_error() -> None:
    report = monitor.render(
        [], {}, account_id="acct", today=date(2026, 9, 3), as_of_label="live"
    )
    assert "No open positions." in report


def test_every_report_carries_the_read_only_banner() -> None:
    for positions in ([], _vertical()):
        report = monitor.render(
            positions,
            {"SPY": Decimal("745")},
            account_id="acct",
            today=date(2026, 9, 3),
            as_of_label="live",
        )
        assert "READ ONLY" in report
        assert "does not close, cancel or replace anything" in report


# -- the Hard Rule the docstring promises ----------------------------------------------------
FORBIDDEN = (
    "cancel_order",
    "cancel_orders",
    "cancel_order_by_id",
    "close_position",
    "close_all_positions",
    "replace_order",
    "submit_order",
    "delete_order",
)


def test_the_monitor_contains_no_broker_mutation_call() -> None:
    """B4, checked against the file rather than against the promise at the top of it.

    Every name below is a real ``alpaca-py`` method. If a later edit reaches for one of them, this
    fails - which is the only reason the docstring's "it cannot" is worth reading.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    for name in FORBIDDEN:
        assert f".{name}(" not in source, f"{SCRIPT.name} calls {name}; the monitor must only read"


def test_the_docstring_states_the_report_only_rule() -> None:
    assert "REPORTS ONLY" in monitor.__doc__
    assert "NEVER CLOSES ANYTHING" in monitor.__doc__


# -- the spot price the distance is measured against -------------------------------------------
class _Broker:
    """A broker double returning a real ``MarketSnapshot``, the contract type the adapter emits."""

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot
        self.calls: list[dict] = []

    def get_market_snapshot(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._snapshot


def test_the_spot_used_is_the_quote_midpoint_the_adapter_publishes() -> None:
    """Regression: reading a field the contract does not have made every spot silently unquoted.

    ``Quote`` carries ``price`` - already the bid/ask midpoint, computed inside the adapter - and has
    no ``mark``. Asking for ``mark`` returned None for every underlying, so every short strike
    reported "distance unknown" against a venue that had quoted it perfectly well. The failure was
    quiet, which is what makes it worth a test: a missing distance looks like missing data, not like
    a bug.
    """
    from mizan.contracts import MarketSnapshot, Quote

    snapshot = MarketSnapshot.build(
        as_of="2026-09-03T19:00:00Z",
        quotes={
            "SPY": Quote(
                symbol="SPY",
                price="745.20",
                bid="745.10",
                ask="745.30",
                as_of="2026-09-03T19:00:00Z",
                source="alpaca:paper:quotes",
            )
        },
        option_quotes={},
        sectors={},
        source="alpaca:paper",
    )
    assert monitor._spot_quotes(_Broker(snapshot), ["SPY"]) == {"SPY": Decimal("745.20")}


def test_an_underlying_the_venue_did_not_quote_stays_none() -> None:
    from mizan.contracts import MarketSnapshot

    snapshot = MarketSnapshot.build(
        as_of="2026-09-03T19:00:00Z",
        quotes={},
        option_quotes={},
        sectors={},
        source="alpaca:paper",
    )
    assert monitor._spot_quotes(_Broker(snapshot), ["SPY"]) == {"SPY": None}


def test_a_quote_failure_degrades_the_report_rather_than_ending_it() -> None:
    """A dead market-data endpoint must not cost the operator the P&L and expiry columns too."""

    class _Failing:
        def get_market_snapshot(self, **_: object) -> object:
            raise RuntimeError("market data is down")

    assert monitor._spot_quotes(_Failing(), ["SPY", "QQQ"]) == {"SPY": None, "QQQ": None}


def test_no_underlyings_asks_the_venue_nothing() -> None:
    broker = _Broker(None)
    assert monitor._spot_quotes(broker, []) == {}
    assert broker.calls == []
