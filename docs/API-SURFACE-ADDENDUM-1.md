# API-SURFACE Addendum 1 — Risk Canon P0/P1 additions (binding, pre-freeze)

**Applies to:** `docs/API-SURFACE.md`. Everything here is ADDITIVE and OPTIONAL-BY-DEFAULT so that every example, fixture
and test written against the base spec stays valid. Sources: `docs/MIZAN-RISK-CANON.md` §2–§4, §7, §10–§13;
`docs/MIZAN-KILLER-FEATURE-VERDICT.md` §4–§6. Decision record: `docs/adr/0006-pure-engine-state-in-context.md`.

## A. Architectural principle (ADR-0006)

`mizan.risk.evaluate(proposal, context, policy)` remains a **pure function with no hidden state**. Path-dependence,
aggregate multi-agent exposure, agent budgets/calibration, the graduated-response level and the calendar are **inputs on
`RiskContext`**, assembled by the context provider (L3 `BrokerContextProvider` + ledger reads) and captured verbatim in the
`DecisionRecord`. Replay therefore reproduces them exactly (A1). Consequences:

- E2 extended (R-RUIN-4): if a policy enables a check whose required state/data is `None`, the check fails **blocking**
  with the matching `*_MISSING` code. Never skipped, never zero.
- A policy that enables a check the running engine does not implement is refused at load time:
  `PolicyError` / `POLICY_INVALID` with `CHECK_NOT_IMPLEMENTED`. `mizan.risk.IMPLEMENTED_CHECKS: frozenset[str]` is the
  engine's declaration; `mizan.policy.validate_policy(payload, *, implemented=IMPLEMENTED_CHECKS)`.
- Authorization is **state-bound** (Verdict §4): `ExecutionAuthorization.bound_state` records the state hashes the
  decision was made under; the execution gate's TOCTOU step re-evaluates against fresh state and additionally blocks if the
  response level has escalated.

## B. Contract additions

### B.1 `TradeProposal`
```
confidence: RatioStr | None = None            # agent-supplied estimate; policy haircuts it; never authority
signal_sources: list[str] = []                # ≤16 entries, e.g. "vendor:polygon", "news:reuters", "model:featherless/qwen3"
invalidation: Invalidation | None = None      # Invalidation{level: PositiveDecimalStr, direction: Literal["below","above"], target: PositiveDecimalStr | None}
```
`proposal_id_for` still excludes only `proposal_id` and `reasoning`; the new fields ARE hashed.

### B.2 `RiskContext` (all new fields default to `None`/`0`)
```
class PathState:      as_of: Rfc3339; peak_equity: PositiveDecimalStr; current_drawdown_pct: RatioStr; consecutive_losses: int(>=0);
                      days_under_water: int(>=0); daily_pnl_pct: DecimalStr | None; realized_expectancy: DecimalStr | None; sample_size: int(>=0)
class PendingIntent:  agent_id: AgentId; symbol: Symbol; direction: Literal["long","short"]; notional: DecimalStr; proposed_at: Rfc3339; model_provider: str | None
class AggregateState: as_of: Rfc3339; gross_exposure: NonNegativeDecimalStr; net_exposure: DecimalStr; exposure_pct_of_equity: RatioStr;
                      exposure_by_agent: dict[AgentId, DecimalStr]; exposure_by_model_provider: dict[str, DecimalStr];
                      exposure_by_signal_source: dict[str, DecimalStr]; exposure_by_sector: dict[str, DecimalStr];
                      pending_intents: list[PendingIntent]; crowding_score: RatioStr | None; days_to_liquidate_book: DecimalStr | None
class Calibration:    claimed_confidence_mean: RatioStr; realized_hit_rate: RatioStr; sample_size: int(>=0); expectancy: DecimalStr | None
class AgentState:     as_of: Rfc3339; daily_notional_used: NonNegativeDecimalStr; daily_order_count: int(>=0); open_positions: int(>=0); calibration: Calibration | None
class CalendarState:  session: Literal["pre","open","close","after","closed"]; minutes_since_open: int | None; minutes_to_close: int | None;
                      earnings_within_days: dict[Symbol, int]; macro_event_within_minutes: int | None; is_holiday_or_half_day: bool
RiskContext += path_state: PathState | None; aggregate_state: AggregateState | None; agent_state: AgentState | None;
               response_level: int (0..5) = 0; calendar: CalendarState | None
Quote        += adv: NonNegativeDecimalStr | None; spread_pct: RatioStr | None
OptionQuote  += open_interest: int | None; spread_pct: RatioStr | None; iv: NonNegativeDecimalStr | None
PortfolioSnapshot += gross_exposure: NonNegativeDecimalStr | None; net_exposure: DecimalStr | None; margin_requirement: NonNegativeDecimalStr | None;
                     maintenance_excess: DecimalStr | None; factor_exposures: dict[str, DecimalStr] | None
PortfolioGreeks   += short_gamma, long_gamma, short_vega, long_vega: DecimalStr | None
```

