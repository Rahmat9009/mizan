"""The paper boundary, proven the only way that counts: with the network switched off.

Hard Rule B1 says paper and live are separate boundaries, not a configuration flag, and finding F-19
says an adapter that merely *claims* paper mode proves nothing. So:

* every falsy, empty, absent or hostile spelling of ``ALPACA_PAPER`` raises ``LiveTradingForbidden``,
  and ``socket`` is monkeypatched to explode if anything reaches for it - the refusal happens before a
  credential is read and long before a connection could be opened;
* the base URL is re-derived from the client object at construction *and* again immediately before
  every submission, so a client swapped after construction is caught at the mutation, not trusted;
* the adapter has no cancel, replace or close method at all (B4).

No real credential appears anywhere here. The values are obvious placeholders and are shaped so the
repository's secret scanner has nothing to find.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mizan.adapters import PAPER_HOST, AlpacaPaperBroker, OrderRequest
from mizan.contracts import AuthorizedLeg
from mizan.contracts.errors import BrokerError, ConfigurationError, LiveTradingForbidden

PAPER_BASE_URL = f"https://{PAPER_HOST}"
FORBIDDEN = ("false", "0", "no", "", "off", "False", "FALSE", "maybe", "yes-please", "paper")
CREDENTIAL_VARIABLES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
MUTATION_METHODS = (
    "cancel_order",
    "cancel_order_by_id",
    "cancel_orders",
    "cancel_all_orders",
    "replace_order",
    "replace_order_by_id",
    "modify_order",
    "close_position",
    "close_all_positions",
    "liquidate",
)
NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# doubles for the SDK objects the adapter maps
# ---------------------------------------------------------------------------------------------
class FakeClient:
    """The shape of an alpaca-py TradingClient, with none of its behaviour."""

    def __init__(self, base_url: str = PAPER_BASE_URL) -> None:
        self._base_url = base_url
        # account_number carries Alpaca's paper prefix: the SECOND paper signal. A fake without one
        # is a fake of a LIVE account, which is exactly what the adapter must refuse.
        self.account = _obj(equity="100000.00", cash="79395.12", buying_power="158790.24",
                            maintenance_margin="10302.50", account_number="PA3ABCDEFGHI")
        self.positions: list[Any] = []
        self.orders: dict[str, Any] = {}
        self.submitted: list[Any] = []

    def get_account(self) -> Any:
        return self.account

    def get_all_positions(self) -> list[Any]:
        return self.positions

    def get_order_by_client_id(self, client_order_id: str) -> Any:
        found = self.orders.get(client_order_id)
        if found is None:
            raise _NotFound()
        return found

    def get_order_by_id(self, broker_order_id: str) -> Any:
        for order in self.orders.values():
            if order.id == broker_order_id:
                return order
        raise _NotFound()

    def submit_order(self, order_data: Any) -> Any:
        self.submitted.append(order_data)
        order = _obj(
            id=f"broker-{len(self.submitted)}",
            client_order_id=order_data.client_order_id,
            status=_Status("accepted"),
            submitted_at=NOW,
            filled_qty="0",
            filled_avg_price=None,
        )
        self.orders[order_data.client_order_id] = order
        return order


class _NotFound(Exception):
    status_code = 404


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


def _obj(**fields: Any) -> Any:
    return type("Row", (), fields)()


def _leg(**overrides: Any) -> AuthorizedLeg:
    base: dict[str, Any] = {
        "leg_index": 0,
        "side": "buy",
        "symbol": "AAPL",
        "occ_symbol": None,
        "contract_type": None,
        "strike": None,
        "expiry": None,
        "quantity": "10",
        "limit_price": "228.50",
        "order_type": "limit",
    }
    return AuthorizedLeg.model_validate({**base, **overrides})


def _request(**overrides: Any) -> OrderRequest:
    base: dict[str, Any] = {
        "client_order_id": "mz1-" + "a" * 40,
        "symbol": "AAPL",
        "asset_class": "equity",
        "intent": "open",
        "legs": [_leg()],
    }
    return OrderRequest.model_validate({**base, **overrides})


def _block_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access attempted before the paper check")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _placeholder_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obvious placeholders. Never a real credential, not even a well-formed-looking one."""
    monkeypatch.setenv("ALPACA_API_KEY", "placeholder-not-a-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "placeholder-not-a-secret")


# ---------------------------------------------------------------------------------------------
# B1: the environment gate
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", FORBIDDEN)
def test_every_non_true_ALPACA_PAPER_is_refused_before_any_socket(value, monkeypatch):
    _block_the_network(monkeypatch)
    _placeholder_credentials(monkeypatch)
    monkeypatch.setenv("ALPACA_PAPER", value)

    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker.from_environment()


def test_an_unset_ALPACA_PAPER_is_not_permission(monkeypatch):
    _block_the_network(monkeypatch)
    _placeholder_credentials(monkeypatch)
    monkeypatch.delenv("ALPACA_PAPER", raising=False)

    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker.from_environment()


def test_the_paper_check_happens_before_credentials_are_even_read(monkeypatch):
    """Ordering: a live-configured environment must fail before a key is touched (B2/F-18)."""
    _block_the_network(monkeypatch)
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALPACA_PAPER", "false")

    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker.from_environment()  # not ConfigurationError: paper is checked first


def test_paper_true_without_credentials_is_a_configuration_error_not_a_live_fallback(monkeypatch):
    _block_the_network(monkeypatch)
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALPACA_PAPER", "true")

    with pytest.raises(ConfigurationError):
        AlpacaPaperBroker.from_environment()


def test_paper_true_with_credentials_builds_a_client_pointed_at_the_paper_host(monkeypatch):
    """The success path, asserted on the object itself: the base URL names the paper host and nothing else.

    Sockets are not blocked here because constructing the SDK client opens none - the assertion is on
    where the client is pointed, which is what a connection would eventually use.
    """
    _placeholder_credentials(monkeypatch)
    monkeypatch.setenv("ALPACA_PAPER", "true")

    broker = AlpacaPaperBroker.from_environment()

    assert broker.environment == "paper"
    base_url = broker._client._base_url
    assert PAPER_HOST in str(getattr(base_url, "value", base_url))
    assert broker._stock_data is not None and broker._option_data is not None


def test_a_client_pointed_anywhere_else_cannot_be_wrapped():
    for base_url in ("https://example.invalid", "https://data.alpaca.markets", ""):
        with pytest.raises(LiveTradingForbidden):
            AlpacaPaperBroker(FakeClient(base_url=base_url))


def test_the_paper_proof_is_re_derived_immediately_before_every_submission():
    """F-19: not a stored boolean. The object that will carry the request is re-checked at the mutation."""
    client = FakeClient()
    broker = AlpacaPaperBroker(client)
    assert broker.submit_order(_request()).broker_order_id == "broker-1"

    client._base_url = "https://example.invalid"  # swapped after construction
    with pytest.raises(LiveTradingForbidden):
        broker.submit_order(_request(client_order_id="mz1-" + "b" * 40))
    assert len(client.submitted) == 1, "the refused submission never reached the client"


def test_the_adapter_has_no_cancel_replace_or_close_path():
    for method in MUTATION_METHODS:
        assert not hasattr(AlpacaPaperBroker, method), method
    assert AlpacaPaperBroker.environment == "paper"


# ---------------------------------------------------------------------------------------------
# mapping: no SDK object escapes
# ---------------------------------------------------------------------------------------------
def test_the_portfolio_snapshot_is_contract_types_all_the_way_down():
    client = FakeClient()
    client.positions = [
        _obj(symbol="MSFT", asset_class=_Status("us_equity"), qty="50", market_value="20605.00"),
        _obj(symbol="AAPL260925C00230000", asset_class=_Status("us_option"), qty="5",
             market_value="925.00"),
    ]
    snapshot = AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW)

    assert snapshot.equity == "100000" and snapshot.cash == "79395.12"
    assert snapshot.buying_power == "158790.24"
    assert [position.symbol for position in snapshot.positions] == ["MSFT", "AAPL"]
    option = snapshot.positions[1]
    assert option.asset_class == "equity_option"
    assert option.occ_symbol == "AAPL260925C00230000"
    # E2: what the broker did not report stays absent rather than becoming a zero
    assert snapshot.peak_equity is None and snapshot.daily_pnl is None and snapshot.greeks is None
    assert all(isinstance(position.quantity, str) for position in snapshot.positions)


