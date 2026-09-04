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

import threading
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from mizan.advisory.offline import OfflineAdvisoryProvider
from mizan.advisory.openai_compatible import OpenAICompatibleAdvisoryProvider
from mizan.contracts import (
    AdvisoryOpinion,
    Policy,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
    dec,
    dstr,
)
from mizan.contracts.trade_proposal import MAX_REASONING_CHARS
from mizan.contracts.types import NonEmptyStr

__all__ = [
    "AdvisoryProvider",
    "OfflineAdvisoryProvider",
    "OpenAICompatibleAdvisoryProvider",
    "get_advisory",
]

_ZERO = Decimal(0)
_MAX_PROFILE_CHARS = 256
_UNAVAILABLE_PROFILE = "unavailable"


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
    profile = _UNAVAILABLE_PROFILE
    try:
        profile = _profile_of(provider, policy)
        if provider is None:
            return _unavailable(profile, invoked=False, detail="no advisory provider is configured")
        if timeout_seconds <= 0:
            return _unavailable(profile, invoked=False, detail="ADVISORY_SKIPPED: no time budget")
        result, failure = _call(provider, proposal, evaluation, context, policy, timeout_seconds)
        if failure is not None:
            return _unavailable(profile, invoked=True, detail=failure)
        try:
            opinion = _as_opinion(result)
        except Exception as exc:
            return _unavailable(profile, invoked=True, detail=_detail("ADVISORY_INVALID_OUTPUT", exc))
        return _clamped(opinion, dec(evaluation.recommended_quantity), profile)
    except Exception as exc:  # last line of defence: this function does not raise, ever
        return _unavailable(profile, invoked=True, detail=_detail("ADVISORY_INVALID_OUTPUT", exc))


# ----------------------------------------------------------------------------------------------------------
# Calling an untrusted provider
# ----------------------------------------------------------------------------------------------------------


def _call(
    provider: AdvisoryProvider,
    proposal: TradeProposal,
    evaluation: RiskEvaluation,
    context: RiskContext,
    policy: Policy,
    timeout_seconds: int,
) -> tuple[Any, str | None]:
    """Run ``provider.advise`` under a hard deadline. Returns ``(result, failure_detail)``.

    The call runs on a daemon worker thread and the caller waits at most ``timeout_seconds`` for it. A
    provider blocked in a socket read cannot be cancelled from outside in CPython — there is no safe way
    to interrupt an arbitrary third-party call — so the deadline is enforced by *abandoning* the worker
    rather than by stopping it: the thread is a daemon (it cannot hold the process open), whatever it
    eventually returns is discarded, and the governance decision proceeds without it. That is the whole
    point of Hard Rule E8: a hung LLM must cost the risk system nothing but the wait.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = provider.advise(proposal, evaluation, context, policy)
        except BaseException as exc:  # a worker that dies silently would look like a timeout
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="mizan-advisory", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        return (None, f"ADVISORY_UNAVAILABLE: provider exceeded {timeout_seconds}s")
    error = outcome.get("error")
    if error is not None:
        return (None, _detail("ADVISORY_UNAVAILABLE", error))
    return (outcome.get("result"), None)


def _as_opinion(result: Any) -> AdvisoryOpinion:
    """Accept only what the contract can express: an ``AdvisoryOpinion`` or a mapping that validates as one.

    A duck-typed object with the right attribute names is *not* accepted. That is deliberate: the only
    thing standing between a provider's imagination and the governor is this type, and an object that
    merely resembles it (``recommendation = "APPROVE_MORE"``) resembles it precisely where it matters.
    """
    if isinstance(result, AdvisoryOpinion):
        return result
    if isinstance(result, dict):
        return AdvisoryOpinion.model_validate(result)
    raise TypeError(f"advisory provider returned {type(result).__name__}, not an AdvisoryOpinion")


def _clamped(opinion: AdvisoryOpinion, cap: Decimal, profile: str) -> AdvisoryOpinion:
    """Normalise a provider opinion so that nothing downstream has to trust it.

    ``invoked`` is set from what actually happened rather than from what the provider claimed, and a
    recommended quantity above the deterministic cap is clamped to the cap here — the governor clamps
    again, because one layer of defence is not a defence.
    """
    if not opinion.available or opinion.recommendation is None:
        return _unavailable(profile, invoked=True, detail=_text(opinion.reasoning))

    recommendation = opinion.recommendation
    quantity: str | None = None
    reasoning = _text(opinion.reasoning)
    if recommendation == "REDUCE":
        raw = opinion.recommended_quantity
        if raw is None:
            return _unavailable(
                profile, invoked=True, detail="ADVISORY_INVALID_OUTPUT: REDUCE without a quantity"
            )
        parsed = dec(raw)
        if parsed > cap:
            # The clamp is right and stays: an advisory can only ever reduce (E1). What was wrong is
            # that it happened SILENTLY - the opinion left here carrying exactly the cap, which is
            # indistinguishable in the record from a provider that simply concurred. A model quietly
            # asking for 99999 against a cap of 10 is the single most useful thing this layer could
            # tell an operator, and it was the one thing the record could not say (F-32).
            #
            # Recorded, never enforced: nothing downstream reads this text, and INV-17 walks the AST
            # of the enforcement path to keep it that way. The clamped number remains the only input
            # to any decision.
            reasoning = _text(
                f"ADVISORY_CLAMPED: provider asked for {_bounded(str(raw))} against a deterministic "
                f"cap of {dstr(cap)}; clamped to the cap. {reasoning}"
            )
            parsed = cap
        if parsed <= _ZERO:
            recommendation = "REJECT"  # a reduction to nothing is a rejection, and stays downward
        else:
            quantity = dstr(parsed)

    return AdvisoryOpinion(
        profile=_bounded(opinion.profile) or profile,
        invoked=True,
        available=True,
        recommendation=recommendation,
        recommended_quantity=quantity,
        reasoning=reasoning,
        authority_ceiling="reduce_or_reject",
        provider_ref=_bounded(opinion.provider_ref),
        raw_hash=opinion.raw_hash,
    )


# ----------------------------------------------------------------------------------------------------------
# Unavailable opinions
# ----------------------------------------------------------------------------------------------------------


def _unavailable(profile: str, *, invoked: bool, detail: str) -> AdvisoryOpinion:
    """The only failure mode this module has: an opinion that carries no recommendation at all."""
    return AdvisoryOpinion(
        profile=profile,
        invoked=invoked,
        available=False,
        recommendation=None,
        recommended_quantity=None,
        reasoning=_text(detail),
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def _detail(code: str, exc: BaseException) -> str:
    """Record the failure *class*, never the provider's own words.

    An exception message is provider-controlled text; copying it into an audit field would give an
    adversarial endpoint a channel into the decision record. The type name is enough to debug with.
    """
    return f"{code}: {type(exc).__name__}"


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[:MAX_REASONING_CHARS]


def _bounded(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()[:_MAX_PROFILE_CHARS]
    return trimmed or None


def _profile_of(provider: AdvisoryProvider | None, policy: Policy) -> str:
    """The provider's own profile label when it has a usable one, else the policy's configured profile."""
    try:
        declared = getattr(provider, "profile", None)
    except Exception:
        declared = None
    bounded = _bounded(declared)
    if bounded is not None:
        return bounded
    configured: NonEmptyStr = policy.advisory.profile
    return configured
