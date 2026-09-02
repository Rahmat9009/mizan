# MIZAN — THE RISK CANON

(Received from Faisal 2026-09-02 mid-Sprint-1 as a companion to MIZAN-MASTER-PLAN-v2.md. Stored verbatim in substance; the
Orchestrator's implementation decisions are in docs/API-SURFACE-ADDENDUM-1.md and docs/adr/0006-*.md.)

## Summary of binding content

- §1 Twelve gaps; G1 (no path dependence) and G2 (no aggregate multi-agent exposure) are ARCHITECTURAL P0.
- §2 Ruin school: R-RUIN-1..4 (absorbing barrier, ES not VaR, stress scenarios first-class, missing tail data blocks),
  R-ERG-1..3 (time-average sizing, running path state, size scales DOWN with drawdown), R-KELLY-1..3 (fractional Kelly cap,
  confidence haircut, per-agent calibration), R-REGIME-1..3.
- §3 Multi-agent: R-AGG-1..6 (three-level limits, correlated-intent detection, model-provider concentration, signal-source
  concentration, crowding metric, simultaneous-exit risk).
- §4 Trader school: R-TRADE-1..7 (enforced invalidation + reward:risk, max risk per trade, no exit → REJECT, progressive
  exposure, per-agent expectancy, distance-to-limit, margin of safety).
- §5 Portfolio: R-PORT-1..7. §6 Institutional: R-INST-1..8 (three-stage compliance, risk radar, scenario library, factor
  limits, ES, liquidity horizons). §7 Liquidity: R-LIQ-1..5. §8 Model risk: R-MODEL-1..5. §9 Blow-ups: R-BLOW-1..8.
- §10 Options: R-OPT-1..8 (short gamma/vega asymmetric limits, undefined risk requires approval, multi-leg validation,
  assignment, pin risk, DTE, vol-surface stress, margin under stress).
- §11 Graduated response ladder L0..L5 replaces the binary kill switch; R-GRAD-1..3 (one-directional automatic escalation,
  every level change is a hash-chained record, Level 5 reachable independently of the policy engine).
- §12 Time controls R-TIME-1..7. §13 Consolidated DSL additions (tail, path, factor, liquidity, aggregate, options, time,
  response_ladder). §14 Build changes: path state + aggregate layer are P0 "decide now".
- §16 "Keep the invariants sacred. Add the risk depth around them."

Illustrative defaults in §13 are governance-framework defaults, not portfolio recommendations. Not investment advice.