def test_a_missing_required_account_number_is_a_broker_error_not_a_zero():
    client = FakeClient()
    # account_number is present and valid: this test is about a missing NUMBER (equity/cash), and
    # without it the two-signal paper guard would fire first and mask what is being asserted.
    client.account = _obj(equity=None, cash=None, buying_power=None, account_number="PA3ABCDEFGHI")
    with pytest.raises(BrokerError):
        AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW)


def test_quotes_are_valued_at_the_midpoint_and_a_symbol_without_one_is_simply_absent():
    """F-1: the price comes from the venue's own two-sided quote, never from the caller's number."""

    class Data:
        def get_stock_latest_quote(self, request: Any) -> dict[str, Any]:
            return {
                "AAPL": _obj(bid_price=228.45, ask_price=228.55, timestamp=NOW),
                "MSFT": _obj(bid_price=0, ask_price=0, timestamp=NOW),
            }

    snapshot = AlpacaPaperBroker(FakeClient(), stock_data_client=Data()).get_market_snapshot(
        symbols=["AAPL", "MSFT"], as_of=NOW
    )
    assert set(snapshot.quotes) == {"AAPL"}, "a symbol with no usable quote is absent, not zero"
    assert Decimal(snapshot.quotes["AAPL"].price) == Decimal("228.50")
    assert snapshot.quotes["AAPL"].bid == "228.45" and snapshot.quotes["AAPL"].ask == "228.55"
    assert snapshot.source.startswith("alpaca")


