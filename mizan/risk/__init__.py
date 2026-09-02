"""L1 — the deterministic risk engine.

``evaluate`` is a pure function of (proposal, context, policy). No clock, no network, no LLM, no hidden
state: everything it needs — including path-dependence, aggregate multi-agent exposure, agent budgets,
the response level and the calendar — arrives on the RiskContext (ADR-0006). That is what makes Hard
Rule A1 (same inputs, same policy version, same engine version, same verdict) provable by replay.

Hard Rules: A1, A6 (decimal only), E2 (unknown risk is not safe), E8 (runs with the LLM offline).
"""

from __future__ import annotations

from mizan.contracts import CHECK_IDS, Policy, RiskContext, RiskEvaluation, TradeProposal

__all__ = ["IMPLEMENTED_CHECKS", "evaluate"]

#: Checks this engine build actually implements. A policy that enables anything outside this set is
#: refused at load time (CHECK_NOT_IMPLEMENTED) rather than silently skipped — an unimplemented check
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
    }
)

#: Deferred to Sprint 3+. Enumerated so the gap is visible rather than implied.
DEFERRED_CHECKS: frozenset[str] = frozenset(CHECK_IDS) - IMPLEMENTED_CHECKS


def evaluate(proposal: TradeProposal, context: RiskContext, policy: Policy) -> RiskEvaluation:
    """Evaluate one proposal against one policy under one context. Pure and total.

    Returns a RiskEvaluation with a verdict of PASS, REDUCE or REJECT, a CheckResult for every id in
    CHECK_IDS, and reason codes for anything that did not pass. Missing risk-critical data is a
    blocking REJECT, never a zero and never a skip.
    """
    raise NotImplementedError("L1 implements this in Sprint 2")
