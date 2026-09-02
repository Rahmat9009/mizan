# ADR-0006: The engine stays pure — path, aggregate, agent and calendar state are inputs on the context

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule A1 (byte-identical replay), E2 as extended by Risk Canon R-RUIN-4 (missing
  risk-critical data blocks), E8 (the engine runs with the LLM offline), Risk Canon §14 (path-dependence
  state and the aggregate multi-agent layer are P0 and architectural), R-GRAD-1..3 (graduated response),
  Killer Feature Verdict §4 (continuous state-bound authorization)
- **Related:** `docs/API-SURFACE-ADDENDUM-1.md` (the contract additions this decision made possible)

## Context

The Risk Canon arrived mid-Sprint-1 and named two gaps as **architectural P0**, not features: G1, no
path-dependence state (drawdown, consecutive losses, days under water, realised expectancy), and G2, no
aggregate multi-agent exposure (what all agents together are doing, by symbol, by model provider, by
signal source). Around them sit per-agent budgets and calibration, the graduated response ladder L0–L5,
and a trading calendar. §14 says: decide now.

Every one of those is *state that changes between decisions*. That is what makes them architectural. The
obvious way to add them is to give the risk engine somewhere to keep them — a `RiskEngine` object that
holds the running peak equity, tracks consecutive losses, accumulates exposure across agents, and knows
what response level it is currently at. Every framework in this space is built that way, and it is the
natural shape for the domain.

It is also incompatible with the one property this product actually sells. A1 says the same inputs, the
same policy version and the same engine version produce the same verdict and the same reason codes, and
the Killer Feature Verdict §4 scores deterministic decision replay as the top P0 item. A verdict that
depended on an engine object's accumulated internal state could only be replayed by reconstructing that
state — which means replaying every decision since process start, in order, hoping nothing else touched
the object. That is not replay; it is re-simulation, and it is not byte-identical.

So the question forced by the Risk Canon was: where does state live?

## Decision

**`mizan.risk.evaluate(proposal, context, policy)` remains a pure function with no hidden state.**

Path-dependence, aggregate exposure, agent budgets and calibration, the graduated-response level and the
calendar are **inputs on `RiskContext`**, assembled by the context provider (the L3
`BrokerContextProvider` plus ledger reads) and captured **verbatim** in the `DecisionRecord`. A replay
therefore sees exactly what the original decision saw, and reproduces it byte for byte.

Concretely, `RiskContext` gains `path_state`, `aggregate_state`, `agent_state`, `response_level` and
`calendar` (Addendum 1 §B.2), all defaulting to `None`/`0` so that every fixture written against the base
spec stays valid. Five further decisions follow from the same principle:

1. **Missing state for an enabled check is a blocking REJECT.** E2 extended per R-RUIN-4: if a policy
   enables a check whose required state is `None`, the check fails *blocking* with the matching
   `*_MISSING` reason code — `PATH_STATE_MISSING`, `AGGREGATE_STATE_MISSING`, `AGENT_STATE_MISSING`,
   `CALENDAR_MISSING`, `LIQUIDITY_DATA_MISSING`. Never skipped, never treated as zero. Purity makes this
   necessary as well as correct: a pure function has nowhere to fall back to, so the only honest answer to
   absent state is to refuse.

