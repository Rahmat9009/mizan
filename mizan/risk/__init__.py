"""L1 -- the deterministic risk engine.

``evaluate`` is a pure function of (proposal, context, policy). No clock, no network, no LLM, no hidden
state: everything it needs -- including path-dependence, aggregate multi-agent exposure, agent budgets,
the response level and the calendar -- arrives on the RiskContext (ADR-0006). That is what makes Hard
Rule A1 (same inputs, same policy version, same engine version, same verdict) provable by replay.

Hard Rules: A1, A6 (decimal only), E2 (unknown risk is not safe), E8 (runs with the LLM offline).
"""

from __future__ import annotations

from decimal import Decimal

from mizan.contracts import (
    CHECK_IDS,
    ENGINE_VERSION,
    CheckResult,
    Policy,
    ReasonCode,
    RiskContext,
    RiskEvaluation,
    TradeProposal,
    dec,
    dstr,
    sorted_reason_codes,
)
from mizan.risk.checks import CHECK_FUNCTIONS, MISSING_DATA_CODES, CheckFunction
from mizan.risk.expected_value import expected_value
from mizan.risk.valuation import ZERO, divide, exposure_of, floor_units, multiply

__all__ = ["CHECK_REGISTRY", "DEFERRED_CHECKS", "IMPLEMENTED_CHECKS", "evaluate"]

#: Checks this engine build actually implements. A policy that enables anything outside this set is
#: refused at load time (CHECK_NOT_IMPLEMENTED) rather than silently skipped -- an unimplemented check
#: that quietly passes is exactly the failure mode Hard Rule E2 exists to prevent.
IMPLEMENTED_CHECKS: frozenset[str] = frozenset(
    {
        # base
        "market_data_presence",
        "portfolio_state_presence",
        "proposal_expiry",
        "restricted_symbol",
        "restricted_strategy",
        "leg_limit",
        "position_limit",
        "capital_threshold",
        "buying_power_sufficiency",
        "buying_power_utilization",
        "concentration_limit",
        "sector_concentration",
        "drawdown_limit",
        "duplicate_order",
        "erroneous_order",
        "days_to_expiry",
        "options_delta_limit",
        "options_gamma_limit",
        "options_vega_limit",
        # Risk Canon, Sprint 2 scope (Addendum 1 section C)
        "response_level_gate",
        "agent_budget",
        "invalidation_defined",
        "reward_risk",
        "risk_per_trade",
        "drawdown_size_scaling",
        "consecutive_loss_review",
        "aggregate_exposure",
        "correlated_intent",
        "model_provider_concentration",
        "signal_source_concentration",
        "liquidity_adv",
        "option_liquidity",
        "time_blackout",
        "session_window",
        "options_short_gamma_limit",
        "options_short_vega_limit",
        # REQ-35
        "account_capability",
        # F-31
        "structure_valid",
        # EV-GATE
        "expected_value",
    }
)

#: The dispatch table. ``expected_value`` lives in its own module rather than ``checks.py`` because it
#: carries a normal CDF and a square root that nothing else needs, so it is registered here instead of
#: being folded into a registry that has no other reason to know about it.
CHECK_REGISTRY: dict[str, CheckFunction] = {**CHECK_FUNCTIONS, "expected_value": expected_value}
assert set(CHECK_REGISTRY) == set(IMPLEMENTED_CHECKS), (
    "every implemented check must have exactly one function, or the engine would KeyError on a policy "
    "that enabled it: "
    f"{sorted(set(IMPLEMENTED_CHECKS) ^ set(CHECK_REGISTRY))}"
)

#: Deferred to Sprint 3+. Enumerated so the gap is visible rather than implied.
DEFERRED_CHECKS: frozenset[str] = frozenset(CHECK_IDS) - IMPLEMENTED_CHECKS

DISABLED_DETAIL = "disabled by policy"
DEFERRED_DETAIL = "not implemented by this engine build; a policy may not enable it"
UNEVALUATED_DETAIL = "not evaluated: the policy is not bound to this context"


def evaluate(proposal: TradeProposal, context: RiskContext, policy: Policy) -> RiskEvaluation:
    """Evaluate one proposal against one policy under one context. Pure and total.

    Returns a RiskEvaluation with a verdict of PASS, REDUCE or REJECT, a CheckResult for every id in
    CHECK_IDS, and reason codes for anything that did not pass. Missing risk-critical data is a
    blocking REJECT, never a zero and never a skip.
    """
    binding = _binding_failure(context, policy)
    if binding is not None:
        return _build(proposal, context, policy, _unevaluated_checks(), extra_codes=[binding], bound=False)
    checks = [_run(check_id, proposal, context, policy) for check_id in CHECK_IDS]
    return _build(proposal, context, policy, checks)


