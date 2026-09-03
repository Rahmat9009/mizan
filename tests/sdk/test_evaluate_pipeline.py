"""``Mizan.evaluate`` — the whole decision plane in one call, and what it refuses to take from you.

The pipeline is context -> deterministic risk -> advisory -> governor -> authorization -> ledger, and
the record it returns is already chained. These tests pin the parts a caller could otherwise subvert:
where the market data comes from (F-1/F-2), whose agent identity is used (F-3), what happens when the
broker will not answer (E2), and the fact that ``evaluate`` never, under any verdict, executes.
"""

from __future__ import annotations

import pytest

from mizan.contracts import DecisionRecord
from mizan.contracts.canonical import ZERO_HASH
from mizan.contracts.errors import MizanError
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    injection_reasoning,
    killer_demo_policy,
    killer_demo_reject_proposal,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)


def codes(obj) -> set[str]:
    return {str(getattr(code, "value", code)) for code in obj.reason_codes}


def test_evaluate_returns_a_chained_record(pipeline, proposal, broker):
    record = pipeline.evaluate(proposal)

    assert isinstance(record, DecisionRecord)
    assert record.tenant_id == TENANT_A
    assert record.proposal_id == proposal.proposal_id
    assert record.sequence == 1
    assert record.audit_prev_hash == ZERO_HASH
    assert record.verdict == record.governor_decision.verdict
    assert pipeline.verify_chain().ok is True
    # the whole point of "evaluate never executes": the broker was read, never written
    assert broker.submitted == []
    assert "broker.submit_order" not in broker.log


def test_evaluate_reads_the_market_from_the_broker_not_the_caller(pipeline, proposal, broker):
    """F-1/F-2: there is no parameter by which a caller supplies the numbers it will be judged against."""
    record = pipeline.evaluate(proposal)

    context = record.risk_context
    assert context.market_snapshot is not None
    assert context.market_snapshot.snapshot_id == broker.market_snapshot.snapshot_id
    assert context.portfolio_snapshot is not None
    assert context.portfolio_snapshot.snapshot_id == broker.portfolio_snapshot.snapshot_id
    assert "broker.get_market_snapshot" in broker.log
    assert "broker.get_portfolio_snapshot" in broker.log

    # and the API surface offers no way to pass one in
    with pytest.raises(TypeError):
        pipeline.evaluate(proposal, context=record.risk_context)


def test_evaluate_appends_every_verdict_including_a_rejection(build_pipeline, broker):
    """A refusal belongs in the chain. It is the product, not an error to swallow."""
    proposal = killer_demo_reject_proposal()
    pipeline = build_pipeline(policy=killer_demo_policy(), agent=proposal.agent)

    record = pipeline.evaluate(proposal)

    assert record.verdict == "REJECT", codes(record)
    assert record.authorization is None
    assert record.authorized.total_quantity == "0"
    assert [r.decision_id for r in pipeline.list_decisions()] == [record.decision_id]
    assert pipeline.verify_chain().ok is True
    assert broker.submitted == []


def test_authorization_is_minted_only_for_a_surviving_decision(pipeline, proposal):
    record = pipeline.evaluate(proposal)

    assert record.verdict in {"APPROVE", "REDUCE"}
    assert record.authorization is not None
    assert record.authorization.tenant_id == TENANT_A
    assert record.authorization.decision_id == record.decision_id
    assert record.authorization.environment == "paper"
    assert record.authorization.single_use is True
    assert record.authorization.bound_state.policy_hash == pipeline.policy.policy_hash


def test_a_proposal_claiming_another_agent_is_refused(pipeline):
    """F-3: identity is the instance's binding, never something the payload asserts."""
    impostor = make_proposal(agent=make_agent(agent_id="some-other-agent"))

    with pytest.raises(MizanError) as refusal:
        pipeline.evaluate(impostor)

    assert refusal.value.http_status in {403, 422}
    assert "some-other-agent" not in refusal.value.message
    assert pipeline.list_decisions() == []


