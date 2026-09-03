"""Vendor JSON into contract types, and what the adapter refuses to invent when a field is absent.

Everything here is driven through a stub MCP client holding canned Alpaca documents, so the mapping
is tested without a network, a credential or a subprocess. What is being asserted is not that JSON
parses - it is that an absent price stays absent, that a missing permission is not read as a grant,
and that a spread is submitted as ONE order at the right ratio and the right net limit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mizan.adapters.base import OrderRequest
from mizan.contracts.errors import BrokerError, LiveTradingForbidden
from mizan.mcp.alpaca import ALLOWED_TOOLS, AlpacaMCPBroker, _net_limit_price
from mizan.mcp.client import MCPToolResult

NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)

ACCOUNT: dict[str, Any] = {
    "id": "5b61edf2-7440-4d4a-9c5a-186a4f262ab0",
    "account_number": "PA3TESTACCOUNT",
    "status": "ACTIVE",
    "equity": "99996.95",
    "cash": "79395.10",
    "buying_power": "158790.20",
    "maintenance_margin": "1030.25",
    "trading_blocked": False,
    "account_blocked": False,
    "trade_suspended_by_user": False,
    "shorting_enabled": True,
    "options_trading_level": 3,
}


class StubClient:
    """Enough of :class:`~mizan.mcp.client.StdioMCPClient` to drive the adapter, and no more."""

    def __init__(self, responses: dict[str, Any], *, errors: set[str] | None = None) -> None:
        self.allowed_tools = ALLOWED_TOOLS
        self.argv = ["stub"]
        self.server_info: dict[str, Any] = {"name": "stub"}
        self.negotiated_version = "2025-06-18"
        self.responses = responses
        self.errors = errors or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        self.calls.append((name, dict(arguments or {})))
        if name in self.errors:
            return MCPToolResult(content=[{"type": "text", "text": f"{name} failed: 404 not found"}],
                                 is_error=True)
        import json

        payload = self.responses.get(name)
        return MCPToolResult(content=[{"type": "text", "text": json.dumps(payload)}])

    def close(self) -> None:
        pass


def broker(responses: dict[str, Any], **kwargs) -> AlpacaMCPBroker:
    return AlpacaMCPBroker(StubClient(responses, **kwargs))


class TestAccount:
    def test_permissions_map_across_and_carry_no_account_number(self) -> None:
        state = broker({"get_account_info": ACCOUNT}).get_account_state(as_of=NOW)
        assert state.status == "ACTIVE"
        assert state.trading_blocked is False
        assert state.options_trading_level == 3
        assert state.shorting_enabled is True
        # REQ-35: the identifier is sensitive and a record carrying one would be unbuildable.
        assert "account_number" not in state.model_dump(mode="json")
        assert "5b61edf2" not in state.model_dump_json()

    def test_a_missing_blocked_flag_stays_unknown_rather_than_becoming_permission(self) -> None:
        """``bool(None)`` is False, and False here means "not blocked" - a grant, invented (ESC-4)."""
        silent = {k: v for k, v in ACCOUNT.items() if k != "trading_blocked"}
        state = broker({"get_account_info": silent}).get_account_state(as_of=NOW)
        assert state.trading_blocked is None

    def test_an_account_that_is_not_paper_stops_every_read(self) -> None:
        live_looking = {**ACCOUNT, "account_number": "9ABCDEF"}
        with pytest.raises(LiveTradingForbidden):
            broker({"get_account_info": live_looking}).get_account_state(as_of=NOW)

    def test_the_paper_signal_is_re_derived_on_the_portfolio_read_too(self) -> None:
        with pytest.raises(LiveTradingForbidden):
            broker({"get_account_info": {**ACCOUNT, "account_number": ""}}).get_portfolio_snapshot(
                as_of=NOW
            )


class TestPortfolio:
    def test_positions_and_balances_map_across(self) -> None:
        responses = {
            "get_account_info": ACCOUNT,
            "get_all_positions": [
                {"symbol": "AAPL", "asset_class": "us_equity", "qty": "50", "market_value": "11425"},
                {
                    "symbol": "AAPL260925C00230000",
                    "asset_class": "us_option",
                    "qty": "2",
                    "market_value": "370",
                },
            ],
        }
        snapshot = broker(responses).get_portfolio_snapshot(as_of=NOW)
        assert snapshot.equity == "99996.95"
        # dstr is canonical: a trailing zero Alpaca sent is not part of the number.
        assert snapshot.cash == "79395.1"
        assert snapshot.margin_requirement == "1030.25"
        equity_position, option_position = snapshot.positions
        assert equity_position.asset_class == "equity"
        assert option_position.asset_class == "equity_option"
        # An option position is reported under its UNDERLYING, with the OCC symbol kept alongside.
        assert option_position.symbol == "AAPL"
        assert option_position.occ_symbol == "AAPL260925C00230000"

    def test_alpaca_reports_no_sector_so_the_field_stays_empty(self) -> None:
        """A DELTA against the contract, recorded rather than papered over: Alpaca exposes no sector
        classification on any endpoint, so ``sector_concentration`` blocks on SECTOR_DATA_MISSING
        until one is supplied out of band. Inventing a sector here would silently disable that check."""
        responses = {
            "get_account_info": ACCOUNT,
            "get_all_positions": [
                {"symbol": "AAPL", "asset_class": "us_equity", "qty": "1", "market_value": "228.5"}
            ],
        }
        assert broker(responses).get_portfolio_snapshot(as_of=NOW).positions[0].sector is None

    def test_a_position_missing_its_market_value_is_an_error_not_a_zero(self) -> None:
        responses = {
            "get_account_info": ACCOUNT,
            "get_all_positions": [{"symbol": "AAPL", "asset_class": "us_equity", "qty": "50"}],
        }
        with pytest.raises(BrokerError, match="requires"):
            broker(responses).get_portfolio_snapshot(as_of=NOW)

    def test_an_account_missing_equity_is_an_error_and_is_recorded_as_a_delta(self) -> None:
        starved = {k: v for k, v in ACCOUNT.items() if k != "equity"}
        adapter = broker({"get_account_info": starved, "get_all_positions": []})
        with pytest.raises(BrokerError):
            adapter.get_portfolio_snapshot(as_of=NOW)
        assert any("equity" in delta for delta in adapter.deltas)


class TestMarketData:
    def test_a_quote_is_the_midpoint_of_the_two_sided_market(self) -> None:
        responses = {
            "get_stock_latest_quote": {
                "quotes": {"AAPL": {"bp": "228.45", "ap": "228.55", "t": "2026-09-02T17:39:55Z"}}
            }
        }
        snapshot = broker(responses).get_market_snapshot(symbols=["AAPL"], as_of=NOW)
        quote = snapshot.quotes["AAPL"]
        assert quote.price == "228.5"
        assert quote.bid == "228.45"
        assert quote.as_of.startswith("2026-09-02T17:39:55")

    def test_the_long_field_names_are_understood_too(self) -> None:
        responses = {
            "get_stock_latest_quote": {
                "quotes": {
                    "AAPL": {
                        "bid_price": "10",
                        "ask_price": "10.5",
                        "timestamp": "2026-09-02T17:00:00Z",
                    }
                }
            }
        }
        snapshot = broker(responses).get_market_snapshot(symbols=["AAPL"], as_of=NOW)
        assert snapshot.quotes["AAPL"].price == "10.25"

    def test_a_symbol_with_no_usable_price_is_simply_absent(self) -> None:
        """Absent is the correct answer. ``mizan.risk`` blocks on a missing price; inventing a last
        close, a zero, or the caller's own limit (F-1) is exactly how that block gets lost."""
        responses = {"get_stock_latest_quote": {"quotes": {"AAPL": {"bp": "0", "ap": "0"}}}}
        assert broker(responses).get_market_snapshot(symbols=["AAPL"], as_of=NOW).quotes == {}

    def test_option_greeks_come_from_the_snapshot_tool(self) -> None:
        responses = {
            "get_option_snapshot": {
                "snapshots": {
                    "AAPL260925C00230000": {
                        "latestQuote": {"bp": "1.80", "ap": "1.90", "t": "2026-09-02T17:39:50Z"},
                        "greeks": {"delta": "0.168", "gamma": "0.021", "vega": "0.142", "theta": "-0.061"},
                    }
                }
            }
        }
        snapshot = broker(responses).get_market_snapshot(
            symbols=[], occ_symbols=["AAPL260925C00230000"], as_of=NOW
        )
        option = snapshot.option_quotes["AAPL260925C00230000"]
        assert option.mark == "1.85"
        assert option.delta == "0.168"
        assert option.theta == "-0.061"

    def test_greeks_the_feed_did_not_send_stay_none(self) -> None:
        responses = {
            "get_option_snapshot": {
                "snapshots": {"AAPL260925C00230000": {"latestQuote": {"bp": "1.80", "ap": "1.90"}}}
            }
        }
        snapshot = broker(responses).get_market_snapshot(
            symbols=[], occ_symbols=["AAPL260925C00230000"], as_of=NOW
        )
        assert snapshot.option_quotes["AAPL260925C00230000"].delta is None

    def test_the_snapshot_is_as_fresh_as_its_freshest_quote_not_as_the_callers_clock(self) -> None:
        """REQ-34. Stamping ``as_of`` from the clock would make every read look like a state change
        to the execution gate, which is how a revalidation stops telling the operator anything."""
        responses = {
            "get_stock_latest_quote": {
                "quotes": {"AAPL": {"bp": "1", "ap": "2", "t": "2026-09-02T17:39:55Z"}}
            }
        }
        snapshot = broker(responses).get_market_snapshot(symbols=["AAPL"], as_of=NOW)
        assert snapshot.as_of.startswith("2026-09-02T17:39:55")

    def test_asking_for_nothing_calls_nothing(self) -> None:
        adapter = broker({})
        adapter.get_market_snapshot(symbols=[], occ_symbols=[], as_of=NOW)
        assert adapter.client.calls == []


