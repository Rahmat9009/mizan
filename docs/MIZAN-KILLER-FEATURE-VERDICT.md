# MIZAN — KILLER FEATURE VERDICT (received 2026-09-02; TRUNCATED in transmission after §7 "The demo, revised")

## Binding content as received

- §1 Verdict: layered, not either/or. CONTROL LAYER (continuous state-bound authorization) on top of PROOF LAYER
  (deterministic replay + hash-chained ledger). Build replay first; Doc C's features (policy compiler, agent certification,
  blast radius, counterfactual lab) all run on the replay engine.
- §2 Unverified competitor claims (Vorda, TradeOS) must not drive strategy. Rule: no competitor claim enters a planning doc
  or deck without a working link. Verified adjacent: SecProve Agent Firewall (retail), Straiker, AgentGuard/agent47,
  retail "trade replay" tools (naming collision → use "decision replay" / "governance replay", never bare "replay").
  Academic support for E1: an LLM safety filter is itself a 15c3-5 control defect; the governor must be deterministic.
- §3 FINRA 2026 report maps directly: Scope & Authority → continuous state-bound authorization; Auditability → decision
  record + deterministic replay; Data Sensitivity → redaction + tenant isolation; Autonomy → approval queue + autonomy budgets.
- §4 Scoring: P0 = deterministic decision replay, hash-chained ledger, full model provenance, continuous state-bound
  authorization, deterministic governor, aggregate multi-agent exposure, path-dependence state. P1 = policy compiler,
  agent certification, autonomy budgets, blast radius, factor limits, graduated response ladder, approval queue.
  Kill switch / prompt logging / basic limits = commodity: build, never pitch.
- §5 Intent-vs-action drift: ADVISORY-ONLY and ADVISORY-DOWNWARD. New invariant:
  `assert semantic_layer_disabled_produces_identical_verdict()`. Express intent/action matching deterministically where
  possible (direction, underlying, delta sign); LLM only for ambiguous cases, always advisory.
- §6 Five primitives: IDENTITY → INTENT → AUTHORITY → ACTION → EVIDENCE. Product sentence: "Every AI action receives the
  minimum authority it needs, for the shortest time it needs, under the exact policy and state that justified it — and
  Mizan can replay, byte for byte, exactly why." Build sequence: Tier 0 contracts/CI/invariants; Tier 1 proof layer
  (record, ledger, replay, path state); Tier 2 control layer (state-bound authorization, identity + autonomy budgets,
  aggregate exposure, response ladder); Tier 3 distribution (adapters, SDK, MCP); Tier 4 intelligence; Tier 5 frontier.
- §7 onward: NOT RECEIVED. Faisal: please add the full file to docs/ (see ledger/escalations.md).