def test_reasoning_is_audit_only_and_changes_no_verdict(build_pipeline, broker):
    """Invariant 17 at the SDK level: adversarial free text is recorded and enforced against nothing."""
    plain = make_proposal()
    injected = make_proposal(reasoning=injection_reasoning())
    assert injected.proposal_id == plain.proposal_id  # reasoning is excluded from the id

    clean = build_pipeline().evaluate(plain)
    poisoned = build_pipeline(ledger=None).evaluate(injected)

    assert poisoned.verdict == clean.verdict
    assert poisoned.authorized.total_quantity == clean.authorized.total_quantity
    assert poisoned.governor_decision.verdict_hash == clean.governor_decision.verdict_hash
    # the text is kept, because an auditor needs to read what the agent said
    assert poisoned.proposal.reasoning == injection_reasoning()


@pytest.mark.parametrize("missing", ["market", "portfolio"])
def test_a_broker_that_cannot_answer_fails_closed(build_pipeline, broker, proposal, missing):
    """E2: unreadable state is a refusal, never an evaluation against zero."""
    if missing == "market":
        broker.set_market_snapshot(None)
    else:
        broker.set_portfolio_snapshot(None)

    pipeline = build_pipeline()
    try:
        record = pipeline.evaluate(proposal)
    except MizanError as refusal:
        # A refusal that never reaches the ledger is acceptable only if it authorizes nothing.
        assert refusal.http_status >= 400
        assert pipeline.list_decisions() == []
        assert broker.submitted == []
        return
    assert record.verdict == "REJECT", codes(record)
    assert record.authorization is None
    assert record.risk_evaluation.data_complete is False
    assert broker.submitted == []


def test_a_policy_for_another_tenant_cannot_be_bound(broker, proposal):
    with pytest.raises(MizanError):
        Mizan(
            tenant_id="tenant-b",
            agent=proposal.agent,
            policy=make_policy(tenant_id=TENANT_A),
            broker=broker,
            clock=lambda: FIXED_NOW,
        )


def test_evaluating_without_a_broker_refuses_rather_than_inventing_prices(proposal):
    """No market data source is a configuration error, not an empty snapshot."""
    pipeline = Mizan(
        tenant_id=TENANT_A,
        agent=proposal.agent,
        policy=make_policy(),
        broker=None,
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(MizanError) as refusal:
        pipeline.evaluate(proposal)

    assert refusal.value.http_status >= 400


def test_the_record_captures_the_exact_policy_the_decision_was_made_under(pipeline, proposal):
    record = pipeline.evaluate(proposal)

    assert record.policy_snapshot.policy_hash == pipeline.policy.policy_hash
    assert record.policy.hash == pipeline.policy.policy_hash
    assert record.risk_context.policy.hash == pipeline.policy.policy_hash
    assert record.risk_evaluation.policy.hash == pipeline.policy.policy_hash


def test_consecutive_evaluations_extend_one_chain(build_pipeline, broker):
    pipeline = build_pipeline()
    first = pipeline.evaluate(make_proposal())
    second = pipeline.evaluate(make_proposal(symbol="MSFT", legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "3",
            "limit_price": "412.10",
            "order_type": "limit",
        }
    ]))

    assert second.sequence == first.sequence + 1
    assert second.audit_prev_hash == first.audit_hash
    verification = pipeline.verify_chain()
    assert verification.ok is True
    assert verification.length == 2


def test_a_market_snapshot_the_caller_owns_is_never_consulted(build_pipeline, proposal):
    """Even a broker that returns a *different* snapshot than the caller expects wins.

    The assertion is deliberately about provenance rather than value: what matters is that the record
    shows the broker's snapshot id, so an auditor can prove which numbers were used.
    """
    from mizan.adapters import MockBroker

    # snapshot_id is content-derived (REQ-34), so a distinct snapshot is made by distinct CONTENT.
    other = make_market_snapshot(source="broker-only:feed")
    broker = MockBroker(portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=other)
    record = build_pipeline(broker=broker).evaluate(proposal)

    assert record.risk_context.market_snapshot.snapshot_id == other.snapshot_id
    assert record.risk_context.market_snapshot.source == "broker-only:feed"
    assert record.proposal.market_snapshot_ref != record.risk_context.market_snapshot.snapshot_id