### B.3 `Policy` (new OPTIONAL sections; `None` means "checks in that section are disabled")
```
trade: TradeLimits | None            {max_risk_per_trade_pct: RatioStr|None, min_reward_risk: DecimalStr|None, require_invalidation: bool=False,
                                      confidence_haircut: RatioStr="0", kelly_fraction_cap: RatioStr|None (≤ "0.5")}
path: PathPolicy | None              {size_scaling_by_drawdown: list[{drawdown_pct: RatioStr, size_multiplier: RatioStr}] (drawdown ascending, multiplier non-increasing),
                                      max_consecutive_losses_before_review: int|None, max_days_under_water: int|None}
aggregate: AggregatePolicy | None    {max_portfolio_exposure_pct: RatioStr, max_correlated_intent_agents: int, correlated_intent_window_seconds: int,
                                      max_exposure_per_model_provider_pct: RatioStr|None, max_exposure_per_signal_source_pct: RatioStr|None,
                                      crowding_score_threshold: RatioStr|None, max_days_to_liquidate_book: DecimalStr|None}
agent_budgets: dict[AgentId, AgentBudget] = {}   AgentBudget{max_daily_notional: PositiveDecimalStr|None, max_daily_orders: int|None, max_open_positions: int|None,
                                                             allowed_symbols: list[Symbol]|None, active_hours_utc: [HH:MM, HH:MM]|None}
response_ladder: ResponseLadder | None {levels: list[{level: int(1..5), trigger: {daily_loss_pct: RatioStr|None, drawdown_pct: RatioStr|None},
                                        size_multiplier: RatioStr, new_risk_allowed: bool}], de_escalation_requires_human: Literal[True]}
liquidity: LiquidityPolicy | None    {max_pct_of_adv: RatioStr, max_option_spread_pct: RatioStr|None, min_option_open_interest: int|None, max_estimated_impact_bps: DecimalStr|None}
time: TimePolicy | None              {earnings_blackout_days_before: int, earnings_blackout_days_after: int, macro_event_blackout_minutes: int,
                                      no_trade_first_minutes: int, no_trade_last_minutes: int, max_overnight_exposure_pct: RatioStr|None}
tail: TailPolicy | None              {absorbing_barrier_equity_floor: DecimalStr|None, stress_scenarios: list[str], max_loss_under_worst_scenario_pct: RatioStr|None,
                                      expected_shortfall_99_limit: DecimalStr|None}
factor: FactorPolicy | None          {model_version: str, max_beta: DecimalStr|None, max_sector_exposure_pct: RatioStr|None, min_effective_independent_bets: int|None,
                                      max_factor_exposure: dict[str, DecimalStr]}
OptionsLimits += max_short_gamma, max_long_gamma, max_short_vega, max_long_vega: DecimalStr|None;
                 undefined_risk_requires_approval: Literal[True] = True; assignment_risk_check: bool = True; pin_risk_buffer_pct: RatioStr|None
```
`CHECK_IDS` is extended (APPENDED, order preserved):
```
"response_level_gate", "agent_budget", "invalidation_defined", "reward_risk", "risk_per_trade",
"drawdown_size_scaling", "consecutive_loss_review", "aggregate_exposure", "correlated_intent",
"model_provider_concentration", "signal_source_concentration", "crowding", "liquidity_adv", "option_liquidity",
"time_blackout", "session_window", "options_short_gamma_limit", "options_short_vega_limit", "assignment_risk",
"pin_risk", "stress_scenarios", "absorbing_barrier", "factor_exposure", "book_liquidation_time"
```
`response_level_gate` joins `ALWAYS_ON_CHECKS` (it is a no-op at level 0, cannot be disabled).

### B.4 `ExecutionAuthorization`
```
class BoundState: policy_hash: Sha256Hex; portfolio_snapshot_id: str; portfolio_state_hash: Sha256Hex; market_snapshot_id: str;
                  response_level: int; path_state_hash: Sha256Hex | None; aggregate_state_hash: Sha256Hex | None
ExecutionAuthorization += bound_state: BoundState          # REQUIRED (issue() fills it from the RiskContext in the decision's record)
```
`issue(decision, proposal, policy, *, now, context: RiskContext)` — signature gains `context`. Hash of a snapshot =
`sha256_hex(canonical_json(snapshot))`.

### B.5 `ExecutionResult`
```
RevalidationReport += state_changed: bool = False; response_level_at_execution: int | None = None
```
Gate step 4 (TOCTOU) additionally: `fresh.response_level > auth.bound_state.response_level` → BLOCKED `REAUTHORIZATION_REQUIRED` + `RESPONSE_LEVEL_ESCALATED`.

