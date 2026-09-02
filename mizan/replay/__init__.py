"""L2 — decision replay.

Say "decision replay", never bare "replay": the term already means chart playback in retail trading
tools, and the distinction matters in every customer conversation.

Exact replay re-runs the deterministic engine over a record's own captured inputs and must reach a
byte-identical verdict (Hard Rule A1). Policy replay re-runs it against a different policy, which is how
you answer "would today's rules have stopped last quarter's order?". Everything the intelligence tier
eventually does — agent certification, blast-radius analysis, counterfactual policy testing, model
regression — runs on this function.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from mizan.contracts import (
    AdvisoryOpinion,
    ContractModel,
    DecisionRecord,
    GovernorDecision,
    Policy,
    ReasonCodeList,
    RiskEvaluation,
    Sha256Hex,
)

__all__ = ["ReplayResult", "replay"]


class ReplayResult(ContractModel):
    """What a replay produced, and whether it matched the original."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision_id: str
    mode: Literal["exact", "policy", "counterfactual"]
    identical: bool
    original_verdict: str
    replayed_verdict: str
    original_reason_codes: ReasonCodeList = []
    replayed_reason_codes: ReasonCodeList = []
    original_verdict_hash: Sha256Hex
    replayed_verdict_hash: Sha256Hex
    replayed_evaluation: RiskEvaluation
    replayed_decision: GovernorDecision


def replay(
    record: DecisionRecord,
    *,
    policy: Policy | None = None,
    advisory: AdvisoryOpinion | Literal["as_recorded"] = "as_recorded",
) -> ReplayResult:
    """Re-run the engine over a record's captured inputs.

    With no overrides this is exact replay and ``identical`` must be True; if it is not, either the
    record or the engine has changed, and both are worth knowing about immediately. Supplying a policy
    switches to policy replay, where a differing verdict is the answer rather than a failure.
    """
    raise NotImplementedError("L2 implements this in Sprint 2")
