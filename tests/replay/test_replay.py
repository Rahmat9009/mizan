"""Decision replay: the same inputs, the same policy version and the same engine give the same verdict.

Hard Rule A1. Say "decision replay", never bare "replay" - the bare word already means chart playback in
retail trading tools.

Exact mode is a claim about the product: hand a customer a record and they get the same answer back,
byte for byte. Policy mode is the question the record makes answerable - "would today's rules have
stopped last quarter's order?" - and there a different verdict is the answer, not a failure.
"""

from __future__ import annotations

import pytest

from mizan import governor, risk
from mizan import replay as decision_replay
from mizan.audit import InMemoryLedger
from mizan.contracts import DecisionRecord
from mizan.contracts.canonical import ENGINE_VERSION, canonical_json, uuid7
from mizan.contracts.errors import ValidationFailed
from mizan.contracts.types import dec
from mizan.replay import ReplayResult
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    make_advisory,
    make_context,
    make_decision,
    make_evaluation,
    make_policy,
    make_proposal,
)


def _engine_is_implemented() -> bool:
    policy = make_policy()
    try:
        risk.evaluate(make_proposal(), make_context(policy=policy.ref), policy)
    except NotImplementedError:
        return False
    return True


requires_engine = pytest.mark.skipif(
    not _engine_is_implemented(), reason="mizan.risk / mizan.governor are not implemented yet (L1/L2a)"
)


def engine_record(*, proposal=None, policy=None, advisory=None, tenant_ledger=None) -> DecisionRecord:
    """A record produced by the REAL engine and appended to a ledger, the way production makes one."""
    policy = policy or make_policy()
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = proposal or make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, advisory, context=context)
    tenant_ledger = tenant_ledger or InMemoryLedger().for_tenant(TENANT_A)
    return tenant_ledger.append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        policy_snapshot=policy,
        recorded_at=FIXED_NOW,
    )


def stricter_than(policy):
    return make_policy(order={**policy.order.model_dump(mode="json"), "max_notional": "0.01"})


# ------------------------------------------------------------------------------------------------
# Exact mode
# ------------------------------------------------------------------------------------------------


@requires_engine
def test_exact_decision_replay_reproduces_the_recorded_decision():
    record = engine_record()
    result = decision_replay.replay(record)

    assert result.mode == "exact"
    assert result.identical is True, result.detail
    assert result.decision_id == record.decision_id
    assert result.original_verdict == record.verdict
    assert result.replayed_verdict == record.verdict
    assert result.original_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
    assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert canonical_json(result.replayed_evaluation) == canonical_json(record.risk_evaluation)
    assert result.engine_version_matches is True
    assert result.recorded_engine_version == result.running_engine_version == ENGINE_VERSION
    assert "decision replay" in result.detail


@requires_engine
def test_only_the_decision_id_differs_and_the_docstring_says_so():
    """Everything except ``decision_id`` is compared; the governor mints a fresh one every call."""
    record = engine_record()
    result = decision_replay.replay(record)

    assert result.replayed_decision.decision_id != record.decision_id
    original = record.governor_decision.model_dump(mode="json")
    replayed = result.replayed_decision.model_dump(mode="json")
    assert original.pop("decision_id") != replayed.pop("decision_id")
    assert canonical_json(original) == canonical_json(replayed)
    assert "byte-identical once decision_id is set aside" in result.detail
    assert "decision_id" in decision_replay.replay.__doc__


@requires_engine
def test_decision_replay_is_idempotent():
    record = engine_record()
    first = decision_replay.replay(record)
    second = decision_replay.replay(record)

    assert first.identical is second.identical is True
    assert first.replayed_verdict_hash == second.replayed_verdict_hash
    assert canonical_json(first.replayed_evaluation) == canonical_json(second.replayed_evaluation)
    assert first.detail == second.detail


