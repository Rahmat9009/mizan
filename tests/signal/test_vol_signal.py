"""The computation: it is real, it is Decimal-only, and it is named for what it measures.

These tests are about the *signal*, not about the seam - the seam has its own file. Three things are
being defended here:

1. **The name.** ``realized_vol_rank`` is a realized-volatility percentile. The options-data
   entitlement this project has returns no implied volatility at all, so a field named "IV rank" would
   describe an input that does not exist. That is a correctness property, and it is asserted.
2. **Determinism.** Same bars, byte-identical reading. Hard Rule A1 applies to anything that ends up on
   a decision record, advisory or not.
3. **No binary float.** Hard Rule A6. The venue sends OHLC as JSON *numbers*, which is exactly where a
   float would enter unnoticed, so the AST scan below mirrors
   ``tests/adapters/test_alpaca_paper.py::test_no_float_appears_anywhere_in_the_account_parse_path``
   and the runtime tests check that the decoded values really are Decimals.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from mizan.contracts.canonical import canonical_json
from mizan.contracts.types import dec
from mizan.signal import (
    ATR_PERIOD,
    REALIZED_VOL_WINDOW,
    Bar,
    BarDataError,
    InsufficientBars,
    bars_url,
    compute_vol_signal,
    minimum_bars,
    parse_bars,
    parse_bars_payload,
)
from tests.signal.conftest import bar_rows

SIGNAL_PACKAGE = Path(__file__).resolve().parents[2] / "mizan" / "signal"


# ---------------------------------------------------------------------------------------------------
# It computes something real
# ---------------------------------------------------------------------------------------------------
def test_the_reading_has_a_rank_an_atr_and_a_regime(signal):
    rank = dec(signal.realized_vol_rank)
    assert Decimal(0) <= rank <= Decimal(100), signal.realized_vol_rank
    assert dec(signal.atr) > 0, "ATR of a series that moves cannot be zero"
    assert dec(signal.realized_vol) > 0
    assert signal.regime in {"HIGH", "MID", "LOW"}
    assert signal.symbol == "SPY"
    assert signal.as_of == "2025-02-04"


def test_the_regime_is_the_rank_band_and_high_is_the_upper_half(bars):
    from mizan.signal.vol import HIGH_RANK_THRESHOLD, LOW_RANK_THRESHOLD, _regime

    assert _regime(HIGH_RANK_THRESHOLD) == "HIGH"
    assert _regime(HIGH_RANK_THRESHOLD - Decimal("0.01")) == "MID"
    assert _regime(LOW_RANK_THRESHOLD) == "MID"
    assert _regime(LOW_RANK_THRESHOLD - Decimal("0.01")) == "LOW"
    assert _regime(Decimal(0)) == "LOW"
    assert _regime(Decimal(100)) == "HIGH"

    reading = compute_vol_signal(bars, symbol="SPY")
    rank = dec(reading.realized_vol_rank)
    expected = "HIGH" if rank >= HIGH_RANK_THRESHOLD else ("MID" if rank >= LOW_RANK_THRESHOLD else "LOW")
    assert reading.regime == expected


def test_atr_matches_wilders_definition_computed_independently(bars):
    """A second, differently-written implementation of ATR(14), so the test is not the code again."""
    ranges: list[Decimal] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        prior_close = dec(previous.close)
        ranges.append(
            max(
                dec(current.high) - dec(current.low),
                abs(dec(current.high) - prior_close),
                abs(dec(current.low) - prior_close),
            )
        )
    atr = sum(ranges[:ATR_PERIOD], Decimal(0)) / Decimal(ATR_PERIOD)
    for true_range in ranges[ATR_PERIOD:]:
        atr = (atr * Decimal(ATR_PERIOD - 1) + true_range) / Decimal(ATR_PERIOD)

    computed = dec(compute_vol_signal(bars, symbol="SPY").atr)
    assert abs(computed - atr) <= Decimal("0.0001"), (computed, atr)


def test_a_calmer_series_ranks_below_a_wilder_one():
    """The rank has to *respond* to volatility, or it is a constant with a good name."""
    calm = parse_bars(bar_rows(300, start_price="400.00"))
    calm_reading = compute_vol_signal(calm, symbol="SPY")

    rows = bar_rows(300, start_price="400.00")
    price = Decimal("400")
    for index, row in enumerate(rows):
        # Same walk, amplified fivefold over the most recent quarter only: the lookback is unchanged,
        # the current window is not, so the percentile must move up.
        if index >= 220:
            swing = Decimal(5 if index % 2 == 0 else -5)
            price = dec(str(row["c"])) * (Decimal(1) + swing / Decimal(100))
            row["c"] = price
            row["o"] = price
            row["h"] = price * Decimal("1.01")
            row["l"] = price * Decimal("0.99")
    stormy_reading = compute_vol_signal(parse_bars(rows), symbol="SPY")

    assert dec(stormy_reading.realized_vol) > dec(calm_reading.realized_vol)
    assert dec(stormy_reading.realized_vol_rank) > dec(calm_reading.realized_vol_rank)
    assert stormy_reading.regime == "HIGH"


# ---------------------------------------------------------------------------------------------------
# The name is part of the contract with the reader
# ---------------------------------------------------------------------------------------------------
def test_nothing_in_the_package_claims_to_be_implied_volatility():
    """The data tier returns no greeks and no IV. A field called "iv_rank" would state a falsehood.

    Prose that *explains* the distinction is fine and is exactly what the docstrings do; an identifier,
    a dict key or a rendered value that presents this number as implied volatility is not.
    """
    reading = compute_vol_signal(parse_bars(bar_rows(200)), symbol="SPY")
    payload = reading.as_dict()
    assert "realized_vol_rank" in payload
    assert not any("iv" == key.lower() or key.lower().startswith("iv_") for key in payload)
    assert "implied" not in reading.summary().lower().replace("not implied volatility", "")

    offenders: list[str] = []
    for path in sorted(SIGNAL_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                name = node.value
            if name is None:
                continue
            lowered = name.lower()
            if "iv_rank" in lowered or "ivrank" in lowered or "implied_vol" in lowered:
                offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')}: {name!r}")
    assert not offenders, f"the package must not present realized vol as implied vol: {offenders}"


# ---------------------------------------------------------------------------------------------------
# Determinism (Hard Rule A1)
# ---------------------------------------------------------------------------------------------------
def test_the_same_bars_produce_a_byte_identical_reading(bars):
    first = compute_vol_signal(bars, symbol="SPY")
    second = compute_vol_signal(bars, symbol="SPY")
    third = compute_vol_signal(list(bars), symbol="SPY")
    assert first.canonical() == second.canonical() == third.canonical()
    assert first.digest() == second.digest() == third.digest()
    assert first == second == third
    assert canonical_json(first.as_dict()) == canonical_json(third.as_dict())


def test_page_order_does_not_change_the_series_or_the_reading(rows):
    """Pagination is an implementation detail of the fetch. It must not reach the reading."""
    shuffled = list(reversed(rows))
    assert parse_bars(shuffled) == parse_bars(rows)
    assert compute_vol_signal(parse_bars(shuffled), symbol="SPY").digest() == (
        compute_vol_signal(parse_bars(rows), symbol="SPY").digest()
    )


def test_a_duplicated_bar_collapses_rather_than_double_counting(rows):
    duplicated = rows + rows[100:140]
    assert parse_bars(duplicated) == parse_bars(rows)


def test_evaluation_touches_no_socket(bars, monkeypatch):
    """The signal is a pure function of bars. Fetching is a separate step, by construction."""
    import socket

    def refuse(*_args, **_kwargs):
        raise AssertionError("compute_vol_signal must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert compute_vol_signal(bars, symbol="SPY").regime in {"HIGH", "MID", "LOW"}


# ---------------------------------------------------------------------------------------------------
# No binary float anywhere (Hard Rule A6 / INV-15), mirroring the adapter's own scan
# ---------------------------------------------------------------------------------------------------
def test_no_float_appears_anywhere_in_the_signal_package():
    """The venue sends OHLC as JSON numbers; a float here would silently reshape a price nobody quoted.

    Same scan as tests/adapters/test_alpaca_paper.py, widened to every module in the package and to
    ``math`` imports, since a volatility calculation is the obvious place someone would reach for one.
    """
    files = sorted(SIGNAL_PACKAGE.rglob("*.py"))
    assert files, "no files scanned"
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            line = getattr(node, "lineno", "?")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                offenders.append(f"{path.name}:{line}: float(...)")
            elif isinstance(node, ast.Name) and node.id == "float":
                offenders.append(f"{path.name}:{line}: name `float`")
            elif isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path.name}:{line}: float literal {node.value!r}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, complex):
                offenders.append(f"{path.name}:{line}: complex literal {node.value!r}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "math" or alias.name.startswith("math."):
                        offenders.append(f"{path.name}:{line}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "math" or node.module.startswith("math."):
                    offenders.append(f"{path.name}:{line}: from {node.module} import ...")
    assert not offenders, f"the signal must never touch a binary float: {offenders}"


def test_json_numbers_are_decoded_as_decimals_not_floats():
    """The one line that keeps a float out: ``json.loads(..., parse_float=Decimal)``."""
    body = '{"bars":[{"t":"2026-01-02T05:00:00Z","o":0.1,"h":0.3,"l":0.1,"c":0.3,"v":10}],' \
           '"next_page_token":null}'
    parsed, token = parse_bars_payload(body)
    assert token is None
    assert parsed[0].close == "0.3", "the price must be the digits the venue printed"
    assert isinstance(parsed[0].close, str)
    assert dec(parsed[0].close) + dec(parsed[0].open) == Decimal("0.4"), "0.1 + 0.3 in decimal, exactly"


def test_a_float_shaped_price_never_becomes_a_binary_float():
    import mizan.signal.bars as bars_module

    source = inspect.getsource(bars_module)
    assert "parse_float=Decimal" in source, "the JSON decoder must map numbers to Decimal"


# ---------------------------------------------------------------------------------------------------
# It refuses rather than guessing
# ---------------------------------------------------------------------------------------------------
def test_too_few_bars_is_a_refusal_not_a_number():
    for count in (0, 1, ATR_PERIOD, REALIZED_VOL_WINDOW + 1, minimum_bars() - 1):
        with pytest.raises(InsufficientBars):
            compute_vol_signal(parse_bars(bar_rows(count)), symbol="SPY")
    assert compute_vol_signal(parse_bars(bar_rows(minimum_bars())), symbol="SPY").bars_used == minimum_bars()


def test_out_of_order_bars_are_refused(bars):
    with pytest.raises(InsufficientBars):
        compute_vol_signal(tuple(reversed(bars)), symbol="SPY")


@pytest.mark.parametrize(
    "row",
    [
        {"t": "2026-01-02T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": None, "v": 1},
        {"t": "2026-01-02T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": True, "v": 1},
        {"t": "not-a-date", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
        {"t": "2026-01-02T05:00:00Z", "o": 1, "h": 1, "l": 2, "c": 1, "v": 1},
        {"t": "2026-01-02T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 0, "v": 1},
    ],
)
def test_an_unusable_bar_is_a_data_error(row):
    with pytest.raises(BarDataError):
        parse_bars([row])


def test_a_non_json_payload_is_a_data_error():
    with pytest.raises(BarDataError):
        parse_bars_payload("<html>403 Forbidden</html>")


# ---------------------------------------------------------------------------------------------------
# The fetch seam
# ---------------------------------------------------------------------------------------------------
def test_the_bars_url_is_the_read_only_market_data_endpoint():
    url = bars_url("SPY", start="2024-01-01")
    assert url.startswith("https://data.alpaca.markets/v2/stocks/SPY/bars?")
    assert "timeframe=1Day" in url
    assert "start=2024-01-01" in url
    assert "/v2/orders" not in url


def test_missing_credentials_is_a_named_failure_not_a_crash(monkeypatch):
    from mizan.signal import MissingCredentials, fetch_daily_bars

    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingCredentials):
        fetch_daily_bars("SPY")


def test_credentials_never_appear_in_the_reading_or_its_text(monkeypatch, bars):
    monkeypatch.setenv("APCA_API_KEY_ID", "SECRET-KEY-VALUE")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "SECRET-SECRET-VALUE")
    reading = compute_vol_signal(bars, symbol="SPY")
    rendered = reading.canonical() + reading.summary()
    assert "SECRET-KEY-VALUE" not in rendered
    assert "SECRET-SECRET-VALUE" not in rendered


def test_a_bar_is_immutable(bars):
    with pytest.raises(FrozenInstanceError):
        bars[0].close = "1"  # type: ignore[misc]
    assert isinstance(bars[0], Bar)
