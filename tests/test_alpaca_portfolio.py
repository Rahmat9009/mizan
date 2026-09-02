from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.alpaca.client import (
    AlpacaConfigurationError,
    AlpacaLiveModeDisabledError,
    ReadOnlyAlpacaClient,
    create_read_only_alpaca_client,
)
from app.alpaca.portfolio import AlpacaPortfolioError, AlpacaPortfolioProvider
from app.models import MarketRiskSnapshot, TradeProposal
from app.risk_engine import RiskEngine


def account(**updates):
    values = {
        "equity": "101500.00",
        "last_equity": "100000.00",
        "cash": "40000.00",
        "buying_power": "80000.00",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def position(**updates):
    values = {
        "symbol": "NVDA",
        "qty": "10",
        "side": "long",
        "market_value": "1800.00",
        "current_price": "180.00",
        "unrealized_pl": "150.00",
        "unrealized_plpc": "0.0909",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def provider_for(account_value=None, positions=None):
    client = Mock()
    client.get_account.return_value = account_value if account_value is not None else account()
    client.get_all_positions.return_value = positions if positions is not None else [position()]
    return AlpacaPortfolioProvider(client=client), client


def test_valid_account_and_position_mapping() -> None:
    provider, client = provider_for()

    snapshot = provider.get_snapshot()

    assert snapshot.source == "ALPACA_PAPER"
    assert snapshot.equity == 101_500
    assert snapshot.cash == 40_000
    assert snapshot.buying_power == 80_000
    assert snapshot.daily_pnl_pct == pytest.approx(0.015)
    assert snapshot.current_positions == {"NVDA": 1_800}
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].quantity == 10
    assert snapshot.positions[0].current_price == 180
    assert snapshot.positions[0].unrealized_pl == 150
    assert snapshot.positions[0].unrealized_pl_pct == pytest.approx(0.0909)
    client.get_account.assert_called_once_with()
    client.get_all_positions.assert_called_once_with()


def test_short_position_uses_signed_quantity_and_gross_concentration() -> None:
    provider, _ = provider_for(
        positions=[position(side="short", qty="5", market_value="-900")]
    )

    snapshot = provider.get_snapshot()

    assert snapshot.positions[0].quantity == -5
    assert snapshot.positions[0].market_value == -900
    assert snapshot.current_positions["NVDA"] == 900


def test_no_positions_maps_to_empty_portfolio() -> None:
    provider, _ = provider_for(positions=[])

    snapshot = provider.get_snapshot()

    assert snapshot.positions == []
    assert snapshot.current_positions == {}


@pytest.mark.parametrize(
    ("bad_account", "bad_positions"),
    [
        (account(equity="not-a-number"), []),
        (account(cash="NaN"), []),
        (account(buying_power="Infinity"), []),
        (account(), [position(market_value="bad")]),
        (account(), [position(current_price="bad")]),
    ],
)
def test_malformed_numeric_values_fail_without_fabrication(bad_account, bad_positions) -> None:
    provider, _ = provider_for(account_value=bad_account, positions=bad_positions)

    with pytest.raises(AlpacaPortfolioError):
        provider.get_snapshot()


def test_api_failure_produces_no_partial_snapshot() -> None:
    client = Mock()
    client.get_account.side_effect = TimeoutError("network timeout")
    provider = AlpacaPortfolioProvider(client=client)

    with pytest.raises(AlpacaPortfolioError, match="TimeoutError"):
        provider.get_snapshot()
    client.get_all_positions.assert_not_called()


def test_missing_credentials_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")
    monkeypatch.setenv("ALPACA_PAPER", "true")

    with pytest.raises(AlpacaConfigurationError, match="ALPACA_API_KEY"):
        create_read_only_alpaca_client(client_factory=Mock())


def test_live_mode_is_rejected_before_client_creation(monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    monkeypatch.setenv("ALPACA_PAPER", "false")

    with pytest.raises(AlpacaLiveModeDisabledError, match="forbidden"):
        create_read_only_alpaca_client(client_factory=factory)
    factory.assert_not_called()


def test_invalid_paper_setting_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "maybe")

    with pytest.raises(AlpacaConfigurationError, match="true/false"):
        create_read_only_alpaca_client(client_factory=Mock())


def test_paper_mode_is_true_by_default_and_forced_into_sdk(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    factory = Mock()
    sdk_client = Mock()
    factory.return_value = sdk_client

    wrapped = create_read_only_alpaca_client(
        api_key="paper-key",
        secret_key="paper-secret",
        client_factory=factory,
    )

    assert isinstance(wrapped, ReadOnlyAlpacaClient)
    factory.assert_called_once_with("paper-key", "paper-secret", paper=True)


def test_missing_daily_pnl_remains_none_and_blocks_deterministic_risk() -> None:
    provider, _ = provider_for(account_value=account(last_equity=None), positions=[])
    snapshot = provider.get_snapshot()
    proposal = TradeProposal(
        symbol="NVDA",
        side="BUY",
        quantity=10,
        estimated_price=180,
        strategy_confidence=0.8,
        thesis="Fictional sample thesis.",
        invalidation_condition="Fictional sample invalidation.",
    )
    market = MarketRiskSnapshot(
        symbol="NVDA",
        annualized_volatility=0.4,
        max_drawdown_30d=0.1,
        liquidity_score=0.9,
    )

    report = RiskEngine().evaluate(proposal, snapshot, market)

    assert snapshot.daily_pnl_pct is None
    assert report.blocked is True
    check = next(item for item in report.checks if item.rule == "daily_drawdown")
    assert check.severity == "BLOCK"
    assert "unavailable" in check.message


def test_read_only_wrapper_and_package_have_no_order_mutation_surface() -> None:
    wrapper = ReadOnlyAlpacaClient(Mock())
    forbidden = {
        "submit_order",
        "cancel_order",
        "cancel_orders",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
    }
    assert forbidden.isdisjoint(set(dir(wrapper)))

    package_root = Path(__file__).parents[1] / "app" / "alpaca"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.glob("*.py")
        if path.name != "execution.py"
    )
    for method in forbidden:
        assert f".{method}(" not in source
