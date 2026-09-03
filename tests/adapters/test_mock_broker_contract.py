"""``MockBroker`` is the broker every other lane will build against, so it is tested like shipped code.

Two properties matter more than the rest. It must satisfy the ``BrokerAdapter`` Protocol *exactly* -
no extra mutation, no missing read - because a test double that is more capable than the real thing
lets a lane write code the real adapter cannot run. And it must **raise** on missing data rather than
return an empty snapshot: a broker that answers "no positions" when it means "I could not tell you"
turns a fail-closed engine into a fail-open one (E2).

Self-contained: this module builds everything it uses.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mizan.adapters import BrokerAdapter, BrokerOrder, MockBroker, OrderRequest
from mizan.contracts.errors import BrokerError
from tests.fixtures import (
    FIXED_NOW,
    FIXED_NOW_STR,
    make_authorization,
    make_market_snapshot,
    make_portfolio_snapshot,
)

#: Anything that could change or unwind a position. The Protocol has no vocabulary for these (B4).
MUTATION_METHODS = (
    "cancel_order",
    "cancel_orders",
    "cancel_all_orders",
    "replace_order",
    "modify_order",
    "close_position",
    "close_all_positions",
    "liquidate",
)


def a_broker(**kwargs) -> MockBroker:
    kwargs.setdefault("portfolio_snapshot", make_portfolio_snapshot())
    kwargs.setdefault("market_snapshot", make_market_snapshot())
    return MockBroker(**kwargs)


def an_order_request(client_order_id: str = "mz1-test-order") -> OrderRequest:
    auth = make_authorization()
    return OrderRequest(
        client_order_id=client_order_id,
        symbol=auth.scope.symbol,
        asset_class=auth.scope.asset_class,
        intent=auth.scope.intent,
        legs=list(auth.scope.legs),
    )


# ---------------------------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------------------------
def test_the_mock_satisfies_the_broker_protocol_and_is_paper_only():
    broker = a_broker()
    assert isinstance(broker, BrokerAdapter)
    assert broker.environment == "paper"
    assert broker.name


def test_the_mock_has_no_cancel_replace_or_close_path():
    """B4: the double must not offer a capability the real adapter is forbidden to have."""
    broker = a_broker()
    for method in MUTATION_METHODS:
        assert not hasattr(broker, method), method


def test_every_read_and_the_one_mutation_are_recorded_in_call_order():
    broker = a_broker()
    broker.get_portfolio_snapshot(as_of=FIXED_NOW)
    broker.get_market_snapshot(symbols=["AAPL"], as_of=FIXED_NOW)
    broker.find_order("mz1-absent")
    broker.submit_order(an_order_request())
    assert broker.log == [
        "broker.get_portfolio_snapshot",
        "broker.get_market_snapshot",
        "broker.find_order",
        "broker.submit_order",
    ]


# ---------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------
def test_the_snapshots_come_back_exactly_as_they_were_scripted():
    portfolio, market = make_portfolio_snapshot(), make_market_snapshot()
    broker = MockBroker(portfolio_snapshot=portfolio, market_snapshot=market)
    assert broker.get_portfolio_snapshot(as_of=FIXED_NOW) is portfolio
    assert broker.get_market_snapshot(symbols=["AAPL"], as_of=FIXED_NOW) is market


def test_a_missing_portfolio_raises_rather_than_returning_an_empty_one():
    """E2: absent state is an error to be handled, never a zero to be computed with."""
    broker = MockBroker(portfolio_snapshot=None, market_snapshot=make_market_snapshot())
    with pytest.raises(BrokerError) as failure:
        broker.get_portfolio_snapshot(as_of=FIXED_NOW)
    assert "PORTFOLIO_STATE_MISSING" in {code.value for code in failure.value.reason_codes}


def test_missing_market_data_raises_rather_than_returning_an_empty_snapshot():
    broker = MockBroker(portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=None)
    with pytest.raises(BrokerError) as failure:
        broker.get_market_snapshot(symbols=["AAPL"], as_of=FIXED_NOW)
    assert "MARKET_DATA_MISSING" in {code.value for code in failure.value.reason_codes}


def test_a_read_hook_fires_inside_the_call_so_a_test_can_move_the_world_mid_read():
    """This is what makes a TOCTOU test possible: the state changes while the gate is reading it."""
    broker = a_broker()
    starved = make_portfolio_snapshot(buying_power="0")
    broker.on_portfolio_read = lambda: broker.set_portfolio_snapshot(starved)
    assert broker.get_portfolio_snapshot(as_of=FIXED_NOW) is starved


def test_find_order_answers_none_until_an_order_exists_under_that_key():
    broker = a_broker()
    assert broker.find_order("mz1-nothing-here") is None
    seeded = broker.seed_order(
        BrokerOrder(
            broker_order_id="seed-1",
            client_order_id="mz1-nothing-here",
            status="accepted",
            submitted_at=FIXED_NOW_STR,
        )
    )
    assert broker.find_order("mz1-nothing-here") is seeded


def test_get_order_finds_by_broker_id_and_refuses_an_unknown_one():
    broker = a_broker()
    order = broker.submit_order(an_order_request())
    assert broker.get_order(order.broker_order_id) == order
    with pytest.raises(BrokerError):
        broker.get_order("no-such-order")


# ---------------------------------------------------------------------------------------------
# The one mutation
# ---------------------------------------------------------------------------------------------
def test_submitting_records_the_request_and_returns_a_broker_order_keyed_by_the_client_id():
    broker = a_broker()
    request = an_order_request("mz1-first")
    order = broker.submit_order(request)

    assert broker.submitted == [request]
    assert isinstance(order, BrokerOrder)
    assert order.client_order_id == "mz1-first"
    assert order.broker_order_id
    assert order.status == "accepted"
    assert order.filled_quantity == "0"
    assert broker.find_order("mz1-first") is order


def test_each_submission_gets_its_own_broker_order_id():
    broker = a_broker()
    first = broker.submit_order(an_order_request("mz1-a"))
    second = broker.submit_order(an_order_request("mz1-b"))
    assert first.broker_order_id != second.broker_order_id
    assert len(broker.submitted) == 2


def test_a_scripted_failure_raises_and_records_nothing_as_submitted():
    """An unreachable venue must not look like a rejected order, and must not look like a success."""
    broker = a_broker(fail_with=BrokerError("venue down", reason_codes=["BROKER_UNAVAILABLE"]))
    with pytest.raises(BrokerError):
        broker.submit_order(an_order_request())
    assert broker.submitted == []


def test_an_order_request_cannot_be_built_for_anything_but_paper():
    with pytest.raises(ValidationError):
        OrderRequest(
            client_order_id="mz1-test",
            symbol="AAPL",
            asset_class="equity",
            intent="open",
            legs=list(make_authorization().scope.legs),
            environment="not-paper",
        )
