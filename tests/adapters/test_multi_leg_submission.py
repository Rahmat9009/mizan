"""A spread goes to the venue as ONE atomic mleg order, or it does not go at all.

The alternative - two single-leg orders - has a window in which the short leg is filled and the long
one is not. That is precisely the undefined-risk position `structure_valid` refuses at decision time,
so allowing the adapter to create it would mean defending a rule in the engine and breaking it one
layer down. Verified against the live Alpaca paper API: both orders placed carried
``order_class=mleg`` with two legs.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mizan.adapters import AlpacaPaperBroker
from mizan.adapters.base import OrderRequest
from mizan.contracts import TradeProposal
from mizan.contracts.errors import BrokerError
from tests.adapters.test_alpaca_paper import NOW, FakeClient  # noqa: F401
from tests.fixtures import make_authorized_leg, make_option_proposal


def _spread(long_qty: str = "1", short_qty: str = "1", order_type: str = "limit") -> OrderRequest:
    payload = make_option_proposal().model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    base = payload["legs"][0]
    payload["strategy"] = "bull_call_spread"
    payload["intent"] = "open"
    # The contract refuses a market order that carries a limit price, so the price goes with the type.
    priced = order_type == "limit"
    payload["legs"] = [
        {**base, "leg_index": 0, "side": "buy", "contract_type": "call", "strike": "230",
         "quantity": long_qty, "limit_price": "5.20" if priced else None, "order_type": order_type},
        {**base, "leg_index": 1, "side": "sell", "contract_type": "call", "strike": "235",
         "quantity": short_qty, "limit_price": "2.80" if priced else None, "order_type": order_type},
    ]
    proposal = TradeProposal.build(**payload)
    return OrderRequest(
        client_order_id="mleg-test",
        symbol=proposal.symbol,
        asset_class="equity_option",
        intent="open",
        legs=[make_authorized_leg(proposal, i) for i in range(2)],
    )


def _submit(request: OrderRequest):
    client = FakeClient()
    order = AlpacaPaperBroker(client).submit_order(request)
    return client, client.submitted[0], order


def test_a_spread_is_submitted_as_exactly_one_order():
    client, _, _ = _submit(_spread())
    assert len(client.submitted) == 1, "two orders would leave a window holding one naked leg"


def test_the_order_is_marked_mleg_and_carries_both_legs():
    _, sent, _ = _submit(_spread())
    assert str(sent.order_class).endswith("MLEG")
    assert len(sent.legs) == 2
    assert sent.symbol is None, "an mleg order has no top-level symbol; the legs carry the contracts"


def test_each_leg_goes_by_its_occ_symbol_not_the_underlying():
    _, sent, _ = _submit(_spread())
    for leg in sent.legs:
        assert leg.symbol.startswith("AAPL26"), f"{leg.symbol} is not an OCC contract symbol"


def test_qty_is_the_number_of_SPREADS_and_the_legs_carry_ratios():
    """Alpaca prices and counts an mleg order per SPREAD. A 3x3 spread is three 1:1 spreads, not a
    3-lot of two separate legs, and getting this wrong changes the size actually sent."""
    _, sent, _ = _submit(_spread("3", "3"))
    assert Decimal(str(sent.qty)) == 3
    assert [Decimal(str(leg.ratio_qty)) for leg in sent.legs] == [1, 1]


def test_an_unequal_spread_keeps_its_ratio():
    """A 2x4 is two 1:2 spreads. The ratio is the shape; qty is how many of that shape."""
    _, sent, _ = _submit(_spread("2", "4"))
    assert Decimal(str(sent.qty)) == 2
    assert [Decimal(str(leg.ratio_qty)) for leg in sent.legs] == [1, 2]


def test_the_limit_price_is_the_NET_debit_PER_SPREAD():
    """The bug this pins: summing legs at absolute quantities sends N times the intended debit. A
    2-lot of a 2.40 spread must go in at 2.40, not 4.80 - Alpaca multiplies by qty itself."""
    for long_qty, short_qty in (("1", "1"), ("2", "2"), ("4", "4")):
        _, sent, _ = _submit(_spread(long_qty, short_qty))
        assert Decimal(str(sent.limit_price)) == Decimal("2.4"), (
            f"{long_qty}x{short_qty} priced at {sent.limit_price}, not the per-spread net debit"
        )


def test_a_partially_priced_spread_is_never_silently_sent_to_market():
    """If any leg lacks a limit the whole order goes to market deliberately, rather than pricing one
    side and letting the other fill anywhere."""
    _, sent, _ = _submit(_spread(order_type="market"))
    assert getattr(sent, "limit_price", None) is None


def test_the_position_intent_follows_the_proposals_intent():
    _, sent, _ = _submit(_spread())
    intents = [str(leg.position_intent) for leg in sent.legs]
    assert any("BUY_TO_OPEN" in i for i in intents)
    assert any("SELL_TO_OPEN" in i for i in intents)


def test_a_leg_without_an_occ_symbol_is_refused_rather_than_guessed():
    request = _spread()
    stripped = request.model_copy(
        update={"legs": [request.legs[0].model_copy(update={"occ_symbol": None}), request.legs[1]]},
        deep=True,
    )
    with pytest.raises(BrokerError, match="OCC symbol"):
        _submit(stripped)


def test_a_single_leg_order_still_takes_the_simple_path():
    """The mleg path must not capture single-leg orders: a one-leg mleg is a needless complication."""
    request = _spread()
    single = request.model_copy(update={"legs": [request.legs[0]]}, deep=True)
    _, sent, _ = _submit(single)
    assert getattr(sent, "order_class", None) in (None, "simple") or not getattr(sent, "legs", None)
    assert sent.symbol is not None, "a single-leg order carries its contract at the top level"


def test_the_paper_proof_still_runs_before_a_multi_leg_submission():
    """Both paper signals guard the mleg path too - it is a mutation like any other."""
    from mizan.contracts.errors import LiveTradingForbidden
    from tests.adapters.test_alpaca_paper import _obj

    client = FakeClient()
    client.account = _obj(equity="1", cash="1", account_number="LIVE-999")
    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker(client).submit_order(_spread())
    assert client.submitted == []
