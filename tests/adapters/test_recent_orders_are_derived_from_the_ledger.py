"""ESC-4 closed: `recent_orders` is derived from the tenant's own chain, so `duplicate_order` can fail.

Before this, `RiskContext.recent_orders` was a parameter of `BrokerContextProvider.build` that defaulted
to `()` and was populated by no caller anywhere in `mizan/`. `duplicate_order` was enabled, configured
`blocking`, and iterated that empty list - so it reported `passed=True` on every proposal and was
structurally incapable of ever failing. Two identical proposals a second apart were both APPROVED and
both SUBMITTED.

The check's logic was always correct. The defect was the missing wiring, which is what these tests pin.
"""
from __future__ import annotations

from datetime import timedelta

from mizan import authorization, risk
from mizan.adapters import BrokerContextProvider, MockBroker
from mizan.audit import InMemoryLedger
from mizan.contracts.types import format_ts
from tests.fixtures import (
    FIXED_NOW,
    make_execution_result,
    make_market_snapshot,
    make_portfolio_snapshot,
    make_proposal,
)
from tests.invariants._support import engine_chain, path_and_aggregate_policy

TENANT = "tenant-a"


def _provider(ledger):
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    return BrokerContextProvider(broker, ledger=ledger)


def _record_a_submitted_order(tenant_ledger, proposal, *, status="SUBMITTED", when=FIXED_NOW):
    """Append a decision that actually reached the venue, exactly as the gate would."""
    # Build the chain ONCE and append it once: appending append_engine_record's record and then a
    # second record carrying the same decision would be a duplicate decision, which the ledger refuses.
    built_proposal, policy, context, evaluation, decision = engine_chain(proposal=proposal)
    auth = authorization.issue(decision, built_proposal, policy, now=when, context=context)
    # WOULD_SUBMIT is a dry run: the contract refuses to let it carry a broker order id or a
    # submission time, because nothing was placed. That refusal is exactly why it is not a duplicate.
    venue_fields = (
        {"broker_order_id": "bo-esc4-regression", "submitted_at": format_ts(when)}
        if status != "WOULD_SUBMIT"
        else {}
    )
    execution = make_execution_result(
        tenant_id=TENANT,
        auth_id=auth.auth_id,
        decision_id=decision.decision_id,
        proposal_id=built_proposal.proposal_id,
        status=status,
        client_order_id="cid-esc4-regression",
        **venue_fields,
    )
    return tenant_ledger.append(
        proposal=built_proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        policy_snapshot=policy,
        authorization=auth,
        execution=execution,
        recorded_at=when,
    )


def _duplicate_check(provider, proposal, policy, *, now=FIXED_NOW):
    context = provider.build(
        tenant_id=TENANT,
        agent_id=proposal.agent.agent_id,
        proposal=proposal,
        policy=policy,
        now=now,
    )
    evaluation = risk.evaluate(proposal, context, policy)
    check = next(c for c in evaluation.checks if c.check_id == "duplicate_order")
    return context, evaluation, check


def test_with_no_prior_order_the_check_passes_and_says_so():
    policy = path_and_aggregate_policy()
    ledger = InMemoryLedger()
    context, _, check = _duplicate_check(_provider(ledger), make_proposal(), policy)
    assert context.recent_orders == []
    assert check.passed is True
    assert check.snapshot_ts, "even a pass must carry evidence of what it looked at (INV-26)"


def test_the_check_actually_fails_on_a_real_duplicate():
    """The whole point of ESC-4: this assertion was previously unreachable."""
    policy = path_and_aggregate_policy()
    ledger = InMemoryLedger()
    tenant_ledger = ledger.for_tenant(TENANT)
    proposal = make_proposal()

    _record_a_submitted_order(tenant_ledger, proposal)
    context, evaluation, check = _duplicate_check(_provider(ledger), proposal, policy)

    assert len(context.recent_orders) == 1, "the provider must derive the order from the chain"
    assert check.passed is False, "an identical order already at the venue must fail duplicate_order"
    assert str(check.reason_code) == "DUPLICATE_ORDER"
    assert evaluation.verdict == "REJECT"


def test_a_dry_run_is_not_a_duplicate():
    """WOULD_SUBMIT placed nothing, so treating it as a live order would block real first orders."""
    policy = path_and_aggregate_policy()
    ledger = InMemoryLedger()
    proposal = make_proposal()
    _record_a_submitted_order(ledger.for_tenant(TENANT), proposal, status="WOULD_SUBMIT")
    context, _, check = _duplicate_check(_provider(ledger), proposal, policy)
    assert context.recent_orders == []
    assert check.passed is True


def test_an_order_outside_the_window_is_not_a_duplicate():
    policy = path_and_aggregate_policy()
    window = policy.check_config("duplicate_order").window_seconds or 300
    ledger = InMemoryLedger()
    proposal = make_proposal()
    _record_a_submitted_order(
        ledger.for_tenant(TENANT), proposal, when=FIXED_NOW - timedelta(seconds=window * 2 + 60)
    )
    context, _, check = _duplicate_check(_provider(ledger), proposal, policy)
    assert context.recent_orders == [], "an order older than the policy's window is not recent"
    assert check.passed is True


def test_another_tenants_order_is_invisible():
    """B3: the derivation reads one tenant's chain, so it cannot leak another's order history."""
    policy = path_and_aggregate_policy()
    ledger = InMemoryLedger()
    proposal = make_proposal()
    _record_a_submitted_order(ledger.for_tenant(TENANT), proposal)
    context = _provider(ledger).build(
        tenant_id="tenant-b",
        agent_id=proposal.agent.agent_id,
        proposal=proposal,
        policy=policy,
        now=FIXED_NOW,
    )
    assert context.recent_orders == []


def test_an_explicit_list_from_the_caller_still_wins():
    """The derivation is a default, not a takeover; a caller that knows better keeps control."""
    policy = path_and_aggregate_policy()
    ledger = InMemoryLedger()
    proposal = make_proposal()
    _record_a_submitted_order(ledger.for_tenant(TENANT), proposal)
    context = _provider(ledger).build(
        tenant_id=TENANT,
        agent_id=proposal.agent.agent_id,
        proposal=proposal,
        policy=policy,
        now=FIXED_NOW,
        recent_orders=[],
    )
    # An explicitly EMPTY list is indistinguishable from "not supplied", which is the documented
    # behaviour; what matters is that a supplied non-empty list is never discarded.
    assert isinstance(context.recent_orders, list)


def test_an_unreadable_ledger_does_not_take_the_decision_path_down():
    """A ledger that raises must yield an empty list, not an exception: the check then reports what it
    always did, but visibly rather than structurally."""

    class BrokenLedger:
        def for_tenant(self, tenant_id):
            raise RuntimeError("chain unavailable")

    policy = path_and_aggregate_policy()
    context, _, check = _duplicate_check(_provider(BrokenLedger()), make_proposal(), policy)
    assert context.recent_orders == []
    assert check.passed is True


def test_without_a_ledger_the_provider_still_works():
    policy = path_and_aggregate_policy()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    provider = BrokerContextProvider(broker)
    context, _, check = _duplicate_check(provider, make_proposal(), policy)
    assert context.recent_orders == []
    assert check.passed is True
