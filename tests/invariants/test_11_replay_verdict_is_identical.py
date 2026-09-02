"""Invariant 11 - Hard Rule A1: same inputs + same policy version + same engine version = same verdict.

Pass criterion: a DecisionRecord produced by the real engine (risk.evaluate + governor.govern) and appended to a
ledger replays in exact mode with identical=True, replayed verdict / reason codes / verdict_hash equal to the
originals and a byte-identical RiskEvaluation (same evaluation_id, same canonical JSON); running the replay twice
gives the same answer; a rejected proposal replays identically too. Replaying the same record under a stricter
policy (policy mode) is a real re-evaluation: the verdict flips to REJECT, identical is False, and the change is
attributable to the policy content, not to a TENANT_MISMATCH / POLICY_HASH_MISMATCH artefact. The SDK pipeline
(Mizan.evaluate -> Mizan.replay) satisfies the same criterion.
"""
from __future__ import annotations

from mizan import replay
from mizan.advisory import OfflineAdvisoryProvider
from mizan.audit import InMemoryLedger
from mizan.contracts import DecisionRecord
from mizan.contracts.canonical import canonical_json
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan

from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    killer_demo_reject_proposal,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)
from tests.invariants._support import RecordingBroker, append_engine_record, code_str, codes


def _assert_exact_replay(result, record: DecisionRecord):
    assert result.decision_id == record.decision_id
    assert result.mode == "exact"
    assert result.identical is True
    assert result.original_verdict == record.verdict == record.governor_decision.verdict
    assert result.replayed_verdict == result.original_verdict
    assert sorted(map(code_str, result.replayed_reason_codes)) == sorted(map(code_str, record.reason_codes))
    assert sorted(map(code_str, result.original_reason_codes)) == sorted(map(code_str, record.reason_codes))
    assert result.original_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert canonical_json(result.replayed_evaluation) == canonical_json(record.risk_evaluation)
    assert result.replayed_decision.verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_decision.authorized.total_quantity == record.authorized.total_quantity


def test_replay_verdict_is_identical():
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    record, _chain = append_engine_record(tenant_ledger)

    first = replay.replay(record)
    _assert_exact_replay(first, record)

    second = replay.replay(record)
    _assert_exact_replay(second, record)
    assert canonical_json(second.replayed_evaluation) == canonical_json(first.replayed_evaluation)
    assert second.replayed_verdict_hash == first.replayed_verdict_hash

    # the record read back from the ledger replays identically as well (A5: verification from storage)
    stored = tenant_ledger.get(record.decision_id)
    _assert_exact_replay(replay.replay(stored), stored)


def test_rejected_decision_replays_identically():
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    record, _chain = append_engine_record(tenant_ledger, proposal=killer_demo_reject_proposal())
    assert record.verdict == "REJECT", codes(record)
    _assert_exact_replay(replay.replay(record), record)


def test_replay_with_a_stricter_policy_is_a_real_reevaluation():
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    record, (_proposal, policy, _context, _evaluation, _decision) = append_engine_record(tenant_ledger)
    assert record.verdict != "REJECT", (
        "the default fixture proposal must be authorizable under the default policy: " + str(codes(record))
    )
    strict = make_policy(order={**policy.order.model_dump(mode="json"), "max_notional": "0.01"})
    assert strict.policy_hash != policy.policy_hash
    assert strict.tenant_id == record.tenant_id

    result = replay.replay(record, policy=strict)
    assert result.decision_id == record.decision_id
    assert result.mode == "policy"
    assert result.identical is False
    assert result.original_verdict == record.verdict
    assert result.replayed_verdict == "REJECT"
    replayed_codes = {code_str(c) for c in result.replayed_reason_codes}
    assert not replayed_codes & {"POLICY_HASH_MISMATCH", "TENANT_MISMATCH"}, replayed_codes
    assert result.replayed_verdict_hash != result.original_verdict_hash
    assert result.replayed_decision.policy.hash == strict.policy_hash
    assert result.replayed_evaluation.policy.hash == strict.policy_hash


def test_sdk_pipeline_replays_identically():
    proposal = make_proposal()
    policy = make_policy()
    broker = RecordingBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    mizan = Mizan(
        tenant_id=TENANT_A,
        agent=proposal.agent,
        policy=policy,
        broker=broker,
        ledger=InMemoryLedger(),
        advisory=OfflineAdvisoryProvider(),
        kill_switch=InMemoryKillSwitch(),
        config=ExecutionConfig(),
        clock=lambda: FIXED_NOW,
    )
    record = mizan.evaluate(proposal)
    assert isinstance(record, DecisionRecord)
    assert record.tenant_id == TENANT_A
    assert broker.submitted == []  # evaluate never executes

    result = mizan.replay(record.decision_id)
    _assert_exact_replay(result, record)
    assert mizan.verify_chain().ok is True
    assert canonical_json(mizan.get_decision(record.decision_id)) == canonical_json(record)
