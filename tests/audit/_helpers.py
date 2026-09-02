"""Chain builders for the audit and decision-replay tests, assembled only from ``tests.fixtures``.

Not a test module: it defines no test and no pytest fixture, so it can be imported from both
``tests/audit`` and ``tests/replay`` under ``--import-mode=importlib``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, NamedTuple

from mizan.contracts import (
    AdvisoryOpinion,
    DecisionRecord,
    ExecutionAuthorization,
    ExecutionResult,
    GovernorDecision,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)
from mizan.contracts.canonical import uuid7
from tests.fixtures import (
    FIXED_NOW,
    make_authorization,
    make_context,
    make_decision,
    make_evaluation,
    make_execution_result,
    make_policy,
    make_proposal,
)


class ChainParts(NamedTuple):
    """One consistent object chain: proposal -> context -> evaluation -> decision (-> auth -> result)."""

    proposal: TradeProposal
    policy: Policy
    context: RiskContext
    evaluation: RiskEvaluation
    decision: GovernorDecision
    authorization: ExecutionAuthorization | None
    execution: ExecutionResult | None


def chain_parts(
    *,
    tenant_id: str | None = None,
    proposal: TradeProposal | None = None,
    policy: Policy | None = None,
    context: RiskContext | None = None,
    advisory: AdvisoryOpinion | None = None,
    authorized: bool = False,
    **decision_overrides: Any,
) -> ChainParts:
    """A fully linked object chain built from the shared fixture builders."""
    policy = policy or make_policy(**({"tenant_id": tenant_id} if tenant_id else {}))
    context = context or make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = proposal or make_proposal()
    evaluation = make_evaluation(proposal=proposal, context=context, policy_snapshot=policy)
    decision = make_decision(
        proposal=proposal,
        evaluation=evaluation,
        decision_id=uuid7(),
        llm_advisory=advisory,
        **decision_overrides,
    )
    authorization = None
    execution = None
    if authorized:
        authorization = make_authorization(
            proposal=proposal, decision=decision, context=context, policy_snapshot=policy
        )
        execution = make_execution_result(authorization=authorization)
    return ChainParts(proposal, policy, context, evaluation, decision, authorization, execution)


def append_record(
    tenant_ledger: Any, *, recorded_at: datetime = FIXED_NOW, **parts: Any
) -> DecisionRecord:
    """Append one fixture-built decision through the public ``TenantLedger.append``."""
    built = parts.pop("parts", None) or chain_parts(**parts)
    return tenant_ledger.append(
        proposal=built.proposal,
        risk_context=built.context,
        risk_evaluation=built.evaluation,
        governor_decision=built.decision,
        policy_snapshot=built.policy,
        authorization=built.authorization,
        execution=built.execution,
        recorded_at=recorded_at,
    )


def all_rows(db_path: Any, table: str = "decision_records") -> list[tuple[Any, ...]]:
    """Every row of ``table``, through a raw connection, in sequence order."""
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            f"SELECT sequence, audit_prev_hash, audit_hash, tenant_id, record_json, recorded_at "
            f"FROM {table} ORDER BY sequence"
        ).fetchall()
    finally:
        connection.close()


def drop_every_trigger(db_path: Any) -> list[str]:
    """Remove the append-only triggers, the way an attacker with file access would. Returns their names."""
    connection = sqlite3.connect(db_path)
    try:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ]
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.commit()
        return names
    finally:
        connection.close()
