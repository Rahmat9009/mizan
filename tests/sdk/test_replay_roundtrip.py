"""evaluate -> ledger -> decision replay, and the verdict hash that must survive the round trip.

Hard Rule A1 says the same inputs, policy version and engine version give the same verdict. Invariant
11 proves it for the engine; this proves it for the *SDK path*, which is the one a customer's code
actually takes: through a real context provider, a real ledger append, a real read back from storage,
and back into the engine. If the SDK dropped, reordered or re-derived a single field on the way in,
the replayed ``verdict_hash`` would differ and this suite would say so.
"""

from __future__ import annotations

import pytest

from mizan.audit import SqliteLedger
from mizan.contracts.canonical import canonical_json
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    killer_demo_policy,
    killer_demo_reject_proposal,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)


def assert_identical(result, record) -> None:
    assert result.decision_id == record.decision_id
    assert result.mode == "exact"
    assert result.identical is True
    assert result.original_verdict == record.verdict
    assert result.replayed_verdict == record.verdict
    assert result.original_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert canonical_json(result.replayed_evaluation) == canonical_json(record.risk_evaluation)
    assert result.replayed_decision.authorized.total_quantity == record.authorized.total_quantity


def test_an_approved_decision_round_trips_to_an_identical_verdict_hash(pipeline, proposal):
    record = pipeline.evaluate(proposal)

    assert_identical(pipeline.replay(record.decision_id), record)

    # and again: a decision replay is not allowed to depend on how many times it has run
    assert_identical(pipeline.replay(record.decision_id), record)
    assert canonical_json(pipeline.get_decision(record.decision_id)) == canonical_json(record)


def test_a_rejected_decision_round_trips_too(build_pipeline):
    proposal = killer_demo_reject_proposal()
    pipeline = build_pipeline(policy=killer_demo_policy(), agent=proposal.agent)

    record = pipeline.evaluate(proposal)
    assert record.verdict == "REJECT"

    assert_identical(pipeline.replay(record.decision_id), record)


def test_a_reduced_decision_round_trips_too(build_pipeline):
    from tests.sdk.conftest import reducing_policy

    pipeline = build_pipeline(policy=reducing_policy(max_quantity="4"))
    record = pipeline.evaluate(make_proposal())
    assert record.verdict == "REDUCE"
    assert record.authorized.total_quantity == "4"

    assert_identical(pipeline.replay(record.decision_id), record)


def test_the_round_trip_survives_real_storage(tmp_path):
    """Read back from SQLite, not from the object the append returned. A5: verification from storage."""
    from mizan.adapters import MockBroker

    proposal = make_proposal()
    pipeline = Mizan(
        tenant_id=TENANT_A,
        agent=proposal.agent,
        policy=make_policy(),
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=SqliteLedger(root_dir=tmp_path),
        advisory=None,
        kill_switch=InMemoryKillSwitch(),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )

    record = pipeline.evaluate(proposal)
    stored = pipeline.get_decision(record.decision_id)

    assert canonical_json(stored) == canonical_json(record)
    assert_identical(pipeline.replay(record.decision_id), stored)
    assert pipeline.verify_chain().ok is True
    assert (tmp_path / f"{TENANT_A}.sqlite").exists()


def test_a_decision_replay_under_a_stricter_policy_is_a_real_reevaluation(pipeline, proposal):
    record = pipeline.evaluate(proposal)
    strict = make_policy(order={"max_notional": "0.01", "max_quantity": "20", "max_legs": 4})

    result = pipeline.replay(record.decision_id, policy=strict)

    assert result.mode == "policy"
    assert result.identical is False
    assert result.replayed_verdict == "REJECT"
    replayed = {str(code.value) for code in result.replayed_reason_codes}
    assert not replayed & {"POLICY_HASH_MISMATCH", "TENANT_MISMATCH"}, replayed
    assert result.replayed_decision.policy.hash == strict.policy_hash


def test_replaying_an_unknown_decision_is_not_found(pipeline):
    from mizan.contracts.errors import NotFound

    with pytest.raises(NotFound):
        pipeline.replay("01a00000-0000-7000-8000-000000000000")


def test_the_advisory_can_be_switched_off_for_a_decision_replay(build_pipeline, proposal):
    """Addendum 1 section D: the deterministic part must reproduce with the semantic layer absent."""
    from mizan.advisory import OfflineAdvisoryProvider

    pipeline = build_pipeline(advisory=OfflineAdvisoryProvider())
    record = pipeline.evaluate(proposal)
    assert record.llm_advisory is not None and record.llm_advisory.invoked is True

    offline = pipeline.replay(record.decision_id, advisory=None)

    assert offline.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert offline.replayed_decision.authorized.total_quantity <= record.authorized.total_quantity
