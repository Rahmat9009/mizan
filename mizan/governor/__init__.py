"""L2 — arbitration between the deterministic evaluation and the optional advisory opinion.

The governor is where Hard Rule E1 becomes code: the advisory layer can only make the outcome equally or
more conservative. It cannot approve, it cannot upsize, and it cannot overturn a deterministic rejection.
The contract types already make "more" unrepresentable; this module makes sure nothing in the arithmetic
reintroduces it.

The governor never reads free text. Its inputs are a verdict, a quantity and a set of codes.

Arbitration table (deterministic verdict x advisory recommendation -> authorized quantity)::

    evaluation      advisory                  outcome
    --------------  ------------------------  ------------------------------------------------------
    REJECT          anything, or nothing      REJECT, authorized "0", no legs, + HARD_REJECTION_UPHELD
    PASS / REDUCE   none / unavailable        fail_closed.on_advisory_unavailable ? REJECT (+
                                              ADVISORY_UNAVAILABLE) : the deterministic cap
    PASS / REDUCE   REJECT                    REJECT, authorized "0", no legs, + ADVISORY_REJECT
    PASS / REDUCE   REDUCE below the cap      the advised quantity, + ADVISORY_REDUCE
    PASS / REDUCE   REDUCE equal to the cap   the cap (the advisory cut nothing)
    PASS / REDUCE   REDUCE above the cap      the cap, + ADVISORY_CLAMPED
    PASS / REDUCE   CONCUR                    the cap

In every row ``authorized.total_quantity <= evaluation.recommended_quantity``. The authorized total is
then apportioned across the proposal's legs in whole units at the proposal's own leg ratios; a structure
that cannot be preserved at that size is a REJECT (STRUCTURE_INVALID), never a broken spread — a spread
that loses or unbalances a leg is a naked short (Risk Canon R-OPT-3).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from mizan.contracts import (
    DECIMAL_CONTEXT,
    ENGINE_VERSION,
    AdvisoryOpinion,
    Authorized,
    AuthorizedLegQuantity,
    GovernorDecision,
    Policy,
    Quantities,
    ReasonCode,
    Reduction,
    ReductionSource,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
    dec,
    dstr,
    sorted_reason_codes,
    uuid7,
)

__all__ = ["govern"]

_ZERO = Decimal(0)
_ONE = Decimal(1)

# What the advisory asked for, reduced to the only four things the governor can act on.
_UNAVAILABLE = "unavailable"
_CONCUR = "concur"
_REDUCE = "reduce"
_REJECT = "reject"


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
    original = dec(evaluation.original_quantity)
    cap = dec(evaluation.recommended_quantity)
    codes: set[ReasonCode] = set(evaluation.reason_codes)
    reductions: list[Reduction] = []
    advisory = _recordable(advisory)

    incoherence = _incoherence(proposal, evaluation, policy, context)
    if incoherence is not None:
        return _rejected(proposal, evaluation, policy, context, advisory, codes, "deterministic", incoherence)

    if evaluation.verdict == "REJECT":
        return _rejected(
            proposal,
            evaluation,
            policy,
            context,
            advisory,
            codes,
            "deterministic",
            ReasonCode.HARD_REJECTION_UPHELD,
        )

    intent, advised = _advisory_intent(advisory)

    if intent == _REJECT:
        return _rejected(
            proposal, evaluation, policy, context, advisory, codes, "advisory", ReasonCode.ADVISORY_REJECT
        )
    if intent == _UNAVAILABLE and policy.advisory.enabled and policy.fail_closed.on_advisory_unavailable:
        return _rejected(
            proposal,
            evaluation,
            policy,
            context,
            advisory,
            codes,
            "deterministic",
            ReasonCode.ADVISORY_UNAVAILABLE,
        )

    if cap < original:
        reductions.append(
            Reduction(
                source="deterministic",
                from_quantity=dstr(original),
                to_quantity=dstr(cap),
                reason_code=_deterministic_code(evaluation, cap),
            )
        )

    target = cap
    if intent == _REDUCE and advised is not None:
        if advised < cap:
            target = advised
            codes.add(ReasonCode.ADVISORY_REDUCE)
            reductions.append(
                Reduction(
                    source="advisory",
                    from_quantity=dstr(cap),
                    to_quantity=dstr(advised),
                    reason_code=ReasonCode.ADVISORY_REDUCE,
                )
            )
        elif advised > cap:
            # The advisory asked for more than the deterministic engine allows. It does not get it.
            codes.add(ReasonCode.ADVISORY_CLAMPED)

    legs = _apportion(proposal, target)
    if legs is None:
        return _rejected(
            proposal,
            evaluation,
            policy,
            context,
            advisory,
            codes,
            "deterministic",
            ReasonCode.STRUCTURE_INVALID,
        )

    authorized_total = _sum(dec(leg.quantity) for leg in legs)
    if authorized_total > cap:  # unreachable by construction; fail closed rather than trust the arithmetic
        return _rejected(
            proposal,
            evaluation,
            policy,
            context,
            advisory,
            codes,
            "deterministic",
            ReasonCode.ENGINE_DEGRADED,
        )
    if authorized_total < target:
        # Whole-unit apportionment at the proposal's leg ratios: the remainder is not authorized.
        codes.add(ReasonCode.SIZE_REDUCED_TO_POLICY_CAP)
        reductions.append(
            Reduction(
                source="deterministic",
                from_quantity=dstr(target),
                to_quantity=dstr(authorized_total),
                reason_code=ReasonCode.SIZE_REDUCED_TO_POLICY_CAP,
            )
        )

    verdict = "APPROVE" if authorized_total == original else "REDUCE"
    return _build(
        proposal,
        evaluation,
        policy,
        context,
        advisory,
        verdict=verdict,
        codes=codes,
        authorized=Authorized(
            total_quantity=dstr(authorized_total),
            total_notional=_authorized_notional(evaluation, original, cap, authorized_total),
            legs=legs,
            reductions=reductions,
        ),
    )


# ----------------------------------------------------------------------------------------------------------
# Advisory intent
# ----------------------------------------------------------------------------------------------------------


def _recordable(advisory: object) -> AdvisoryOpinion | None:
    """Reduce whatever was passed to something the decision record can actually carry.

    The governor is handed an opinion by a caller it does not control. An object that is not an
    ``AdvisoryOpinion``, or one that was forced past the contract's validators, becomes an *unavailable*
    opinion here rather than an exception three layers down at decision-build time: an unreadable advisory
    must degrade the same way an absent one does.
    """
    if advisory is None:
        return None
    if not isinstance(advisory, AdvisoryOpinion):
        return _unreadable("ADVISORY_INVALID_OUTPUT: not an advisory opinion")
    try:
        return AdvisoryOpinion.model_validate(advisory.model_dump(mode="json"))
    except Exception:
        return _unreadable("ADVISORY_INVALID_OUTPUT: the opinion is not a valid advisory opinion")


def _unreadable(detail: str) -> AdvisoryOpinion:
    return AdvisoryOpinion(
        profile="unreadable",
        invoked=True,
        available=False,
        recommendation=None,
        recommended_quantity=None,
        reasoning=detail,
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def _advisory_intent(advisory: AdvisoryOpinion | None) -> tuple[str, Decimal | None]:
    """Reduce an opinion to ``(intent, quantity)``. Malformed input is *unavailable*, never authority.

    An opinion that cannot be understood must not become more powerful than one that can, so the only
    ways out of this function are "the deterministic verdict stands", "reduce to this quantity" and
    "reject". There is no fourth branch, and no value of any field can create one.
    """
    if advisory is None or not advisory.available or advisory.recommendation is None:
        return (_UNAVAILABLE, None)
    recommendation = advisory.recommendation
    if recommendation == "REJECT":
        return (_REJECT, None)
    if recommendation == "CONCUR":
        return (_CONCUR, None)
    quantity = advisory.recommended_quantity
    if quantity is None:
        return (_UNAVAILABLE, None)
    try:
        parsed = dec(quantity)
    except (TypeError, ValueError):
        return (_UNAVAILABLE, None)
    if parsed <= _ZERO:
        return (_REJECT, None)  # "reduce to nothing" is a rejection, and rejections are always honoured
    return (_REDUCE, parsed)


# ----------------------------------------------------------------------------------------------------------
# Apportionment
# ----------------------------------------------------------------------------------------------------------


def _apportion(proposal: TradeProposal, target: Decimal) -> list[AuthorizedLegQuantity] | None:
    """Split ``target`` across the proposal's legs at the proposal's own ratios, in whole units.

    A multi-leg structure is authorized in whole multiples of its ratio block, so a 1:1 spread is never
    authorized 2:1. ``None`` means the structure cannot be preserved at this size; the caller turns that
    into a REJECT rather than authorizing a broken structure.
    """
    if target <= _ZERO:
        return None
    quantities = [dec(leg.quantity) for leg in proposal.legs]
    indices = [leg.leg_index for leg in proposal.legs]

    if len(quantities) == 1:
        single = _floor(target) if _is_whole(quantities[0]) else target
        if single <= _ZERO:
            return None
        return [AuthorizedLegQuantity(leg_index=indices[0], quantity=dstr(single))]

    if not all(_is_whole(quantity) for quantity in quantities):
        return None
    units = [int(quantity) for quantity in quantities]
    divisor = 0
    for unit in units:
        divisor = _gcd(divisor, unit)
    if divisor <= 0:
        return None
    ratio = [unit // divisor for unit in units]
    block = sum(ratio)
    multiple = int(_floor_divide(target, Decimal(block)))
    if multiple <= 0:
        return None
    return [
        AuthorizedLegQuantity(leg_index=index, quantity=dstr(Decimal(multiple * share)))
        for index, share in zip(indices, ratio, strict=True)
    ]


def _gcd(left: int, right: int) -> int:
    """Euclid, on integers only (``math`` is not importable in the decision path — Hard Rule A6)."""
    while right:
        left, right = right, left % right
    return abs(left)


def _is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value()


def _floor(value: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.divide_int(value, _ONE)


def _floor_divide(value: Decimal, divisor: Decimal) -> Decimal:
    return DECIMAL_CONTEXT.divide_int(value, divisor)


def _sum(values: Iterable[Decimal]) -> Decimal:
    total = _ZERO
    for value in values:
        total = DECIMAL_CONTEXT.add(total, value)
    return total


# ----------------------------------------------------------------------------------------------------------
# Supporting detail
# ----------------------------------------------------------------------------------------------------------


def _incoherence(
    proposal: TradeProposal, evaluation: RiskEvaluation, policy: Policy, context: RiskContext
) -> ReasonCode | None:
    """Identity checks that must hold before any arbitration is meaningful. Fail closed when they do not."""
    if evaluation.proposal_id != proposal.proposal_id:
        return ReasonCode.SCHEMA_INVALID
    if evaluation.tenant_id != policy.tenant_id or evaluation.tenant_id != context.tenant_id:
        return ReasonCode.TENANT_MISMATCH
    return None


def _deterministic_code(evaluation: RiskEvaluation, cap: Decimal) -> ReasonCode:
    """The code of the check that bound the deterministic size, so the console can name the cause."""
    for check in evaluation.checks:
        if check.passed or check.reason_code is None or check.recommended_quantity is None:
            continue
        if dec(check.recommended_quantity) == cap:
            return check.reason_code
    for check in evaluation.checks:
        if not check.passed and check.reason_code is not None:
            return check.reason_code
    if evaluation.reason_codes:
        return evaluation.reason_codes[0]
    return ReasonCode.SIZE_REDUCED_TO_POLICY_CAP


def _authorized_notional(
    evaluation: RiskEvaluation, original: Decimal, cap: Decimal, authorized: Decimal
) -> str | None:
    """Notional of the authorized size: the engine's own figures where they apply, pro-rated otherwise."""
    if authorized == original:
        return evaluation.original_notional
    if authorized == cap:
        return evaluation.recommended_notional
    if evaluation.original_notional is None:
        return None
    scaled = DECIMAL_CONTEXT.divide(
        DECIMAL_CONTEXT.multiply(dec(evaluation.original_notional), authorized), original
    )
    return dstr(scaled)