class TestOrders:
    ORDER = {
        "id": "3869a51a-e26a-4d21-8a74-21a3dfba6e80",
        "client_order_id": "mz1-abcdef",
        "status": "accepted",
        "submitted_at": "2026-09-02T17:40:01Z",
        "filled_qty": "0",
        "filled_avg_price": None,
    }

    def test_find_order_maps_the_idempotency_read(self) -> None:
        order = broker({"get_order_by_client_id": self.ORDER}).find_order("mz1-abcdef")
        assert order is not None
        assert order.broker_order_id == "3869a51a-e26a-4d21-8a74-21a3dfba6e80"
        assert order.status == "accepted"
        assert order.filled_quantity == "0"

    def test_a_404_means_no_such_order_rather_than_an_outage(self) -> None:
        assert broker({}, errors={"get_order_by_client_id"}).find_order("nope") is None

    def test_an_order_without_identifiers_is_refused(self) -> None:
        with pytest.raises(BrokerError, match="without identifiers"):
            broker({"get_order_by_client_id": {"status": "accepted"}}).find_order("x")


class TestSubmission:
    @staticmethod
    def request(legs: list[dict[str, Any]], **kwargs: Any) -> OrderRequest:
        return OrderRequest(
            client_order_id="mz1-test",
            symbol=kwargs.pop("symbol", "AAPL"),
            asset_class=kwargs.pop("asset_class", "equity_option"),
            intent=kwargs.pop("intent", "open"),
            legs=[
                {
                    "leg_index": index,
                    "symbol": "AAPL",
                    "occ_symbol": None,
                    "side": "buy",
                    "quantity": "1",
                    "order_type": "limit",
                    "limit_price": None,
                    "contract_type": None,
                    "strike": None,
                    "expiry": None,
                    **leg,
                }
                for index, leg in enumerate(legs)
            ],
            **kwargs,
        )

    ACCEPTED = {
        "get_account_info": ACCOUNT,
        "place_option_order": TestOrders.ORDER,
        "place_stock_order": TestOrders.ORDER,
    }

    def test_a_spread_goes_as_ONE_mleg_order_at_the_gcd_ratio(self) -> None:
        """Two single-leg orders have a window in which the short fills and the long does not - which
        is the undefined-risk position ``structure_valid`` refuses at decision time."""
        adapter = broker(self.ACCEPTED)
        adapter.submit_order(
            self.request(
                [
                    {"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "4",
                     "limit_price": "2.40", "contract_type": "call", "strike": "230",
                     "expiry": "2026-09-25"},
                    {"occ_symbol": "AAPL260925C00235000", "side": "sell", "quantity": "4",
                     "limit_price": "1.00", "contract_type": "call", "strike": "235",
                     "expiry": "2026-09-25"},
                ]
            )
        )
        submissions = [c for c in adapter.client.calls if c[0] == "place_option_order"]
        assert len(submissions) == 1, "a spread must be one atomic order"
        arguments = submissions[0][1]
        assert arguments["order_class"] == "mleg"
        # A 4:4 spread is FOUR 1:1 spreads, never one 4:4 order.
        assert arguments["qty"] == "4"
        assert [leg["ratio_qty"] for leg in arguments["legs"]] == ["1", "1"]
        # The net limit is PER SPREAD: debit paid minus credit received, at the ratios.
        assert Decimal(arguments["limit_price"]) == Decimal("1.40")
        assert arguments["type"] == "limit"

    def test_the_position_intent_follows_the_proposals_intent(self) -> None:
        adapter = broker(self.ACCEPTED)
        adapter.submit_order(
            self.request(
                [
                    {"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "1",
                     "contract_type": "call", "strike": "230", "expiry": "2026-09-25",
                     "order_type": "market"},
                    {"occ_symbol": "AAPL260925C00235000", "side": "sell", "quantity": "1",
                     "contract_type": "call", "strike": "235", "expiry": "2026-09-25",
                     "order_type": "market"},
                ],
                intent="close",
            )
        )
        legs = [c for c in adapter.client.calls if c[0] == "place_option_order"][0][1]["legs"]
        assert [leg["position_intent"] for leg in legs] == ["buy_to_close", "sell_to_close"]

    def test_a_partially_priced_spread_goes_to_market_rather_than_half_limited(self) -> None:
        """A spread priced on only some of its legs is not a limit order, and treating it as one lets
        the unpriced side fill at any price."""
        adapter = broker(self.ACCEPTED)
        adapter.submit_order(
            self.request(
                [
                    {"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "1",
                     "limit_price": "2.40", "contract_type": "call", "strike": "230",
                     "expiry": "2026-09-25"},
                    {"occ_symbol": "AAPL260925C00235000", "side": "sell", "quantity": "1",
                     "order_type": "market", "contract_type": "call", "strike": "235",
                     "expiry": "2026-09-25"},
                ]
            )
        )
        arguments = [c for c in adapter.client.calls if c[0] == "place_option_order"][0][1]
        assert arguments["type"] == "market"
        assert "limit_price" not in arguments

    def test_the_paper_account_is_re_asked_at_the_moment_of_submission(self) -> None:
        adapter = broker(self.ACCEPTED)
        adapter.submit_order(
            self.request(
                [{"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "1",
                  "limit_price": "1.85", "contract_type": "call", "strike": "230",
                  "expiry": "2026-09-25"}]
            )
        )
        names = [name for name, _ in adapter.client.calls]
        assert names.index("get_account_info") < names.index("place_option_order")

    def test_an_equity_order_uses_the_equity_tool(self) -> None:
        adapter = broker(self.ACCEPTED)
        adapter.submit_order(
            self.request(
                [{"side": "buy", "quantity": "10", "limit_price": "228.50"}],
                asset_class="equity",
            )
        )
        assert any(name == "place_stock_order" for name, _ in adapter.client.calls)

    def test_a_fractional_contract_is_refused(self) -> None:
        adapter = broker(self.ACCEPTED)
        with pytest.raises(BrokerError, match="whole"):
            adapter.submit_order(
                self.request(
                    [{"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "0.5",
                      "limit_price": "1.85", "contract_type": "call", "strike": "230",
                      "expiry": "2026-09-25"}]
                )
            )

    def test_a_broker_refusal_becomes_a_machine_code_not_a_stack_trace(self) -> None:
        adapter = broker({**self.ACCEPTED}, errors={"place_option_order"})
        with pytest.raises(BrokerError, match="rejected the order"):
            adapter.submit_order(
                self.request(
                    [{"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "1",
                      "limit_price": "1.85", "contract_type": "call", "strike": "230",
                      "expiry": "2026-09-25"}]
                )
            )


