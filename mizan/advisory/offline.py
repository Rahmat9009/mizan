"""The deterministic, network-free advisory provider.

Every demo, every air-gapped deployment and most of the test suite runs on this. It exists so that the
advisory *seam* can be exercised — and shown to have no authority — without a model, a key or a socket.
"""

from __future__ import annotations

from mizan.contracts import (
    AdvisoryOpinion,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)

__all__ = ["OfflineAdvisoryProvider"]

_CONCUR_NOTE = (
    "Offline advisory profile: the deterministic evaluation is complete and did not reject; "
    "the advisory concurs with the deterministic recommendation and adds no size."
)
_REJECT_NOTE = (
    "Offline advisory profile: the deterministic evaluation rejected or its data was incomplete; "
    "the conservative offline opinion is to reject."
)


class OfflineAdvisoryProvider:
    """Deterministic, network-free provider for tests, demos and air-gapped deployments."""

    profile = "offline"

    def __init__(self, *, opinion: AdvisoryOpinion | None = None) -> None:
        self.opinion = opinion

    def advise(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion:
        """Return the scripted opinion, or derive a conservative one from the evaluation alone.

        The derived opinion is a function of the deterministic evaluation and nothing else: no clock, no
        randomness, no free text. It is therefore replayable, and it can never be more permissive than
        the deterministic recommendation — CONCUR *is* that recommendation, and the only other answer
        this provider gives is REJECT.
        """
        if self.opinion is not None:
            return self.opinion
        conservative = evaluation.verdict == "REJECT" or not evaluation.data_complete
        return AdvisoryOpinion(
            profile=self.profile,
            invoked=True,
            available=True,
            recommendation="REJECT" if conservative else "CONCUR",
            recommended_quantity=None,
            reasoning=_REJECT_NOTE if conservative else _CONCUR_NOTE,
            authority_ceiling="reduce_or_reject",
            provider_ref="offline:deterministic",
            raw_hash=None,
        )