### B.6 New contract: `control_event.schema.json` / `mizan.contracts.control_event.ControlEvent`
Graduated-response level changes and kill-switch flips are hash-chained records in the SAME per-tenant chain (R-GRAD-2).
```
class ControlEvent: schema_version; event_id: str (uuid7); sequence: int; tenant_id: TenantId
    event_type: Literal["response_level_changed","kill_switch_activated","kill_switch_deactivated","policy_activated"]
    from_level: int | None; to_level: int | None; actor: {type: Literal["system","human"], id: str}
    trigger_reason_codes: list[ReasonCode]; policy: PolicyRef | None; occurred_at: Rfc3339; recorded_at: Rfc3339
    audit_prev_hash: Sha256Hex; audit_hash: Sha256Hex
```
Validators: `response_level_changed` ⇒ both levels present; a DOWNWARD level change ⇒ `actor.type == "human"` (R-GRAD-1).
`TenantLedger.append_control_event(...)` is L2 Sprint 3 scope; the chain rule is unchanged (prev = last record of any type).

### B.7 Reason codes (add to `contracts/reason_codes.json`)
PATH_STATE_MISSING, AGGREGATE_STATE_MISSING, AGENT_STATE_MISSING, CALENDAR_MISSING, LIQUIDITY_DATA_MISSING,
RESPONSE_LEVEL_RESTRICTS_NEW_RISK, RESPONSE_LEVEL_HALT, RESPONSE_LEVEL_ESCALATED, SIZE_SCALED_BY_DRAWDOWN,
CONSECUTIVE_LOSS_REVIEW, DAYS_UNDER_WATER_EXCEEDED, AGENT_DAILY_NOTIONAL_EXCEEDED, AGENT_DAILY_ORDERS_EXCEEDED,
AGENT_OPEN_POSITIONS_EXCEEDED, AGENT_SYMBOL_NOT_ALLOWED, AGENT_OUTSIDE_ACTIVE_HOURS, INVALIDATION_MISSING,
REWARD_RISK_BELOW_MINIMUM, RISK_PER_TRADE_EXCEEDED, CONFIDENCE_HAIRCUT_APPLIED, AGGREGATE_EXPOSURE_EXCEEDED,
CORRELATED_INTENT_DETECTED, MODEL_PROVIDER_CONCENTRATION_EXCEEDED, SIGNAL_SOURCE_CONCENTRATION_EXCEEDED,
CROWDING_THRESHOLD_EXCEEDED, ADV_PARTICIPATION_EXCEEDED, OPTION_SPREAD_TOO_WIDE, OPTION_OPEN_INTEREST_TOO_LOW,
ESTIMATED_IMPACT_EXCEEDED, EARNINGS_BLACKOUT, MACRO_EVENT_BLACKOUT, SESSION_WINDOW_RESTRICTED,
OVERNIGHT_EXPOSURE_EXCEEDED, OPTIONS_SHORT_GAMMA_LIMIT_EXCEEDED, OPTIONS_SHORT_VEGA_LIMIT_EXCEEDED,
UNDEFINED_RISK_REQUIRES_APPROVAL, ASSIGNMENT_RISK, PIN_RISK, STRESS_LOSS_EXCEEDED, ABSORBING_BARRIER_BREACHED,
EXPECTED_SHORTFALL_EXCEEDED, FACTOR_EXPOSURE_EXCEEDED, BOOK_LIQUIDATION_TIME_EXCEEDED, CHECK_NOT_IMPLEMENTED,
STATE_BINDING_MISMATCH. Categories: add PATH, AGGREGATE, AGENT, LIQUIDITY, TIME, TAIL, FACTOR, CONTROL.

## C. Sprint-2 implementation scope for L1 (`mizan.risk.IMPLEMENTED_CHECKS`)
Base 19 checks plus: response_level_gate, agent_budget, invalidation_defined, reward_risk, risk_per_trade,
drawdown_size_scaling, consecutive_loss_review, aggregate_exposure, correlated_intent, model_provider_concentration,
signal_source_concentration, liquidity_adv, option_liquidity, time_blackout, session_window,
options_short_gamma_limit, options_short_vega_limit. Sprint 3+: crowding, assignment_risk, pin_risk, stress_scenarios,
absorbing_barrier, factor_exposure, book_liquidation_time (policies enabling them are refused until then).

Reduction semantics for multipliers (drawdown scaling, response ladder): `authorized = floor(original × multiplier)`; the
binding cap is the MINIMUM across all reducing checks; a multiplier of `"0"` or `new_risk_allowed=False` on a
risk-increasing proposal (`intent == "open"` or "adjust" that raises gross exposure) is a REJECT; `intent == "close"`
proposals are exempt from response levels 1–3 (reducing risk is always allowed), never from level 4–5.

## D. Invariant 18 (added to `tests/invariants/`, READ-ONLY after S1)
`test_18_semantic_layer_disabled_produces_identical_verdict.py::test_semantic_layer_disabled_produces_identical_verdict`
Proves (Verdict §5): (a) `RiskEvaluation` is byte-identical whether or not an advisory/semantic opinion exists (it is not an
input); (b) for the same evaluation, `govern(..., advisory=None)` never authorizes LESS than `govern(..., advisory=X)`
for any valid X — i.e. the semantic layer is downward-only, and disabling it never makes a decision stricter or looser in
the deterministic part; (c) `replay(record, advisory=None)` reproduces the recorded `RiskEvaluation.evaluation_id` exactly
and reproduces the recorded governor verdict whenever the recorded advisory was CONCUR or unavailable.
