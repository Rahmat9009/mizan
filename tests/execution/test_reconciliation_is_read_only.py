"""Reconciliation reads. The tests are mostly about what it must never be able to do.

A reconciler that can act is a second execution path: one that runs on a timer, with no authorization,
no policy evaluation and no kill-switch check in front of it. So the assertions here come in two
kinds - that the *classification* is right, and that no input, no discrepancy and no broker answer can
make it mutate anything (B4).

The interesting classification is ``UNEXPECTED_AT_BROKER``: an order exists at the venue for an
execution Mizan recorded as BLOCKED. That is the shape a gate bypass would take, so it gets its own
status rather than being folded into a generic mismatch.

Self-contained by design.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mizan.adapters import BrokerOrder, MockBroker
from mizan.contracts.errors import BrokerError
from mizan.execution import DISCREPANCY_STATUSES, Reconciler
from tests.fixtures import (
    FIXED_NOW,
    FIXED_NOW_STR,
    make_authorization,
    make_execution_result,
    make_market_snapshot,
    make_portfolio_snapshot,
)

AS_OF = FIXED_NOW + timedelta(minutes=1)

#: Anything that could change or unwind a position. The reconciler must define none of them.
MUTATION_METHODS = (
    "cancel_order",
    "cancel_orders",
    "cancel_all_orders",
    "replace_order",
    "modify_order",
    "close_position",
    "close_all_positions",
    "liquidate",
    "resubmit",
    "repair",
    "remediate",
)


def a_broker() -> MockBroker:
    return MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )


def a_submitted_result(
    *, broker_order_id="mock-1", broker_status="accepted", client_order_id=None
):
    """A recorded submission. ``client_order_id`` is explicit so several rows can name several orders.

    The fixture authorization is deterministic, so its idempotency key is the same every time; a report
    with more than one row therefore has to say which key each row is about.
    """
    auth = make_authorization()
    return make_execution_result(
        authorization=auth,
        status="SUBMITTED",
        client_order_id=client_order_id or auth.idempotency_key,
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        submitted_at=FIXED_NOW_STR,
        message="submitted to the paper broker",
    )


def a_blocked_result(*, code="KILL_SWITCH_ACTIVE", client_order_id=None):
    auth = make_authorization()
    return make_execution_result(
        authorization=auth,
        status="BLOCKED",
        client_order_id=client_order_id or auth.idempotency_key,
        reason_codes=[code],
        message="blocked before the mutation",
    )


def an_order(client_order_id: str, *, broker_order_id="mock-1", status="accepted") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        status=status,
        submitted_at=FIXED_NOW_STR,
    )


# ---------------------------------------------------------------------------------------------
# It cannot act
# ---------------------------------------------------------------------------------------------
def test_the_reconciler_defines_no_way_to_change_anything():
    reconciler = Reconciler(a_broker())
    for method in MUTATION_METHODS:
        assert not hasattr(reconciler, method), method


@pytest.mark.parametrize(
    "seed_order",
    [True, False],
    ids=["broker-holds-an-order", "broker-holds-nothing"],
)
def test_no_discrepancy_causes_a_broker_mutation(seed_order):
    """Whatever it finds - agreement, a missing order, an unexpected one - it only ever reads."""
    broker = a_broker()
    submitted = a_submitted_result(client_order_id="mz1-submitted")
    blocked = a_blocked_result(client_order_id="mz1-blocked")
    if seed_order:
        broker.seed_order(an_order("mz1-blocked"))

    report = Reconciler(broker).reconcile([submitted, blocked], as_of=AS_OF)

    assert broker.submitted == []
    assert set(broker.log) == {"broker.find_order"}, broker.log
    assert len(report.items) == 2


def test_the_report_is_frozen_so_a_caller_cannot_edit_a_discrepancy_away():
    report = Reconciler(a_broker()).reconcile([a_submitted_result()], as_of=AS_OF)
    with pytest.raises(ValidationError):
        report.items = []  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------
def test_an_order_that_matches_is_MATCHED_and_the_report_is_clean():
    broker = a_broker()
    result = a_submitted_result()
    broker.seed_order(an_order(result.client_order_id, broker_order_id=result.broker_order_id))

    report = Reconciler(broker).reconcile([result], as_of=AS_OF)
    item = report.items[0]

    assert item.status == "MATCHED"
    assert item.is_discrepancy is False
    assert item.auth_id == result.auth_id
    assert item.broker_order_id == result.broker_order_id
    assert item.broker_status == "accepted"
    assert report.clean is True
    assert report.discrepancies == []
    assert report.broker == broker.name
    assert report.as_of.endswith("Z")


def test_a_submission_the_broker_has_never_heard_of_is_MISSING_AT_BROKER():
    result = a_submitted_result()
    report = Reconciler(a_broker()).reconcile([result], as_of=AS_OF)

    assert report.items[0].status == "MISSING_AT_BROKER"
    assert report.clean is False
    assert report.discrepancies == report.items


def test_an_order_at_the_broker_for_a_BLOCKED_execution_is_UNEXPECTED_AT_BROKER():
    """The serious one: the gate says it never mutated anything, and a mutation exists anyway."""
    broker = a_broker()
    result = a_blocked_result()
    broker.seed_order(an_order(result.client_order_id, broker_order_id="ghost-1"))

    item = Reconciler(broker).reconcile([result], as_of=AS_OF).items[0]

    assert item.status == "UNEXPECTED_AT_BROKER"
    assert item.is_discrepancy is True
    assert item.mizan_status == "BLOCKED"
    assert item.broker_order_id == "ghost-1"


def test_a_blocked_execution_with_no_order_at_the_broker_is_agreement():
    item = Reconciler(a_broker()).reconcile([a_blocked_result()], as_of=AS_OF).items[0]
    assert item.status == "ABSENT_AS_EXPECTED"
    assert item.is_discrepancy is False


def test_a_dry_run_with_an_order_at_the_broker_is_also_unexpected():
    """WOULD_SUBMIT asserts that nothing was sent. If something was, that is worth a human."""
    broker = a_broker()
    result = make_execution_result()  # the fixture default is a dry run
    assert result.status == "WOULD_SUBMIT"
    broker.seed_order(an_order(result.client_order_id))

    assert Reconciler(broker).reconcile([result], as_of=AS_OF).items[0].status == "UNEXPECTED_AT_BROKER"


def test_a_different_broker_order_id_under_the_same_key_is_STATUS_DIVERGED():
    broker = a_broker()
    result = a_submitted_result(broker_order_id="mock-1")
    broker.seed_order(an_order(result.client_order_id, broker_order_id="somebody-elses-order"))

    item = Reconciler(broker).reconcile([result], as_of=AS_OF).items[0]
    assert item.status == "STATUS_DIVERGED"
    assert item.broker_order_id == "somebody-elses-order"


def test_a_changed_order_status_is_STATUS_DIVERGED():
    broker = a_broker()
    result = a_submitted_result(broker_status="accepted")
    broker.seed_order(
        an_order(result.client_order_id, broker_order_id=result.broker_order_id, status="canceled")
    )

    item = Reconciler(broker).reconcile([result], as_of=AS_OF).items[0]
    assert item.status == "STATUS_DIVERGED"
    assert item.broker_status == "canceled"


def test_a_result_without_a_client_order_id_has_nothing_to_look_up():
    result = make_execution_result(
        status="BLOCKED",
        reason_codes=["EXECUTION_DISABLED"],
        client_order_id=None,
        message="execution is disabled",
    )
    item = Reconciler(a_broker()).reconcile([result], as_of=AS_OF).items[0]
    assert item.status == "NOT_APPLICABLE"
    assert item.is_discrepancy is False


def test_a_broker_that_cannot_be_asked_is_a_discrepancy_not_an_agreement():
    """"I could not ask" is never "fine" (E2). The report says so, and the other rows still run."""

    class UnreachableBroker:
        name = "unreachable"
        environment = "paper"

        def find_order(self, client_order_id: str):
            raise BrokerError("venue down", reason_codes=["BROKER_UNAVAILABLE"])

    report = Reconciler(UnreachableBroker()).reconcile(
        [a_submitted_result(), a_blocked_result()], as_of=AS_OF
    )

    assert [item.status for item in report.items] == ["BROKER_UNAVAILABLE", "BROKER_UNAVAILABLE"]
    assert report.clean is False
    assert "BROKER_UNAVAILABLE" in DISCREPANCY_STATUSES


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------
def test_a_mixed_report_separates_agreement_from_the_rows_that_need_a_human():
    broker = a_broker()
    matched = a_submitted_result(client_order_id="mz1-matched")
    broker.seed_order(an_order("mz1-matched", broker_order_id=matched.broker_order_id))
    missing = a_submitted_result(client_order_id="mz1-missing", broker_order_id="mock-9")
    absent = a_blocked_result(client_order_id="mz1-absent")

    report = Reconciler(broker).reconcile([matched, missing, absent], as_of=AS_OF)

    assert [item.status for item in report.items] == [
        "MATCHED",
        "MISSING_AT_BROKER",
        "ABSENT_AS_EXPECTED",
    ]
    assert [item.status for item in report.discrepancies] == ["MISSING_AT_BROKER"]
    assert report.by_status("MATCHED") == [report.items[0]]
    assert report.clean is False


def test_an_empty_run_is_clean_and_asks_the_broker_nothing():
    broker = a_broker()
    report = Reconciler(broker).reconcile([], as_of=datetime(2026, 9, 3, tzinfo=UTC))
    assert report.items == []
    assert report.clean is True
    assert broker.log == []
