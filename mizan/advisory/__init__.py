"""L2 — the optional AI advisory layer.

This is the only enforcement-adjacent module permitted to read a proposal's free text, and it has no
authority: it produces an opinion that the governor may act on downward, or ignore. Everything here is
built on the assumption that the provider is unreliable and possibly adversarial — malformed JSON, extra
fields, a truncated response, a quantity above the cap, or a claim of higher authority are all normal
inputs, not exceptional ones.

``get_advisory`` never raises. A provider failure produces an unavailable opinion, because an LLM outage
must never mean the risk system is unavailable (Hard Rule E8).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mizan.contracts import (
    AdvisoryOpinion,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)

__all__ = [
    "AdvisoryProvider",
    "OfflineAdvisoryProvider",
    "OpenAICompatibleAdvisoryProvider",
    "get_advisory",
]


@runtime_checkable
class AdvisoryProvider(Protocol):
    """May raise, may hang, may return nonsense. The caller is built for all three."""

    def advise(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion: ...


def get_advisory(
    provider: AdvisoryProvider | None,
    proposal: TradeProposal,
    evaluation: RiskEvaluation,
    context: RiskContext,
    policy: Policy,
    *,
    timeout_seconds: int = 10,
) -> AdvisoryOpinion:
    """Consult ``provider`` defensively and return an opinion that is always safe to act on.

    Any exception, timeout, schema violation or out-of-range quantity becomes an opinion with
    ``available=False``. A quantity above ``evaluation.recommended_quantity`` is clamped before the
    opinion leaves this function, so nothing downstream has to trust the provider.
    """
    raise NotImplementedError("L2 implements this in Sprint 2")


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
        raise NotImplementedError("L2 implements this in Sprint 2")


class OpenAICompatibleAdvisoryProvider:
    """JSON-mode provider for any OpenAI-compatible endpoint (Featherless and friends).

    The client is constructed lazily so that importing this module never opens a socket and never
    requires a key — the deterministic engine must import cleanly on a machine with no provider at all.
    """

    def __init__(
        self,
        *,
        profile: str = "standard_advisory",
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.profile = profile
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds

    def advise(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion:
        raise NotImplementedError("L2 implements this in Sprint 2")
