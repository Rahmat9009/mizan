"""The context provider and the mock broker: where the engine's inputs come from, and where they don't.

Finding F-1 was a gate bypassed by price poisoning - the caller-supplied estimated price was the only
valuation input, so a thousand shares claimed at a cent passed every notional rule. The structural
answer is that the proposal contributes *which* symbols to fetch and nothing else; the numbers come
from the broker. These tests make that observable: the same proposal at an absurd limit price produces
a byte-identical context, and a broker that cannot answer produces no context at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mizan.adapters import (
    BrokerContextProvider,
    BrokerOrder,
    ContextProvider,
    MockBroker,
    OrderRequest,
)
from mizan.contracts import AuthorizedLeg, canonical_json, format_ts
from mizan.contracts.errors import BrokerError
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    make_market_snapshot,
    make_option_proposal,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

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


def a_broker(**overrides) -> MockBroker:
    return MockBroker(
        portfolio_snapshot=overrides.pop("portfolio_snapshot", make_portfolio_snapshot()),
        market_snapshot=overrides.pop("market_snapshot", make_market_snapshot()),
        **overrides,
    )


def build(provider, *, proposal=None, policy=None, now=FIXED_NOW):
    policy = policy if policy is not None else make_policy()
    proposal = proposal if proposal is not None else make_proposal()
    return provider.build(
        tenant_id=policy.tenant_id, agent_id=AGENT_ID, proposal=proposal, policy=policy, now=now
    )


# ---------------------------------------------------------------------------------------------
# BrokerContextProvider
# ---------------------------------------------------------------------------------------------
def test_it_satisfies_the_context_provider_protocol():
    assert isinstance(BrokerContextProvider(a_broker()), ContextProvider)


def test_the_context_carries_the_brokers_snapshots_and_the_policy_reference():
    policy = make_policy()
    context = build(BrokerContextProvider(a_broker()), policy=policy)

    assert context.tenant_id == policy.tenant_id
    assert context.agent_id == AGENT_ID
    assert context.policy.hash == policy.policy_hash
    assert context.evaluated_at == format_ts(FIXED_NOW)
    assert context.market_snapshot == make_market_snapshot()
    assert context.portfolio_snapshot == make_portfolio_snapshot()
    assert context.response_level == 0


def test_valuation_data_comes_from_the_broker_and_never_from_the_proposal(recwarn):
    """F-1: the proposal's own price is not an input. Two absurd limits, one identical context."""
    honest = make_proposal()
    poisoned = make_proposal(
        legs=[
            {
                "leg_index": 0,
                "side": "buy",
                "contract_type": None,
                "strike": None,
                "expiry": None,
                "quantity": "10",
                "limit_price": "0.01",
                "order_type": "limit",
            }
        ]
    )
    provider = BrokerContextProvider(a_broker())
    honest_context = build(provider, proposal=honest)
    poisoned_context = build(provider, proposal=poisoned)

    quoted = honest_context.market_snapshot.quotes["AAPL"].price
    assert poisoned_context.market_snapshot.quotes["AAPL"].price == quoted
    assert Decimal(quoted) > Decimal("100"), "the venue's price, not the one cent the agent claimed"
    # the contexts differ in nothing at all: the proposal contributes no valuation input
    assert canonical_json(honest_context) == canonical_json(poisoned_context)


def test_the_context_id_is_derived_from_the_content_so_a_changed_world_is_visibly_a_new_context():
    provider = BrokerContextProvider(a_broker())
    first = build(provider)
    again = build(provider)
    assert first.context_id == again.context_id

    provider.broker.set_portfolio_snapshot(make_portfolio_snapshot(buying_power="1000"))
    moved = build(provider)
    assert moved.context_id != first.context_id