def _binding_failure(context: RiskContext, policy: Policy) -> ReasonCode | None:
    """A policy that is not this tenant's, or not the one the context was built against, decides nothing."""
    if policy.tenant_id != context.tenant_id:
        return ReasonCode.TENANT_MISMATCH
    if policy.policy_hash != context.policy.hash:
        return ReasonCode.POLICY_HASH_MISMATCH
    return None


def _unevaluated_checks() -> list[CheckResult]:
    return [
        CheckResult(
            check_id=check_id,
            passed=True,
            severity="info",
            reason_code=None,
            threshold=None,
            actual=None,
            data_source=None,
            snapshot_ts=None,
            recommended_quantity=None,
            detail=UNEVALUATED_DETAIL,
        )
        for check_id in CHECK_IDS
    ]


def _informational(check_id: str, detail: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=True,
        severity="info",
        reason_code=None,
        threshold=None,
        actual=None,
        data_source=None,
        snapshot_ts=None,
        recommended_quantity=None,
        detail=detail,
    )


def _run(check_id: str, proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult:
    if check_id not in IMPLEMENTED_CHECKS:
        return _informational(check_id, DEFERRED_DETAIL)
    if not policy.is_check_enabled(check_id):
        return _informational(check_id, DISABLED_DETAIL)
    function: CheckFunction = CHECK_REGISTRY[check_id]
    result = function(proposal, context, policy)
    if result is None:
        return CheckResult(
            check_id=check_id,
            passed=True,
            severity=policy.check_config(check_id).severity,
            reason_code=None,
            threshold=None,
            actual=None,
            data_source=None,
            snapshot_ts=None,
            recommended_quantity=None,
            detail="",
        )
    return result


def _build(
    proposal: TradeProposal,
    context: RiskContext,
    policy: Policy,
    checks: list[CheckResult],
    *,
    extra_codes: list[ReasonCode] | None = None,
    bound: bool = True,
) -> RiskEvaluation:
    original = proposal.total_quantity
    codes: list[ReasonCode] = list(extra_codes or [])
    caps: list[Decimal] = []
    blocked = False
    data_complete = bound
    for check in checks:
        if check.reason_code is not None:
            codes.append(check.reason_code)
            if check.reason_code in MISSING_DATA_CODES:
                data_complete = False
        if check.passed:
            continue
        if check.severity == "blocking":
            blocked = True
        elif check.recommended_quantity is not None:
            caps.append(dec(check.recommended_quantity))

    if blocked or not bound:
        recommended = ZERO
    else:
        recommended = original
        for cap in caps:
            if cap < recommended:
                recommended = cap
        if recommended != original:
            recommended = floor_units(recommended)
    verdict = "PASS" if recommended == original else ("REJECT" if recommended <= ZERO else "REDUCE")
    if verdict != "PASS" and not codes:
        # Unreachable through the checks above (a reduction always carries its code); kept because a
        # REDUCE or REJECT without a machine-readable reason would violate Hard Rule A4.
        codes.append(ReasonCode.SIZE_REDUCED_TO_POLICY_CAP)

    original_notional, recommended_notional = _notionals(proposal, context, original, recommended, verdict)
    return RiskEvaluation.build(
        proposal_id=proposal.proposal_id,
        context_id=context.context_id,
        tenant_id=context.tenant_id,
        policy=policy.ref,
        engine_version=ENGINE_VERSION,
        evaluated_at=context.evaluated_at,
        verdict=verdict,
        reason_codes=sorted_reason_codes(codes),
        checks=checks,
        original_quantity=dstr(original),
        recommended_quantity=dstr(recommended),
        original_notional=original_notional,
        recommended_notional=recommended_notional,
        data_complete=data_complete,
    )


def _notionals(
    proposal: TradeProposal,
    context: RiskContext,
    original: Decimal,
    recommended: Decimal,
    verdict: str,
) -> tuple[str | None, str | None]:
    """Both notionals are valued at the market, never at the agent's limit price (finding F-1)."""
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return None, None
    original_notional = exposure.gross
    if verdict == "PASS":
        return dstr(original_notional), dstr(original_notional)
    if recommended <= ZERO:
        return dstr(original_notional), "0"
    share = divide(recommended, original)
    scaled = original_notional if share is None else multiply(original_notional, share)
    return dstr(original_notional), dstr(scaled)
