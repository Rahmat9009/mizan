"""L2 — arbitration between the deterministic evaluation and the optional advisory opinion.

The governor is where Hard Rule E1 becomes code: the advisory layer can only make the outcome equally or
more conservative. It cannot approve, it cannot upsize, and it cannot overturn a deterministic rejection.
The contract types already make "more" unrepresentable; this module makes sure nothing in the arithmetic
reintroduces it.

The governor never reads free text. Its inputs are a verdict, a quantity and a set of codes.
"""

from __future__ import annotations

from mizan.contracts import (
    AdvisoryOpinion,
    GovernorDecision,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)

__all__ = ["govern"]


def govern(
    proposal: TradeProposal,
    evaluation: RiskEvaluation,
    policy: Policy,
    advisory: AdvisoryOpinion | None,
    *,
    context: RiskContext,
) -> GovernorDecision:
    """Combine the deterministic evaluation with an optional advisory opinion.

    The authorized quantity is always at most ``evaluation.recommended_quantity``. A deterministic
    REJECT stands regardless of what the advisory said. An advisory that is missing or unavailable is
    handled by ``policy.fail_closed.on_advisory_unavailable``: advisory is optional, enforcement is not.
    """
    raise NotImplementedError("L2 implements this in Sprint 2")
