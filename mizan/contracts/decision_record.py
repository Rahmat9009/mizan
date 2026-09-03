"""DecisionRecord: one link of the per-tenant hash chain. Everything needed to replay the decision is embedded."""

from __future__ import annotations

from collections.abc import Mapping  # noqa: F401
from typing import Any

from pydantic import Field, ValidationInfo, model_validator

from mizan.contracts._base import verify_presented_hash, ContractModel, build_hashed
from mizan.contracts.canonical import ZERO_HASH, record_hash_for
from mizan.contracts.execution_authorization import ExecutionAuthorization
from mizan.contracts.execution_result import ExecutionResult
from mizan.contracts.governor_decision import AdvisoryOpinion, Authorized, GovernorDecision, GovernorVerdict, Quantities
from mizan.contracts.policy import Policy
from mizan.contracts.reason_codes import ReasonCodeList
from mizan.contracts.risk_context import PolicyRef, RiskContext
from mizan.contracts.risk_evaluation import CheckResult, RiskEvaluation
from mizan.contracts.trade_proposal import TradeProposal
from mizan.contracts.types import AgentId, NonEmptyStr, Rfc3339, SchemaVersion, Sha256Hex, TenantId, Uuid7Str


class DecisionRecord(ContractModel):
    schema_version: SchemaVersion
    decision_id: Uuid7Str
    sequence: int = Field(ge=1)
    tenant_id: TenantId
    agent_id: AgentId
    proposal_id: Sha256Hex
    engine_version: NonEmptyStr
    library_versions: dict[NonEmptyStr, NonEmptyStr] = Field(min_length=1)
    policy: PolicyRef
    policy_snapshot: Policy
    decision_timestamp: Rfc3339
    verdict: GovernorVerdict
    reason_codes: ReasonCodeList
    checks: list[CheckResult]
    proposal: TradeProposal
    risk_context: RiskContext
    risk_evaluation: RiskEvaluation
    governor_decision: GovernorDecision
    authorization: ExecutionAuthorization | None
    execution: ExecutionResult | None
    original: Quantities
    authorized: Authorized
    llm_advisory: AdvisoryOpinion | None
    recorded_at: Rfc3339
    audit_prev_hash: Sha256Hex
    audit_hash: Sha256Hex

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> DecisionRecord:
        decision = self.governor_decision
        evaluation = self.risk_evaluation
        context = self.risk_context
        proposal = self.proposal
        if self.decision_id != decision.decision_id:
            raise ValueError("decision_id must equal governor_decision.decision_id")
        mirrored = (
            ("verdict", self.verdict, decision.verdict),
            ("reason_codes", self.reason_codes, decision.reason_codes),
            ("original", self.original, decision.original),
            ("authorized", self.authorized, decision.authorized),
            ("llm_advisory", self.llm_advisory, decision.llm_advisory),
            ("decision_timestamp", self.decision_timestamp, decision.decision_timestamp),
            ("checks", self.checks, evaluation.checks),
        )
        for name, ours, theirs in mirrored:
            if ours != theirs:
                raise ValueError(f"{name} must equal the embedded decision's {name}")
        if not (self.proposal_id == proposal.proposal_id == evaluation.proposal_id == decision.proposal_id):
            raise ValueError("proposal_id must be identical across the record, proposal, evaluation and decision")
        tenants = {self.tenant_id, context.tenant_id, evaluation.tenant_id, decision.tenant_id, self.policy_snapshot.tenant_id}
        if len(tenants) != 1:
            raise ValueError("tenant_id must be identical across every embedded object")
        if not (self.agent_id == proposal.agent.agent_id == context.agent_id == decision.agent_id):
            raise ValueError("agent_id must be identical across the record, proposal, context and decision")
        if not (self.policy == self.policy_snapshot.ref == context.policy == evaluation.policy == decision.policy):
            raise ValueError("policy must equal policy_snapshot.ref and every embedded policy reference")
        if decision.evaluation_id != evaluation.evaluation_id:
            raise ValueError("governor_decision.evaluation_id must equal risk_evaluation.evaluation_id")
        if evaluation.context_id != context.context_id:
            raise ValueError("risk_evaluation.context_id must equal risk_context.context_id")
        if not (self.engine_version == evaluation.engine_version == decision.engine_version):
            raise ValueError("engine_version must equal the evaluation's and the decision's")
        authorization = self.authorization
        if authorization is not None:
            if self.verdict == "REJECT":
                raise ValueError("a REJECT record cannot carry an authorization")
            if authorization.decision_id != self.decision_id or authorization.proposal_id != self.proposal_id:
                raise ValueError("authorization must reference this record's decision_id and proposal_id")
            if authorization.tenant_id != self.tenant_id or authorization.policy != self.policy:
                raise ValueError("authorization must carry this record's tenant_id and policy")
        execution = self.execution
        if execution is not None:
            if authorization is None:
                raise ValueError("an execution result requires an authorization")
            if execution.auth_id != authorization.auth_id or execution.decision_id != self.decision_id:
                raise ValueError("execution must reference this record's authorization and decision")
            if execution.tenant_id != self.tenant_id or execution.proposal_id != self.proposal_id:
                raise ValueError("execution must carry this record's tenant_id and proposal_id")
        if (self.sequence == 1) != (self.audit_prev_hash == ZERO_HASH):
            raise ValueError("audit_prev_hash is ZERO_HASH exactly when sequence == 1")
        return self

    @model_validator(mode="wrap")
    @classmethod
    def _hash_covers_the_content_as_presented(
        cls, data: Any, handler: Any, info: ValidationInfo
    ) -> DecisionRecord:
        """See :func:`verify_presented_hash` - the hash covers what was written, not what we'd write."""
        model: DecisionRecord = handler(data)
        verify_presented_hash(
            model, data, info, field="audit_hash", compute=record_hash_for, message="audit_hash does not match record_hash_for(record)"
        )
        return model

    @classmethod
    def build(cls, **fields: Any) -> DecisionRecord:
        """Construct a record, computing ``audit_hash`` from the normalised content."""
        return build_hashed(cls, "audit_hash", record_hash_for, fields)


__all__ = ["DecisionRecord"]
