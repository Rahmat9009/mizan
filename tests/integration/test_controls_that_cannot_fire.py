"""Controls that are enabled, blocking, and structurally incapable of failing.

This module exists because of the shape of the defect, not its size. ``mizan.risk`` is careful about
the E2 rule everywhere it can see the gap: ``aggregate_exposure`` with no ``aggregate_state`` returns
``missing(... AGGREGATE_STATE_MISSING)``, a deferred check records ``severity="info"`` with a detail
saying so, and a disabled check says that too. So an operator reading a DecisionRecord has been
taught, correctly, that ``passed=True, severity="blocking"`` means a control ran and held.

``duplicate_order`` and the quantity half of ``erroneous_order`` read one input — ``RiskContext
.recent_orders`` — and in the shipped pipeline that list is ALWAYS empty. ``BrokerContextProvider
.build`` takes ``recent_orders`` with a default of ``()`` and ``Mizan.evaluate`` never supplies it,
because execution outcomes never reach the chain to be derived from (ledger/requests.md REQ-12 and
REQ-17). Both checks therefore return ``ok(...)`` — ``passed=True, severity="blocking", detail=""`` —
whatever has actually been traded.

That is not a missing feature. It is a control that reports success on no evidence, in a record whose
whole purpose is to be believed later. Escalated as ESC-4; these tests pin the behaviour as it ships
so the fix is visible when it lands, and they FAIL when it lands, which is the point.
"""

from __future__ import annotations

from datetime import timedelta

from mizan.contracts import TradeProposal, format_ts
from mizan.policy import load_policy, validate_policy
from tests.integration._world import (
    AGENT_A,
    MODEL,
    NOW,
    POLICY_YAML,
    build_world,
)

# POLICY_YAML is a format template, so its literal braces are doubled. The insert keeps that form.
_WITH_DUPLICATE_CHECK = POLICY_YAML.replace(
    "  position_limit: {{enabled: true, severity: warning}}",
    "  position_limit: {{enabled: true, severity: warning}}\n"
    "  duplicate_order: {{enabled: true, severity: blocking, window_seconds: 60}}\n"
    "  erroneous_order:\n"
    "    enabled: true\n"
    "    severity: blocking\n"
    "    price_deviation_threshold: '0.20'\n"
    "    quantity_deviation_threshold: '5.0'",
)
assert "duplicate_order" in _WITH_DUPLICATE_CHECK, "the policy insert did not apply"


def _policy(tenant_id: str = "tenant-a"):
    return validate_policy(load_policy(_WITH_DUPLICATE_CHECK.format(tenant_id=tenant_id, ttl_seconds=15)))


def _proposal_at(offset_seconds: int, quantity: str = "10") -> TradeProposal:
    """Two proposals that differ only in when they were created are two different proposal ids."""
    created = NOW + timedelta(seconds=offset_seconds)
    return TradeProposal.build(
        agent=AGENT_A,
        model=MODEL,
        created_at=format_ts(created),
        expires_at=format_ts(created + timedelta(minutes=5)),
        intent="open",
        symbol="AAPL",
        asset_class="equity",
        strategy="long_equity",
        legs=[
            {
                "leg_index": 0, "side": "buy", "contract_type": None, "strike": None,
                "expiry": None, "quantity": quantity, "limit_price": "228.50",
                "order_type": "limit",
            }
        ],
        reasoning="",
        market_snapshot_ref="mkt-integration-1",
        portfolio_snapshot_ref="pf-integration-1",
    )


def test_the_shipped_pipeline_never_populates_recent_orders(tmp_path):
    """The root fact, asserted once. Everything below follows from it."""
    world = build_world(ledger_dir=tmp_path, policy=_policy())
    record = world.mizan.evaluate(_proposal_at(0))
    world.mizan.execute(record.decision_id)

    later = world.mizan.evaluate(_proposal_at(1))

    assert record.risk_context.recent_orders == []
    assert later.risk_context.recent_orders == [], (
        "recent_orders is now populated - ESC-4 may be fixed; re-check this whole module"
    )