@requires_engine
def test_a_record_read_back_out_of_the_ledger_replays_identically():
    tenant_ledger = InMemoryLedger().for_tenant(TENANT_A)
    record = engine_record(tenant_ledger=tenant_ledger)
    stored = tenant_ledger.get(record.decision_id)

    assert canonical_json(stored) == canonical_json(record)
    assert decision_replay.replay(stored).identical is True


@requires_engine
def test_a_recorded_advisory_opinion_is_used_again_by_default():
    concurring = make_advisory(recommendation="CONCUR")
    record = engine_record(advisory=concurring)
    assert record.llm_advisory is not None

    result = decision_replay.replay(record)
    assert result.mode == "exact"
    assert result.identical is True, result.detail


# ------------------------------------------------------------------------------------------------
# Policy mode
# ------------------------------------------------------------------------------------------------


@requires_engine
def test_policy_mode_is_a_real_reevaluation_not_a_hash_comparison():
    policy = make_policy()
    record = engine_record(policy=policy)
    assert record.verdict != "REJECT"
    strict = stricter_than(policy)
    assert strict.policy_hash != policy.policy_hash

    result = decision_replay.replay(record, policy=strict)

    assert result.mode == "policy"
    assert result.identical is False
    assert result.replayed_verdict == "REJECT"
    assert result.original_verdict == record.verdict
    assert result.replayed_evaluation.policy.hash == strict.policy_hash
    assert result.replayed_decision.policy.hash == strict.policy_hash
    codes = {str(code) for code in result.replayed_reason_codes}
    assert not codes & {"POLICY_HASH_MISMATCH", "TENANT_MISMATCH"}, codes
    assert "policy decision replay" in result.detail


@requires_engine
def test_policy_mode_with_the_recorded_policy_still_reproduces_the_decision():
    """Passing the same policy back is still policy mode, and must not change the answer."""
    record = engine_record()
    result = decision_replay.replay(record, policy=record.policy_snapshot)

    assert result.mode == "policy"
    assert result.identical is True, result.detail
    assert result.replayed_verdict_hash == record.governor_decision.verdict_hash


@requires_engine
def test_policy_mode_leaves_every_other_recorded_input_alone():
    record = engine_record()
    strict = stricter_than(record.policy_snapshot)
    result = decision_replay.replay(record, policy=strict)

    # only the policy reference moved; the market, portfolio and path inputs are the recorded ones
    assert result.replayed_evaluation.context_id == record.risk_context.context_id
    assert result.replayed_evaluation.evaluated_at == record.risk_context.evaluated_at
    assert result.replayed_evaluation.proposal_id == record.proposal_id


# ------------------------------------------------------------------------------------------------
# The semantic layer (Addendum 1 section D)
# ------------------------------------------------------------------------------------------------


@requires_engine
def test_advisory_none_switches_the_semantic_layer_off_for_this_decision_replay():
    record = engine_record(advisory=make_advisory(recommendation="CONCUR"))
    result = decision_replay.replay(record, advisory=None)

    assert result.mode == "counterfactual"
    assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id
    assert canonical_json(result.replayed_evaluation) == canonical_json(record.risk_evaluation)
    assert result.replayed_verdict == record.verdict
    assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
    assert "semantic layer disabled" in result.detail


@requires_engine
def test_without_the_semantic_layer_a_decision_replay_never_authorizes_less():
    """The advisory layer is downward-only, so removing it can only ever restore quantity."""
    policy = make_policy()
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT"
    reduced = governor.govern(
        proposal,
        evaluation,
        policy,
        make_advisory(recommendation="REDUCE", recommended_quantity="1"),
        context=context,
    )
    record = InMemoryLedger().for_tenant(TENANT_A).append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=reduced,
        policy_snapshot=policy,
        recorded_at=FIXED_NOW,
    )

    result = decision_replay.replay(record, advisory=None)
    assert dec(result.replayed_decision.authorized.total_quantity) >= dec(
        record.authorized.total_quantity
    )
    assert result.replayed_decision.authorized.total_quantity == evaluation.recommended_quantity