def _rejected(
    proposal: TradeProposal,
    evaluation: RiskEvaluation,
    policy: Policy,
    context: RiskContext,
    advisory: AdvisoryOpinion | None,
    codes: set[ReasonCode],
    source: ReductionSource,
    reason_code: ReasonCode,
) -> GovernorDecision:
    """A REJECT authorizes nothing: total "0", no legs, and one reduction naming who refused and why."""
    original = dec(evaluation.original_quantity)
    return _build(
        proposal,
        evaluation,
        policy,
        context,
        advisory,
        verdict="REJECT",
        codes=codes | {reason_code},
        authorized=Authorized(
            total_quantity="0",
            total_notional="0",
            legs=[],
            reductions=[
                Reduction(
                    source=source,
                    from_quantity=dstr(original),
                    to_quantity="0",
                    reason_code=reason_code,
                )
            ],
        ),
    )


def _build(
    proposal: TradeProposal,
    evaluation: RiskEvaluation,
    policy: Policy,
    context: RiskContext,
    advisory: AdvisoryOpinion | None,
    *,
    verdict: str,
    codes: set[ReasonCode],
    authorized: Authorized,
) -> GovernorDecision:
    return GovernorDecision.build(
        decision_id=uuid7(),
        proposal_id=evaluation.proposal_id,
        evaluation_id=evaluation.evaluation_id,
        tenant_id=evaluation.tenant_id,
        agent_id=proposal.agent.agent_id,
        policy=policy.ref,
        engine_version=ENGINE_VERSION,
        # Stamped with the time the state was evaluated, never a wall clock: replay must reproduce this
        # byte for byte (Hard Rule A1).
        decision_timestamp=context.evaluated_at,
        verdict=verdict,
        reason_codes=sorted_reason_codes(codes),
        original=Quantities(
            total_quantity=evaluation.original_quantity, total_notional=evaluation.original_notional
        ),
        authorized=authorized,
        llm_advisory=advisory,
    )