def test_option_quotes_carry_the_greeks_the_engine_needs():
    class Data:
        def get_option_latest_quote(self, request: Any) -> dict[str, Any]:
            return {
                "AAPL260925C00230000": _obj(
                    bid_price=1.80, ask_price=1.90, timestamp=NOW,
                    greeks=_obj(delta=0.42, gamma=0.021, vega=0.142, theta=-0.061),
                )
            }

    snapshot = AlpacaPaperBroker(FakeClient(), option_data_client=Data()).get_market_snapshot(
        symbols=[], occ_symbols=["AAPL260925C00230000"], as_of=NOW
    )
    quote = snapshot.option_quotes["AAPL260925C00230000"]
    assert Decimal(quote.mark) == Decimal("1.85")
    assert quote.delta == "0.42" and quote.theta == "-0.061"


def test_no_data_client_means_no_quotes_rather_than_invented_ones():
    snapshot = AlpacaPaperBroker(FakeClient()).get_market_snapshot(symbols=["AAPL"], as_of=NOW)
    assert snapshot.quotes == {} and snapshot.option_quotes == {}


def test_find_order_maps_the_sdk_order_and_returns_None_for_an_unknown_key():
    client = FakeClient()
    broker = AlpacaPaperBroker(client)
    assert broker.find_order("mz1-" + "c" * 40) is None

    submitted = broker.submit_order(_request())
    found = broker.find_order(submitted.client_order_id)
    assert found is not None
    assert found.broker_order_id == submitted.broker_order_id
    assert found.status == "accepted"
    assert type(found).__module__.startswith("mizan."), "no SDK object escapes the adapter"


def test_a_broker_failure_becomes_one_machine_code_and_never_the_vendor_text():
    class Exploding(FakeClient):
        def get_account(self) -> Any:
            raise RuntimeError("connection refused to 10.1.2.3:443")

    with pytest.raises(BrokerError) as failure:
        AlpacaPaperBroker(Exploding()).get_portfolio_snapshot(as_of=NOW)
    assert "10.1.2.3" not in failure.value.message
    assert "BROKER_UNAVAILABLE" in {code.value for code in failure.value.reason_codes}


def test_a_multi_leg_request_is_submitted_atomically_not_refused():
    """This test's premise was inverted deliberately. It used to pin the adapter's REFUSAL of a
    multi-leg order, which was the honest behaviour while no mleg path existed - refusing beats
    submitting two singles and holding a naked leg between fills. Now the mleg path exists, so the
    spread goes as ONE atomic order and the refusal would be the bug. Full coverage lives in
    tests/adapters/test_multi_leg_submission.py."""
    from tests.adapters.test_multi_leg_submission import _spread

    client = FakeClient()
    AlpacaPaperBroker(client).submit_order(_spread())
    assert len(client.submitted) == 1
    assert str(client.submitted[0].order_class).endswith("MLEG")


