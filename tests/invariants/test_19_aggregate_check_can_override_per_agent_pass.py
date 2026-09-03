"""Invariant 19 - Dispatch Addendum section 2 / Risk Canon R-AGG: the aggregate layer has authority over the per-agent layer.

Pass criterion: a proposal that passes EVERY per-agent check is still REDUCED or REJECTED once book-level state
says the book is full. The per-agent verdict is necessary, never sufficient. Proven by holding proposal, policy
and per-agent state fixed and varying ONLY the aggregate state, so the change in verdict cannot be attributed to
anything else. A stateless per-proposal engine cannot implement this at all, which is why the engine takes all
state as a RiskContext input (ADR-0006).
"""
from __future__ import annotations

from mizan import risk
from mizan.contracts.types import dec

from tests.fixtures import make_proposal
from tests.invariants._support import codes, empty_book, full_book, path_and_aggregate_policy, unstressed_context

PER_AGENT_EXCLUDED = {"aggregate_exposure", "correlated_intent", "model_provider_concentration", "signal_source_concentration", "crowding"}


def test_aggregate_check_can_override_per_agent_pass():
    policy = path_and_aggregate_policy()
    assert policy.aggregate is not None, "this invariant requires an aggregate policy section"
    proposal = make_proposal()

    roomy = unstressed_context(policy)
    baseline = risk.evaluate(proposal, roomy, policy)
    assert baseline.verdict == "PASS", (
        f"the per-agent layer must PASS this proposal for the invariant to mean anything; got "
        f"{baseline.verdict} with {sorted(codes(baseline))}"
    )
    baseline_qty = dec(baseline.recommended_quantity)

    # Same proposal, same policy, same per-agent state. Only the BOOK changed.
    crowded = unstressed_context(
        policy,
        aggregate_state=full_book(roomy.portfolio_snapshot.equity, policy.aggregate.max_portfolio_exposure_pct),
    )
    overridden = risk.evaluate(proposal, crowded, policy)

    assert overridden.verdict in {"REDUCE", "REJECT"}, (
        "a proposal passing every per-agent check must remain overridable by the aggregate layer; "
        f"the book was at its limit and the engine still returned {overridden.verdict}"
    )
    assert dec(overridden.recommended_quantity) < baseline_qty, "the override must actually reduce the size"
    assert "AGGREGATE_EXPOSURE_EXCEEDED" in codes(overridden), (
        f"the override must be attributable to the aggregate layer; codes were {sorted(codes(overridden))}"
    )


def test_the_per_agent_checks_still_pass_when_the_aggregate_layer_overrides():
    """The override is additive: it does not work by making a per-agent check fail."""
    policy = path_and_aggregate_policy()
    roomy = unstressed_context(policy)
    crowded = unstressed_context(
        policy,
        aggregate_state=full_book(roomy.portfolio_snapshot.equity, policy.aggregate.max_portfolio_exposure_pct),
    )
    evaluation = risk.evaluate(make_proposal(), crowded, policy)
    failed_per_agent = [
        c.check_id for c in evaluation.checks if not c.passed and c.check_id not in PER_AGENT_EXCLUDED
    ]
    assert not failed_per_agent, f"no per-agent check should have failed; these did: {failed_per_agent}"
    assert evaluation.verdict in {"REDUCE", "REJECT"}


def test_an_empty_book_does_not_override():
    """Control: the aggregate layer must not object when the book is empty, or the test above proves nothing."""
    policy = path_and_aggregate_policy()
    evaluation = risk.evaluate(make_proposal(), unstressed_context(policy, aggregate_state=empty_book()), policy)
    assert evaluation.verdict == "PASS"
    assert "AGGREGATE_EXPOSURE_EXCEEDED" not in codes(evaluation)
