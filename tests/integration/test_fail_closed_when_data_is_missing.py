"""Missing data through the real broker seam, not through a hand-built context.

Invariants 03, 04 and 05 prove the ENGINE blocks when a snapshot is absent from the context it is
handed. That is the right thing for them to prove and it is not the same question as this one: in a
deployment nobody hands the engine a context, a broker does, and the interesting failures are the
ones where the broker answers partially, answers late, or does not answer at all.

Three shapes are separated here because they behave differently and only two of them are recorded:

1. the snapshot arrives but the quote for the traded symbol is missing -> blocking REJECT, recorded;
2. the snapshot arrives but the account is unreadable -> blocking REJECT, recorded;
3. the broker refuses the read outright -> an exception, and NO decision record at all.

The third is fail-closed (nothing is authorized and nothing is submitted) but it is not fail-*visible*:
the tenant's chain has no trace that an agent asked and could not be judged. It is asserted here as
the behaviour that ships, and raised to L2b/L3 as ledger/requests.md REQ-20.
"""

from __future__ import annotations

import pytest

from mizan.contracts.errors import BrokerError
from tests.integration._world import build_world, market_snapshot, portfolio_snapshot, proposal


def test_a_missing_quote_for_the_traded_symbol_blocks_and_is_recorded(tmp_path):
    world = build_world(ledger_dir=tmp_path, market=market_snapshot(quotes={}))

    record = world.mizan.evaluate(proposal("10"))

    assert record.verdict == "REJECT", (record.verdict, world.codes(record))
    assert "PRICE_MISSING" in world.codes(record)
    assert record.risk_evaluation.data_complete is False
    assert record.authorization is None
    assert world.broker.submitted == []
    # the refusal is on the chain, which is the point of refusing rather than crashing
    assert world.mizan.verify_chain().ok is True
    assert world.mizan.replay(record.decision_id).identical is True


def test_an_unreadable_account_blocks_and_is_recorded(tmp_path):
    """A portfolio with no positions is not the same claim as a portfolio that could not be read."""
    world = build_world(ledger_dir=tmp_path, portfolio=portfolio_snapshot(buying_power="0", cash="0"))

    record = world.mizan.evaluate(proposal("10"))

    assert record.verdict == "REJECT", (record.verdict, world.codes(record))
    assert "INSUFFICIENT_BUYING_POWER" in world.codes(record)
    assert record.authorization is None
    assert world.mizan.verify_chain().ok is True


def test_a_broker_that_will_not_answer_stops_everything_but_records_nothing(tmp_path):
    """Shipped behaviour, pinned so that a change to it is deliberate. See REQ-20."""
    world = build_world(ledger_dir=tmp_path)
    world.broker.set_market_snapshot(None)

    with pytest.raises(BrokerError) as outage:
        world.mizan.evaluate(proposal("10"))

    assert "MARKET_DATA_MISSING" in [
        str(getattr(code, "value", code)) for code in outage.value.reason_codes
    ]
    assert world.broker.submitted == []
    assert world.mizan.list_decisions(limit=50) == [], "REQ-20: the outage leaves no trace on the chain"
    assert world.mizan.verify_chain().length == 0


def test_a_broker_that_fails_the_submission_is_never_treated_as_a_success(tmp_path):
    """An unknown outcome is a FAILED execution; the idempotency key is what makes a retry safe."""
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    world.broker.fail_with = BrokerError("the venue is unreachable")

    result = world.mizan.execute(record.decision_id)

    assert result.status == "FAILED", (result.status, world.codes(result))
    assert world.codes(result) == ["BROKER_UNAVAILABLE"]
    assert result.status not in {"SUBMITTED", "WOULD_SUBMIT", "RECONCILED_EXISTING"}
    assert result.broker_order_id is None
    assert result.fills == []
    assert world.broker.submitted == [], "the broker refused; nothing was recorded as placed"
    # the message the venue produced does not reach the result; only the machine code does
    assert "unreachable" not in result.message


def test_a_broker_that_dies_during_the_gates_revalidation_raises_instead_of_blocking(tmp_path):
    """A real inconsistency in CHECK_ORDER, pinned as it ships. See REQ-21.

    Step 3 (``find_order``) and step 8 (``submit_order``) are both wrapped, and a broker failure there
    becomes a ``FAILED`` ExecutionResult carrying ``BROKER_UNAVAILABLE``. Step 4 - the TOCTOU
    re-validation, which performs TWO broker reads - is not wrapped, so the same class of failure a
    few microseconds later escapes as an exception and produces no ExecutionResult at all: no status,
    no reason codes, no revalidation report for the console or the audit trail to show.

    Nothing is submitted either way, so this is not a safety hole. It is an evidence hole, and it is
    asserted rather than papered over.
    """
    world = build_world(ledger_dir=tmp_path)
    record = world.mizan.evaluate(proposal("10"))
    world.broker.on_find_order = lambda: world.broker.set_portfolio_snapshot(None)

    with pytest.raises(BrokerError):
        world.mizan.execute(record.decision_id)

    assert world.broker.submitted == []
    with pytest.raises(Exception):  # noqa: B017 - NotFound: no ExecutionResult was ever produced
        world.mizan.get_execution(record.decision_id)