@pytest.mark.parametrize(
    "account_number",
    [None, "", "   ", "123456789", "LIVE-123", "pa-lowercase", "XPA123"],
)
def test_an_account_that_does_not_identify_itself_as_paper_is_refused(account_number):
    """Signal 2. The base URL proves where the request GOES; it cannot prove what is waiting there.

    A correct paper host with a live account behind it passes the URL check, which is why one signal is
    not enough. Absent, empty or non-PA is refused - silence is not permission.
    """
    client = FakeClient()
    client.account = _obj(equity="1", cash="1", account_number=account_number)
    broker = AlpacaPaperBroker(client)
    with pytest.raises(LiveTradingForbidden):
        broker.get_portfolio_snapshot(as_of=NOW)


@pytest.mark.parametrize("account_number", [" PA123 ", "PA123\n", "\tPA123"])
def test_surrounding_whitespace_is_normalised_rather_than_treated_as_live(account_number):
    """Deliberate: an SDK that pads a field has not handed us a live account, and refusing on that
    would be a false positive on a safety control - which has its own cost. The prefix test runs on
    the trimmed value; anything that is not PA after trimming is still refused above."""
    client = FakeClient()
    client.account = _obj(equity="1", cash="1", account_number=account_number)
    assert AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW).equity == "1"


def test_both_signals_together_are_accepted():
    """Control: the refusals above must be caused by the account number, not by the check refusing all."""
    broker = AlpacaPaperBroker(FakeClient())
    snapshot = broker.get_portfolio_snapshot(as_of=NOW)
    assert snapshot.equity == "100000"


def test_the_account_signal_is_re_derived_at_the_mutation_boundary_too():
    """An account that stops looking like paper between the read and the submit must stop the submit."""
    client = FakeClient()
    broker = AlpacaPaperBroker(client)
    broker.get_portfolio_snapshot(as_of=NOW)  # passes: both signals agree

    client.account = _obj(equity="1", cash="1", account_number="LIVE-999")
    with pytest.raises(LiveTradingForbidden):
        broker.submit_order(_request())
    assert client.submitted == [], "nothing may reach the venue once a signal disagrees"


def test_a_paper_url_with_a_live_account_is_refused_on_the_second_signal():
    """The exact case one signal cannot catch, stated as its own test so it cannot be optimised away."""
    client = FakeClient(base_url=PAPER_BASE_URL)  # signal 1 agrees
    client.account = _obj(equity="1", cash="1", account_number="9876543210")  # signal 2 does not
    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW)


# ---------------------------------------------------------------------------------------------------
# M3: every numeric crosses the boundary as text, never as a float (INV-15)
# ---------------------------------------------------------------------------------------------------
def test_no_float_appears_anywhere_in_the_account_parse_path():
    """Alpaca returns every numeric as a STRING. Parsing it through float would silently reshape a
    price nobody quoted; a float anywhere in this path is an INV-15 violation."""
    import ast
    import inspect

    import mizan.adapters.alpaca_paper as module

    tree = ast.parse(inspect.getsource(module))
    offenders = [
        f"line {node.lineno}: float(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
    ]
    offenders += [
        f"line {node.lineno}: float literal {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert not offenders, f"the adapter must never touch a binary float: {offenders}"


def test_sdk_numerics_are_parsed_through_text_not_through_binary_floating_point():
    """The specific failure this prevents: 0.1 + 0.2 arithmetic on a quoted price."""
    client = FakeClient()
    client.account = _obj(
        equity="100000.10", cash="0.30", buying_power="0.10", account_number="PA3ABCDEFGHI"
    )
    snapshot = AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW)
    assert snapshot.equity == "100000.1"
    assert snapshot.cash == "0.3"
    assert snapshot.buying_power == "0.1"
    assert isinstance(snapshot.equity, str), "money crosses the boundary as text (A6)"


def test_a_float_from_a_misbehaving_sdk_is_still_parsed_through_its_text():
    """Defence in depth: if the SDK ever hands back a float, it goes through str() rather than being
    fed to Decimal directly, so the value is the one the vendor printed."""
    client = FakeClient()
    client.account = _obj(equity=100000.5, cash=0.25, account_number="PA3ABCDEFGHI")
    snapshot = AlpacaPaperBroker(client).get_portfolio_snapshot(as_of=NOW)
    assert snapshot.equity == "100000.5"
    assert snapshot.cash == "0.25"