def test_duplicate_order_reports_a_blocking_pass_on_no_evidence(tmp_path):
    """``passed=True, severity='blocking'`` is what a control that HELD looks like. It did not run."""
    world = build_world(ledger_dir=tmp_path, policy=_policy())
    assert world.mizan.policy.check_config("duplicate_order").window_seconds == 60, (
        "the explicit configuration did not reach the policy; this test would pass on defaults"
    )
    record = world.mizan.evaluate(_proposal_at(0))
    world.mizan.execute(record.decision_id)

    later = world.mizan.evaluate(_proposal_at(1))
    check = next(c for c in later.checks if c.check_id == "duplicate_order")

    assert check.passed is True
    assert check.severity == "blocking"
    assert check.reason_code is None
    assert check.detail == "", (
        "the check now explains itself - if it says its input was unavailable, ESC-4 is addressed"
    )
    # contrast: a check whose state IS known to be missing says so, loudly and blockingly
    unwired = build_world(ledger_dir=tmp_path / "b", policy=_policy())
    assert unwired.mizan.context_provider.aggregate_state is None


def test_two_identical_orders_one_second_apart_both_reach_the_venue(tmp_path):
    """The consequence, end to end, with the duplicate check enabled and set to blocking.

    Idempotency does not catch this either: ``idempotency_key`` is derived from the proposal id, and
    two proposals created a second apart are two different proposals. So neither the risk layer nor
    the gate stops it, and the paper account holds two positions where the policy said one.
    """
    world = build_world(ledger_dir=tmp_path, policy=_policy())
    first, second = _proposal_at(0), _proposal_at(1)
    assert first.proposal_id != second.proposal_id
    assert [leg.quantity for leg in first.legs] == [leg.quantity for leg in second.legs]

    a = world.mizan.evaluate(first)
    result_a = world.mizan.execute(a.decision_id)
    b = world.mizan.evaluate(second)
    result_b = world.mizan.execute(b.decision_id)

    assert a.verdict == b.verdict == "APPROVE"
    assert world.codes(b) == [], "the duplicate was not even mentioned in the second decision"
    assert result_a.status == "SUBMITTED"
    assert result_b.status == "SUBMITTED", "ESC-4: the second identical order was accepted"
    assert len(world.broker.submitted) == 2, (
        "only one order reached the venue - ESC-4 may be fixed; update this test"
    )
    assert result_a.client_order_id != result_b.client_order_id


def test_the_erroneous_order_quantity_arm_is_dead_while_the_price_arm_is_alive(tmp_path):
    """One check, two arms, only one of which has an input. Worth separating so the fix can be partial.

    The PRICE arm reads the market snapshot and works: a limit far from the quote is refused. The
    QUANTITY arm reads ``recent_orders`` and cannot fire, so "this order is 50x anything you have
    traded today" is unenforceable.
    """
    world = build_world(ledger_dir=tmp_path, policy=_policy())

    # price arm: alive
    absurd = _proposal_at(0)
    payload = absurd.model_dump(mode="json")
    payload.pop("proposal_id")
    payload["legs"][0]["limit_price"] = "1000"
    record = world.mizan.evaluate(TradeProposal.build(**payload))
    assert record.verdict == "REJECT"
    assert "ERRONEOUS_PRICE_DEVIATION" in world.codes(record)

    # quantity arm: dead. A small order, then one twenty times larger, and nothing is said.
    small = world.mizan.evaluate(_proposal_at(1, quantity="1"))
    world.mizan.execute(small.decision_id)
    large = world.mizan.evaluate(_proposal_at(2, quantity="20"))

    assert "ERRONEOUS_QUANTITY_DEVIATION" not in world.codes(large), (
        "the quantity arm fired - ESC-4 may be fixed; update this test"
    )
    check = next(c for c in large.checks if c.check_id == "erroneous_order")
    assert check.passed is True and check.severity == "blocking"
