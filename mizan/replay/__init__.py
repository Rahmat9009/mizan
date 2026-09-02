"""L2 - decision replay.

Say "decision replay", never bare "replay": the term already means chart playback in retail trading
tools, and the distinction matters in every customer conversation.

Exact decision replay re-runs the deterministic engine over a record's own captured inputs and must
reach a byte-identical verdict (Hard Rule A1). Policy decision replay re-runs it against a different
policy, which is how you answer "would today's rules have stopped last quarter's order?". Everything the
intelligence tier eventually does - agent certification, blast-radius analysis, counterfactual policy
testing, model regression - runs on this function.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import ConfigDict

from mizan import governor, risk
from mizan.contracts import (
    AdvisoryOpinion,
    ContractModel,
    DecisionRecord,
    GovernorDecision,
    Policy,
    ReasonCode,
    ReasonCodeList,
    RiskContext,
    RiskEvaluation,
    Sha256Hex,
)
from mizan.contracts.canonical import ENGINE_VERSION, canonical_json, library_versions
from mizan.contracts.errors import ValidationFailed

__all__ = ["ReplayResult", "replay"]

#: Sentinel meaning "use the advisory opinion the record captured", as opposed to ``None``, which means
#: "run this decision replay with the semantic layer switched off entirely".
AS_RECORDED: Literal["as_recorded"] = "as_recorded"


class ReplayResult(ContractModel):
    """What a decision replay produced, and whether it matched the original."""

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
    #: Prose for a human: what matched, what did not, and every reason the comparison might be weaker
    #: than it looks (engine version drift above all). Never empty.
    detail: str = ""
    engine_version_matches: bool = True
    recorded_engine_version: str = ""
    running_engine_version: str = ""


def _code_value(code: ReasonCode | str) -> str:
    return str(code.value) if isinstance(code, Enum) else str(code)


def _codes(source: Any) -> list[str]:
    return sorted({_code_value(code) for code in source})


def _rebound_context(record: DecisionRecord, policy: Policy) -> RiskContext:
    """The recorded context with its policy reference swapped for ``policy``'s.

    Without this, policy decision replay would answer the wrong question: the engine compares
    ``policy.policy_hash`` against ``context.policy.hash`` and would reject on POLICY_HASH_MISMATCH
    before a single limit was evaluated. Only the reference moves; every input the decision actually
    turned on - prices, positions, path state, aggregate exposure, the calendar - is the recorded one.
    """
    payload = record.risk_context.model_dump(mode="json")
    payload["policy"] = policy.ref.model_dump(mode="json")
    return RiskContext.model_validate(payload)


def _library_drift(record: DecisionRecord) -> list[str]:
    running = library_versions()
    drift: list[str] = []
    for name, recorded in sorted(record.library_versions.items()):
        current = running.get(name)
        if current is not None and current != recorded:
            drift.append(f"{name} {recorded} -> {current}")
    return drift


def replay(
    record: DecisionRecord,
    *,
    policy: Policy | None = None,
    advisory: AdvisoryOpinion | None | Literal["as_recorded"] = AS_RECORDED,
) -> ReplayResult:
    """Re-run the engine over a record's captured inputs.

    With no overrides this is exact decision replay and ``identical`` must be True; if it is not, either
    the record or the engine has changed, and both are worth knowing about immediately. Supplying a
    policy switches to policy mode, where a differing verdict is the answer rather than a failure -
    ``identical`` then merely reports whether it happened to come out the same. Passing
    ``advisory=None`` switches the semantic layer off for this decision replay (Addendum 1 section D):
    the deterministic part must reproduce exactly, and the advisory layer, being downward-only, can only
    ever have authorized less.

    **What is compared.** Everything except ``decision_id`` and anything derived from it. The governor
    mints a fresh ``decision_id`` (a uuid7) on every call, so the identifier of a decision-replayed
    decision necessarily differs from the recorded one; that difference proves nothing and is excluded.
    Nothing else is: ``verdict_hash`` is derived from the verdict, the reason codes, the authorized
    quantity, the authorized legs and the ``evaluation_id`` - never from ``decision_id`` - and
    ``decision_timestamp`` comes from the recorded context, so both are compared in full. ``identical``
    is True only when the verdict, the reason codes AND the ``verdict_hash`` all match, compared
    canonically rather than by object identity. The rest of the decision is compared too and any
    difference is named in ``detail``.

    **Engine and library versions.** Hard Rule A1 promises an identical verdict for the same inputs, the
    same policy version and the same *engine* version. When ``record.engine_version`` differs from the
    running ``ENGINE_VERSION`` the decision replay still runs - that comparison is exactly what a
    regression check wants - but ``engine_version_matches`` is False and ``detail`` says so loudly. A
    silent mismatch would let a coincidental match be reported as proof, which is worse than a warning.
    """
    if isinstance(advisory, str):
        if advisory != AS_RECORDED:
            raise ValidationFailed(
                detail=f"advisory must be an AdvisoryOpinion, None, or {AS_RECORDED!r}; got {advisory!r}"
            )
        opinion = record.llm_advisory
        semantic_layer_overridden = False
    else:
        opinion = advisory
        semantic_layer_overridden = True

    if policy is not None:
        mode: Literal["exact", "policy", "counterfactual"] = "policy"
        effective_policy = policy
        context = _rebound_context(record, policy)
    else:
        mode = "counterfactual" if semantic_layer_overridden else "exact"
        effective_policy = record.policy_snapshot
        context = record.risk_context

    evaluation = risk.evaluate(record.proposal, context, effective_policy)
    decision = governor.govern(record.proposal, evaluation, effective_policy, opinion, context=context)

    original = record.governor_decision
    original_codes = _codes(original.reason_codes)
    replayed_codes = _codes(decision.reason_codes)
    identical = (
        decision.verdict == original.verdict
        and replayed_codes == original_codes
        and decision.verdict_hash == original.verdict_hash
    )

    recorded_engine = record.engine_version
    engine_matches = recorded_engine == ENGINE_VERSION
    notes: list[str] = []

    if mode == "exact":
        notes.append(
            "exact decision replay: the recorded proposal, risk context, policy snapshot and advisory "
            "opinion, re-run through the same engine."
        )
    elif mode == "policy":
        notes.append(
            f"policy decision replay against {effective_policy.policy_id} "
            f"{effective_policy.policy_version} ({effective_policy.policy_hash[:12]}...): a differing "
            "verdict is the answer, not a failure."
        )
    else:
        state = "disabled" if opinion is None else "substituted"
        notes.append(
            f"counterfactual decision replay with the semantic layer {state}; the deterministic "
            "evaluation is unaffected, because an advisory opinion is not an input to it."
        )

    if identical:
        notes.append("verdict, reason codes and verdict_hash all reproduce exactly.")
    else:
        differences = []
        if decision.verdict != original.verdict:
            differences.append(f"verdict {original.verdict} -> {decision.verdict}")
        if replayed_codes != original_codes:
            added = sorted(set(replayed_codes) - set(original_codes))
            dropped = sorted(set(original_codes) - set(replayed_codes))
            differences.append(f"reason codes +{added} -{dropped}")
        if decision.verdict_hash != original.verdict_hash:
            differences.append(
                f"verdict_hash {original.verdict_hash[:12]}... -> {decision.verdict_hash[:12]}..."
            )
        notes.append("differences: " + "; ".join(differences) + ".")
        if mode == "exact":
            notes.append(
                "AN EXACT DECISION REPLAY THAT DIFFERS IS A FAILURE OF HARD RULE A1: either this record "
                "was altered after it was written, or the engine no longer decides the way it did. "
                "Verify the chain (python -m mizan.audit.verify_chain) before trusting either."
            )

    replayed_content = decision.model_dump(mode="json")
    original_content = original.model_dump(mode="json")
    replayed_content.pop("decision_id")
    original_content.pop("decision_id")
    if canonical_json(replayed_content) != canonical_json(original_content):
        if identical:
            notes.append(
                "the decision differs outside the verdict, the reason codes and the verdict_hash "
                "(decision_id excluded, as always); compare the two decisions field by field."
            )
    elif mode == "exact":
        notes.append("the whole decision is byte-identical once decision_id is set aside.")

    if not engine_matches:
        notes.append(
            f"ENGINE VERSION MISMATCH: the record was written by {recorded_engine} and this decision "
            f"replay ran on {ENGINE_VERSION}. Hard Rule A1 guarantees an identical verdict only for the "
            "same engine version, so a match here is not proof and a difference is not necessarily a "
            "defect - it is a version comparison."
        )
    drift = _library_drift(record)
    if drift:
        notes.append(
            "library version drift since the record was written: " + ", ".join(drift) + "."
        )

    return ReplayResult(
        decision_id=record.decision_id,
        mode=mode,
        identical=identical,
        original_verdict=original.verdict,
        replayed_verdict=decision.verdict,
        original_reason_codes=list(original.reason_codes),
        replayed_reason_codes=list(decision.reason_codes),
        original_verdict_hash=original.verdict_hash,
        replayed_verdict_hash=decision.verdict_hash,
        replayed_evaluation=evaluation,
        replayed_decision=decision,
        detail=" ".join(notes),
        engine_version_matches=engine_matches,
        recorded_engine_version=recorded_engine,
        running_engine_version=ENGINE_VERSION,
    )