def test_it_asks_for_the_proposal_symbol_and_every_held_symbol_so_the_book_can_be_valued():
    asked: dict[str, list[str]] = {}

    class Recording(MockBroker):
        def get_market_snapshot(self, *, symbols, occ_symbols=(), as_of):
            asked["symbols"] = list(symbols)
            asked["occ"] = list(occ_symbols)
            return super().get_market_snapshot(symbols=symbols, occ_symbols=occ_symbols, as_of=as_of)

    broker = Recording(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    build(BrokerContextProvider(broker))
    assert asked["symbols"] == ["AAPL", "MSFT"], asked
    assert asked["occ"] == []


def test_an_option_proposal_asks_for_its_occ_symbols():
    asked: dict[str, list[str]] = {}

    class Recording(MockBroker):
        def get_market_snapshot(self, *, symbols, occ_symbols=(), as_of):
            asked["occ"] = list(occ_symbols)
            return super().get_market_snapshot(symbols=symbols, occ_symbols=occ_symbols, as_of=as_of)

    broker = Recording(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    build(BrokerContextProvider(broker), proposal=make_option_proposal())
    assert asked["occ"] == ["AAPL260925C00230000"], asked


def test_a_broker_that_cannot_answer_produces_no_context_at_all(monkeypatch):
    """E2: the provider does not fabricate an empty portfolio to keep the pipeline moving."""
    broker = MockBroker(portfolio_snapshot=None, market_snapshot=make_market_snapshot())
    with pytest.raises(BrokerError):
        build(BrokerContextProvider(broker))


def test_addendum_one_state_is_injectable_as_a_value_or_as_a_callable():
    """ADR-0006: path, aggregate, agent and calendar state are INPUTS. Sprint 3 swaps the callable in."""
    from tests.fixtures import make_agent_state, make_aggregate_state, make_calendar, make_path_state

    static = BrokerContextProvider(
        a_broker(),
        path_state=make_path_state(),
        aggregate_state=make_aggregate_state(),
        agent_state=make_agent_state(),
        calendar=make_calendar(),
        response_level=2,
    )
    context = build(static)
    assert context.path_state == make_path_state()
    assert context.aggregate_state == make_aggregate_state()
    assert context.agent_state == make_agent_state()
    assert context.calendar == make_calendar()
    assert context.response_level == 2

    level = {"value": 0}
    dynamic = BrokerContextProvider(a_broker(), response_level=lambda: level["value"])
    assert build(dynamic).response_level == 0
    level["value"] = 4
    assert build(dynamic).response_level == 4


def test_state_the_deployment_cannot_derive_yet_stays_absent_rather_than_zero():
    """The Sprint-3 seam, fail-closed: absent state is None, and a policy needing it will block (E2)."""
    context = build(BrokerContextProvider(a_broker()))
    assert context.path_state is None
    assert context.aggregate_state is None
    assert context.agent_state is None
    assert context.calendar is None


def test_recent_orders_are_passed_through_verbatim():
    from mizan.contracts import RecentOrder

    order = RecentOrder(
        proposal_id="a" * 64,
        symbol="AAPL",
        side="buy",
        total_quantity="10",
        submitted_at=format_ts(FIXED_NOW - timedelta(seconds=30)),
        status="accepted",
    )
    policy = make_policy()
    context = BrokerContextProvider(a_broker()).build(
        tenant_id=policy.tenant_id,
        agent_id=AGENT_ID,
        proposal=make_proposal(),
        policy=policy,
        now=FIXED_NOW,
        recent_orders=[order],
    )
    assert context.recent_orders == [order]


# ---------------------------------------------------------------------------------------------
# MockBroker
# ---------------------------------------------------------------------------------------------
def test_the_mock_broker_has_no_cancel_replace_or_close_path():
    for method in MUTATION_METHODS:
        assert not hasattr(MockBroker, method), method
    assert MockBroker.environment == "paper"


def test_it_records_submissions_and_finds_them_again_by_client_order_id():
    broker = a_broker()
    request = OrderRequest(
        client_order_id="mz1-" + "d" * 40,
        symbol="AAPL",
        asset_class="equity",
        intent="open",
        legs=[
            AuthorizedLeg(
                leg_index=0,
                side="buy",
                symbol="AAPL",
                occ_symbol=None,
                contract_type=None,
                strike=None,
                expiry=None,
                quantity="10",
                limit_price="228.50",
                order_type="limit",
            )
        ],
    )
    assert broker.find_order(request.client_order_id) is None

    order = broker.submit_order(request)
    assert isinstance(order, BrokerOrder)
    assert broker.submitted == [request]
    assert broker.find_order(request.client_order_id) == order
    assert broker.get_order(order.broker_order_id) == order
    assert broker.log == ["broker.find_order", "broker.submit_order", "broker.find_order",
                          "broker.get_order"]


def test_hooks_fire_inside_the_call_so_a_test_can_move_the_world_mid_read():
    broker = a_broker()
    moved = make_portfolio_snapshot(buying_power="1")
    broker.on_portfolio_read = lambda: broker.set_portfolio_snapshot(moved)

    first = broker.get_portfolio_snapshot(as_of=datetime.now(UTC))
    assert first == moved, "the hook ran before the value was returned"


def test_missing_data_raises_rather_than_returning_an_empty_snapshot():
    empty = MockBroker()
    with pytest.raises(BrokerError):
        empty.get_portfolio_snapshot(as_of=FIXED_NOW)
    with pytest.raises(BrokerError):
        empty.get_market_snapshot(symbols=["AAPL"], as_of=FIXED_NOW)
    with pytest.raises(BrokerError):
        empty.get_order("nope")


def test_a_seeded_order_is_what_the_idempotency_step_will_find():
    broker = a_broker()
    seeded = broker.seed_order(
        BrokerOrder(
            broker_order_id="pre-existing-1",
            client_order_id="mz1-" + "e" * 40,
            status="accepted",
            submitted_at=format_ts(FIXED_NOW),
        )
    )
    assert broker.find_order(seeded.client_order_id) == seeded
    assert broker.submitted == []