2. **A policy that enables a check the engine does not implement is refused at load time.**
   `mizan.risk.IMPLEMENTED_CHECKS` is the engine's declaration; `mizan.policy.validate_policy(payload, *,
   implemented=IMPLEMENTED_CHECKS)` raises `PolicyError` / `POLICY_INVALID` with `CHECK_NOT_IMPLEMENTED`.
   Without this, a customer could write a policy naming `crowding` or `factor_exposure`, get a silent
   no-op, and believe a limit was being enforced. Failing at load is the only point where the operator is
   present to be told.

3. **Authorization is state-bound.** `ExecutionAuthorization.bound_state` records the hashes of the state
   the decision was made under — policy, portfolio snapshot, market snapshot, response level and the path
   and aggregate state hashes. The gate's TOCTOU step (E9) re-evaluates against fresh state and
   additionally blocks when the response level has escalated since issue
   (`REAUTHORIZATION_REQUIRED` + `RESPONSE_LEVEL_ESCALATED`). Because state is an input rather than a
   private attribute, "the state this decision was made under" is a thing that can be hashed and named.

4. **A response-level change is a hash-chained `ControlEvent`** in the same per-tenant chain as decisions
   (R-GRAD-2). The level is data, so a change to it is an event with an actor, a trigger and a link — and
   an automatic *de-escalation* is refused: downward requires `actor.type == "human"` (R-GRAD-1), asserted
   in the contract and again in a database `CHECK`.

5. **Level 5 stays reachable independently of the policy engine** (R-GRAD-3). The kill switch is read from
   the environment immediately before the broker mutation (E4). If the engine, the policy loader or the
   ledger is broken, the halt still works, because it does not go through any of them.

### Alternatives considered and why they were rejected

**A stateful engine object (`RiskEngine` holding running state).** Rejected because it cannot be replayed
deterministically. To reproduce decision *N* you would have to reconstruct the object's state at decision
*N*, which means replaying decisions 1..*N*−1 in order and assuming nothing else mutated it — no
concurrent evaluation, no restart, no maintenance job, no second worker. In practice it also makes the
engine untestable in isolation (every test becomes a sequence), makes concurrency a correctness problem
rather than a throughput problem, and makes an audit answer to "why was this rejected?" depend on data
that was never written down. It is the natural shape, and it trades away the entire proof layer for it.

**Let the engine read state itself — query the ledger and the broker inside `evaluate`.** This is the same
objection with extra steps: the function is now impure in a way that also depends on wall-clock timing and
network availability. It breaks E8 (the engine must run and reject with everything else offline), and a
replay would re-read *today's* state instead of the state the decision was made under, so it would
faithfully reproduce a different answer.

**Snapshot the state into the record but keep the engine stateful, using the snapshot only on replay.**
Two code paths for the same computation, one of which is exercised only during incidents. The paths would
diverge, and the divergence would be discovered while investigating something else.

**Ignore the Risk Canon P0 items until Sprint 3.** The Canon is right that these are architectural: adding
a state parameter to a frozen contract later is a schema-version bump and a migration of every record.
Making the fields optional-by-default now costs nothing today and buys the whole capability later.

## Consequences

- **Context providers carry the assembly burden.** All the awkward work — reading the ledger for path
  state, aggregating open and pending exposure across agents, computing the calendar session, fetching the
  current response level — moves to L3, where it can use a clock, a network and a database. The engine
  gets a value object. That is the correct division: the messy part is the part that should not need to be
  deterministic, and the deterministic part is the part that decides.
- **Records are larger**, because every decision carries the full state it was evaluated against rather
  than a reference to state held elsewhere. This is the cost of A5: a customer verifying a record must not
  need our runtime to interpret it, so the record has to be self-contained.
- **The engine stays trivially testable and trivially parallel.** No fixtures that build a history, no
  shared instance, no ordering assumptions; `evaluate` can run concurrently for many tenants because it
  holds nothing.
- **Assembly bugs become visible as blocking rejects rather than as silently permissive decisions.** If a
  context provider fails to compute `aggregate_state` and the policy enables an aggregate check, the
  result is `AGGREGATE_STATE_MISSING` and a refusal — noisy, attributable, and safe. That is E2 doing what
  it is for.
- **Invariant 18** (`semantic_layer_disabled_produces_identical_verdict`, Addendum 1 §D) rests on this:
  the `RiskEvaluation` is byte-identical whether or not an advisory opinion exists, because the advisory
  layer is not an input to the deterministic function at all.