@requires_engine
def test_substituting_an_advisory_opinion_is_a_counterfactual():
    record = engine_record()
    rejecting = make_advisory(recommendation="REJECT", recommended_quantity=None)

    result = decision_replay.replay(record, advisory=rejecting)
    assert result.mode == "counterfactual"
    assert "semantic layer substituted" in result.detail
    assert result.replayed_verdict == "REJECT"
    assert result.identical is False


def test_an_unknown_advisory_sentinel_is_refused():
    record = _fixture_record()
    with pytest.raises(ValidationFailed):
        decision_replay.replay(record, advisory="whatever")


# ------------------------------------------------------------------------------------------------
# Version guards
# ------------------------------------------------------------------------------------------------


def _fixture_record(**overrides) -> DecisionRecord:
    """A record built from fixture objects only, so these tests do not need the engine."""
    from tests.audit._helpers import append_record

    return append_record(InMemoryLedger().for_tenant(TENANT_A), **overrides)


def _rebuilt_with(record: DecisionRecord, **fields) -> DecisionRecord:
    payload = {k: v for k, v in record.model_dump(mode="json").items() if k != "audit_hash"}
    payload.update(fields)
    return DecisionRecord.build(**payload)


@requires_engine
def test_an_engine_version_mismatch_is_reported_loudly_and_never_silently():
    """A1 promises an identical verdict for the SAME engine version. A mismatch must be visible."""
    policy = make_policy()
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = make_proposal()
    old = "mizan-core/0.0.1-ancient"
    evaluation = make_evaluation(
        proposal=proposal, context=context, policy_snapshot=policy, engine_version=old
    )
    decision = make_decision(proposal=proposal, evaluation=evaluation, decision_id=uuid7())
    record = InMemoryLedger().for_tenant(TENANT_A).append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        policy_snapshot=policy,
        recorded_at=FIXED_NOW,
    )
    assert record.engine_version == old

    result = decision_replay.replay(record)
    assert result.engine_version_matches is False
    assert result.recorded_engine_version == old
    assert result.running_engine_version == ENGINE_VERSION
    assert "ENGINE VERSION MISMATCH" in result.detail
    assert old in result.detail and ENGINE_VERSION in result.detail


@requires_engine
def test_library_version_drift_is_reported_as_well():
    record = engine_record()
    drifted = _rebuilt_with(
        record, library_versions={**record.library_versions, "pydantic": "0.0.0-not-what-ran"}
    )
    result = decision_replay.replay(drifted)
    assert "library version drift" in result.detail
    assert "pydantic 0.0.0-not-what-ran" in result.detail


@requires_engine
def test_an_exact_decision_replay_that_differs_says_so_in_capitals():
    """If A1 is ever broken, the result must not read like a quiet 'False'."""
    record = engine_record()
    forged = _rebuilt_with(
        record,
        risk_context={
            **record.risk_context.model_dump(mode="json"),
            "response_level": 5,
        },
    )
    result = decision_replay.replay(forged)
    assert result.identical is False
    assert "HARD RULE A1" in result.detail
    assert "verify_chain" in result.detail


def test_the_result_carries_both_sides_of_the_comparison_and_is_frozen():
    """A ReplayResult is a self-contained answer: original and replayed, side by side, immutable."""
    names = set(ReplayResult.model_fields)
    assert {
        "original_verdict",
        "replayed_verdict",
        "original_reason_codes",
        "replayed_reason_codes",
        "original_verdict_hash",
        "replayed_verdict_hash",
        "replayed_evaluation",
        "replayed_decision",
        "detail",
        "engine_version_matches",
        "recorded_engine_version",
        "running_engine_version",
    } <= names
    assert ReplayResult.model_config["frozen"] is True
    assert ReplayResult.model_config["extra"] == "forbid"
