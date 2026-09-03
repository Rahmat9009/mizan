"""The seam: how a volatility reading reaches the audit record without acquiring any authority.

``VolSignalAdvisoryProvider`` is an ``AdvisoryProvider`` (API-SURFACE section 3.4) that wraps another
provider - or none - and changes exactly one field of its opinion: ``reasoning``. The recommendation,
the recommended quantity, the authority ceiling and the availability flag are passed through byte for
byte from the wrapped provider.

That field is the correct place for this and no contract had to change to allow it:

* ``reasoning`` is free text that the deterministic path is *structurally forbidden* to read. Invariant
  17 scans mizan/risk, mizan/governor, mizan/policy, mizan/authorization and mizan/execution for any
  attribute, subscript, ``getattr`` or string constant naming it, so a future check that tried to read
  the signal would fail that invariant rather than quietly start trading on it.
* ``verdict_hash`` is computed from the verdict, the reason codes, the authorized quantity, the
  authorized legs and the evaluation id. ``reasoning`` is not an input to it, so the text cannot move a
  verdict even by accident.

Therefore: the reading is recorded, the reading is replayable, and the reading decides nothing.

The provider also never raises. A failure to produce a reading yields the wrapped provider's opinion
untouched, which is the same opinion the loop would have had if this package were not installed at all.
"""

from __future__ import annotations

from typing import Any, Protocol

from mizan.contracts import (
    AdvisoryOpinion,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
)
from mizan.contracts.trade_proposal import MAX_REASONING_CHARS
from mizan.signal.shadow import shadow_enabled
from mizan.signal.vol import VolSignal

__all__ = ["SHADOW_PROFILE", "VolSignalAdvisoryProvider", "annotate"]

SHADOW_PROFILE = "vol-signal-shadow"
_SEPARATOR = " || "
_NO_SIGNAL = (
    "vol-signal-shadow: no reading available (bars absent or insufficient); "
    "advisory metadata unchanged."
)


class _Advises(Protocol):
    def advise(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion: ...


def _unavailable(profile: str, detail: str) -> AdvisoryOpinion:
    """What a wrapped provider of ``None`` looks like: no recommendation at all, exactly as before."""
    return AdvisoryOpinion(
        profile=profile,
        invoked=True,
        available=False,
        recommendation=None,
        recommended_quantity=None,
        reasoning=detail[:MAX_REASONING_CHARS],
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def annotate(opinion: AdvisoryOpinion, note: str) -> AdvisoryOpinion:
    """Return ``opinion`` with ``note`` appended to its reasoning and every other field unchanged.

    Rebuilt through the contract rather than copied around it, so an annotation that could not be a
    valid opinion fails here instead of somewhere downstream.
    """
    existing = opinion.reasoning or ""
    combined = f"{existing}{_SEPARATOR}{note}" if existing else note
    return AdvisoryOpinion(
        profile=opinion.profile,
        invoked=opinion.invoked,
        available=opinion.available,
        recommendation=opinion.recommendation,
        recommended_quantity=opinion.recommended_quantity,
        reasoning=combined[:MAX_REASONING_CHARS],
        authority_ceiling=opinion.authority_ceiling,
        provider_ref=opinion.provider_ref,
        raw_hash=opinion.raw_hash,
    )


class VolSignalAdvisoryProvider:
    """Passes a wrapped provider's opinion through, adding the volatility reading as advisory text.

    ``signal`` is a reading computed elsewhere (fetch bars, call ``compute_vol_signal``, hand the
    result in). ``base`` is the provider that would otherwise have been used; when it is ``None`` the
    opinion is unavailable, which is what the loop already does with no provider configured.

    With ``SIGNAL_SHADOW`` unset, ``advise`` returns the wrapped opinion verbatim.
    """

    def __init__(
        self,
        signal: VolSignal | None = None,
        *,
        base: _Advises | None = None,
        profile: str = SHADOW_PROFILE,
    ) -> None:
        self.signal = signal
        self.base = base
        self.profile = profile
        self.annotations = 0

    # -- the wrapped opinion -------------------------------------------------------------------
    def _base_opinion(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion:
        if self.base is None:
            return _unavailable(self.profile, "no advisory provider is configured")
        result: Any = self.base.advise(proposal, evaluation, context, policy)
        if isinstance(result, AdvisoryOpinion):
            return result
        if isinstance(result, dict):
            return AdvisoryOpinion.model_validate(result)
        raise TypeError(f"wrapped provider returned {type(result).__name__}, not an AdvisoryOpinion")

    def note(self) -> str:
        """The advisory line for the current reading, or the explicit statement that there is none."""
        signal = self.signal
        if signal is None:
            return _NO_SIGNAL
        return signal.summary()

    def advise(
        self,
        proposal: TradeProposal,
        evaluation: RiskEvaluation,
        context: RiskContext,
        policy: Policy,
    ) -> AdvisoryOpinion:
        """The wrapped opinion, plus one line of text when the shadow flag is on. Nothing else."""
        opinion = self._base_opinion(proposal, evaluation, context, policy)
        if not shadow_enabled():
            return opinion
        try:
            annotated = annotate(opinion, self.note())
        except Exception:
            # A reading that cannot be rendered is a reading nobody sees. It is never a reason to
            # change, delay or fail a governance decision (Hard Rule E8 applies to this seam too).
            return opinion
        self.annotations += 1
        return annotated