class TestNetLimitArithmetic:
    """Alpaca prices an mleg order PER SPREAD and multiplies by ``qty``. Summing at absolute leg
    quantities would submit N times the intended debit - a 2-lot of a 2.40 spread going in at 4.80."""

    def test_a_two_lot_of_a_one_forty_spread_is_priced_at_one_forty(self) -> None:
        request = TestSubmission.request(
            [
                {"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "2", "limit_price": "2.40",
                 "contract_type": "call", "strike": "230", "expiry": "2026-09-25"},
                {"occ_symbol": "AAPL260925C00235000", "side": "sell", "quantity": "2", "limit_price": "1.00",
                 "contract_type": "call", "strike": "235", "expiry": "2026-09-25"},
            ]
        )
        assert _net_limit_price(request, spreads=2) == Decimal("1.40")

    def test_a_credit_spread_prices_negative(self) -> None:
        request = TestSubmission.request(
            [
                {"occ_symbol": "AAPL260925C00230000", "side": "sell", "quantity": "1", "limit_price": "2.40",
                 "contract_type": "call", "strike": "230", "expiry": "2026-09-25"},
                {"occ_symbol": "AAPL260925C00235000", "side": "buy", "quantity": "1", "limit_price": "1.00",
                 "contract_type": "call", "strike": "235", "expiry": "2026-09-25"},
            ]
        )
        assert _net_limit_price(request, spreads=1) == Decimal("-1.40")

    def test_an_unpriced_leg_makes_the_whole_spread_unpriced(self) -> None:
        request = TestSubmission.request(
            [
                {"occ_symbol": "AAPL260925C00230000", "side": "buy", "quantity": "1", "limit_price": "2.40",
                 "contract_type": "call", "strike": "230", "expiry": "2026-09-25"},
                {"occ_symbol": "AAPL260925C00235000", "side": "sell", "quantity": "1", "order_type": "market",
                 "contract_type": "call", "strike": "235", "expiry": "2026-09-25"},
            ]
        )
        assert _net_limit_price(request, spreads=1) is None
