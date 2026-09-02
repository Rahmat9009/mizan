# Mizan threat model (L5 Security) — skeleton

**Status:** SKELETON, Sprint 1. Every threat below is **PLANNED** until the `mizan/` core exists. The legacy `app/`
column records what the salvage base does today so lanes know what not to carry forward. Findings are in
`security/findings.md`; provable legacy behaviour is pinned under `tests/security/`.

**Binding references:** `docs/MIZAN-MASTER-PLAN-v2.md` §4 (Hard Rules E1–E9, A1–A6, B1–B7), §4.4 (invariant suite),
§8 W11 (this list); `docs/API-SURFACE.md` + `docs/API-SURFACE-ADDENDUM-1.md`; `docs/MIZAN-RISK-CANON.md` §11 (graduated
response ladder, R-GRAD-1..3).

**Invariant test names** are the §4.4 assertion names plus Addendum-1 invariant 18. `tests/invariants/` is being authored
by L0-B (uncommitted at the time of this sweep). Where a section below says `pending L0-B`, read the mapping here:

| §4.4 assertion | `tests/invariants/` file (as of 2026-09-02T15:00Z) |
|---|---|
| `llm_cannot_increase_order_size` | `test_01_llm_cannot_increase_order_size.py` |
| `llm_cannot_overturn_hard_rejection` | `test_02_llm_cannot_overturn_hard_rejection.py` |
| `missing_price_blocks` | `test_03_missing_price_blocks.py` |
| `missing_buying_power_blocks` | `test_04_missing_buying_power_blocks.py` |
| `missing_portfolio_state_blocks` | `test_05_missing_portfolio_state_blocks.py` |
| `expired_authorization_blocks` | `test_06_expired_authorization_blocks.py` |
| `kill_switch_blocks_at_mutation_boundary` | `test_07_kill_switch_blocks_at_mutation_boundary.py` |
| `audit_record_cannot_be_modified` | `test_08_audit_record_cannot_be_modified.py` |
| `audit_record_cannot_be_deleted` | `test_09_audit_record_cannot_be_deleted.py` |
| `hash_chain_verifies` | `test_10_hash_chain_verifies.py` |
| `replay_verdict_is_identical` | `test_11_replay_verdict_is_identical.py` |
| `cross_tenant_access_is_impossible` | `test_12_cross_tenant_access_is_impossible.py` |
| `engine_operates_with_llm_offline` | `test_13_engine_operates_with_llm_offline.py` |
| `toctou_revalidation_occurs` | not on disk yet (expected `test_14_...`) |
| `no_binary_float_in_decision_path` | not on disk yet (expected `test_15_...`) |
| `semantic_layer_disabled_produces_identical_verdict` (Addendum-1 inv. 18) | not on disk yet (`test_18_...` per Addendum-1 §D) |

L5 does not edit `tests/invariants/` (read-only after S1); L5's own pins live in `tests/security/`.

## 0. Assets, actors and trust boundaries

| Asset | Where it lives (new core) | Who may touch it |
|---|---|---|
| Broker credentials (Alpaca paper key/secret) | customer environment variables only (B2); never a Mizan store | `mizan.adapters.alpaca_paper.from_environment()` at construction |
| LLM provider keys | environment only | `mizan.advisory.OpenAICompatibleAdvisoryProvider` |
| Policy (versioned, hashed) | `PolicyStore`, per tenant | L1 loader; activation is a hash-chained `ControlEvent` |
| Portfolio / market snapshots | `RiskContext`, captured verbatim in `DecisionRecord` | context provider (L3); ledger (L2) |
| Agent prompts / LLM reasoning | `proposal.reasoning` (audit only), `AdvisoryOpinion.reasoning`, `prompt_hash`, `raw_hash` | `mizan/advisory`, `mizan/audit`, `mizan/console` display only |
| Decision records + hash chain | one ledger per tenant (SQLite file / Postgres schema) | append only; `verify_chain` offline |
| Kill switch / response level | `KillSwitch.is_active()` read on every call; level changes are `ControlEvent`s | execution gate (last check), humans |

**Actors:** (1) the AI agent / agent framework (untrusted: can lie in every field it authors); (2) the LLM advisory provider
(untrusted: output is data); (3) the broker (semi-trusted: source of truth for fills, can be unavailable or compromised);
(4) an operator with console access (trusted for their tenant only); (5) an unauthenticated network peer; (6) an insider
with database or host access.

**Trust boundaries:** agent → `mizan.api`/`mizan.sdk` (bearer token → tenant binding); Mizan → LLM vendor (data leaves
the boundary); Mizan → broker (only `submit_order`, `find_order`, `get_order`, snapshots; no cancel/replace — B4);
tenant ↔ tenant (separate schema/file — B3).

## 0.1 L5 sweep index (working definitions; 1–2 are this sprint, 3–7 proposed)

| # | Sweep | Re-checks |
|---|---|---|
| 1 | Secret leak prevention (repo, history, logs, errors, responses) | credential leakage |
| 2 | Sensitive data flow (enter → store → log → tenant boundary) | credential leakage, tenant escape, audit redaction |
| 3 | Input validation and injection (prompt, tool, policy, schema strictness) | prompt injection, tool injection, policy tampering, market-data poisoning |
| 4 | Identity, authorization and isolation | agent impersonation, privilege escalation, tenant escape |
| 5 | Concurrency, time and idempotency | replay attacks, nonce/idempotency, race conditions, TOCTOU |
| 6 | Audit integrity and decision-replay determinism | audit tampering, replay attacks (evidence side) |
| 7 | Execution boundary and broker | kill-switch bypass, broker compromise, paper/live separation |

---

## 1. Agent impersonation — PLANNED

- **Attacker capability:** a network peer or a compromised agent process presents itself as another agent (or as a
  human approver) to obtain that identity's policy budget, tenant or authority.
- **Entry point:** `POST /v1/proposals/evaluate`, `POST /v1/decisions/{id}/execute`, SDK `Mizan(tenant_id=..., agent=...)`,
  `TradeProposal.agent` (self-declared `AgentIdentity`).
- **Hard Rules:** E3 (everything crosses the gate), B3 (tenant boundary), A4 (reason codes on rejection). Addendum-1
  `agent_budgets` (per-agent limits) make identity load-bearing.
- **Invariants:** `cross_tenant_access_is_impossible` (pending L0-B); L5 adds `tests/security/test_agent_identity_binding.py`
  (planned): bearer token ↔ `tenant_id` ↔ `AgentId` must be bound server-side, never taken from the body.
- **Sweep:** 4.
- **Legacy `app/`:** no identity at all — no auth header is read, `TradeProposal` has no agent field (F-3, F-17).

## 2. Prompt injection — PLANNED

- **Attacker capability:** the agent (or upstream news/data that reaches the agent) plants instructions in
  `proposal.reasoning`, `thesis`, symbol names or snapshot text so the advisory LLM recommends more size or the console
  renders hostile content.
- **Entry point:** `TradeProposal.reasoning` (≤20 000 chars), any free-text field forwarded to
  `OpenAICompatibleAdvisoryProvider`; LLM output rendered by `mizan/console`.
- **Hard Rules:** E1 (LLM ≤ policy; type cannot express "more"), E8 (engine runs with LLM offline), §0 of API-SURFACE
  (`reasoning` never read outside advisory/audit/console), Verdict §5 (advisory-only, advisory-downward).
- **Invariants:** `llm_cannot_increase_order_size`, `llm_cannot_overturn_hard_rejection`,
  `engine_operates_with_llm_offline`, invariant 18 `semantic_layer_disabled_produces_identical_verdict` (all pending
  L0-B); fixture `injection_reasoning` (L0). L5 adds a static test that `reasoning` is not referenced in
  `mizan/risk|governor|policy|authorization|execution`.
- **Sweep:** 3.
- **Legacy `app/`:** E1 is held (`PortfolioGovernor` takes `min()`; providers clamp to the hard cap) but LLM text flows
  verbatim into `GovernorDecision.reason` and into `unsafe_allow_html` sinks in the Streamlit UI (F-8).

## 3. Tool injection — PLANNED

- **Attacker capability:** the LLM (or a compromised provider) returns tool calls, extra fields, or a
  schema-valid-but-semantically-hostile object to make the advisory adapter execute or persist something.
- **Entry point:** `AdvisoryProvider.advise()` response parsing; MCP server tools (`evaluate_order`, `get_decision`,
  `replay_decision`, `get_policy`) in W7.
- **Hard Rules:** E1, E8; API-SURFACE §3.4 (`extra="forbid"`, strict schema, any anomaly → `available=False`).
- **Invariants:** `llm_cannot_increase_order_size`, `engine_operates_with_llm_offline` (pending L0-B). L5 adds fuzz
  cases for `get_advisory` (tool_calls present, truncated JSON, duplicate keys, quantities above the cap, unicode
  homoglyph keys).
- **Sweep:** 3.
- **Legacy `app/`:** good baseline — `FeatherlessRiskPayload(extra="forbid")`, tool calls rejected, `finish_reason` checked,
  `model_name`/`proposal_id` overwritten from configuration. Carry forward.

## 4. Policy tampering — PLANNED

- **Attacker capability:** an insider or a compromised agent edits, swaps or downgrades the policy that a decision is
  evaluated under, or points a context at a different policy hash.
- **Entry point:** `PolicyStore.put/activate`, `load_policy` (YAML), `RiskContext.policy` (PolicyRef), `GET /v1/policy`.
- **Hard Rules:** A1 (policy version in the verdict), A2 (activation is a chained `ControlEvent`), B7 (customer control);
  API-SURFACE §3.2 (`POLICY_HASH_MISMATCH`, `TENANT_MISMATCH`); Addendum-1 `CHECK_NOT_IMPLEMENTED` refuses at load.
- **Invariants:** `replay_verdict_is_identical`, `hash_chain_verifies` (pending L0-B). L5 adds: a policy whose
  `FailClosed.on_missing_*` is set to false is a validation error; always-on checks cannot be disabled; a policy edited
  after hashing is refused.
- **Sweep:** 3, 6.
- **Legacy `app/`:** `RiskPolicy` is an unversioned dataclass with in-process defaults; decisions carry no policy ref (F-21).

## 5. Credential leakage — PLANNED

- **Attacker capability:** reads a log, an error body, an audit record, a git object, a console page or a debug print
  and obtains a broker/LLM key or a connection string.
- **Entry point:** environment loading, SDK client construction, exception handlers, `redact()`, ledger `append`,
  `FEATHERLESS_DEBUG`-style diagnostics, repository history.
- **Hard Rules:** A3 (recursive redaction before persistence), B2 (Mizan never custodies keys), §14 integrity red line
  ("no secrets in repos, logs or demo videos").
- **Invariants:** none in §4.4 directly; L5 owns `tests/security/test_legacy_redaction.py` (baseline + gaps) and will
  add `tests/security/test_redact_contract.py` against `mizan.contracts.canonical.redact` once it exists (every xfail in
  the legacy file must pass there). L0-C ships the secret scanner + git hook (verify in Sweep 1 next sprint).
- **Sweep:** 1, 2.
- **Legacy `app/`:** no secret in any of the 4 commits; error paths use `type(exc).__name__`; but redaction misses
  proxy-authorization / cookie / set-cookie / private_key / connection_string / dsn / passwd / session / jwt /
  value-embedded secrets / homoglyph keys and is applied to 1 of 4 audit entry points (F-6); debug prints via `print()`
  (F-12); no scanner (F-13).

## 6. Replay attacks — PLANNED

- **Attacker capability:** re-sends a previously valid authorization, decision id or signed request to obtain a second
  execution, or re-submits an old proposal against a now-different portfolio.
- **Entry point:** `POST /v1/decisions/{id}/execute`, `ExecutionAuthorization` objects, SDK `execute(decision_id)`.
- **Hard Rules:** E6 (TTL 5–30 s, re-validated before submission), E7 (deterministic idempotency), E9 (TOCTOU
  re-check), `single_use: Literal[True]`, Addendum-1 `bound_state` (state-bound authorization).
- **Invariants:** `expired_authorization_blocks`, `toctou_revalidation_occurs` (pending L0-B). L5 adds: same
  `auth_id` executed twice → second is `AUTHORIZATION_ALREADY_USED`; an authorization issued under state hash H is
  blocked when fresh state hash ≠ H (`STATE_BINDING_MISMATCH`).
- **Sweep:** 5, 6.
- **Legacy `app/`:** authorizations are re-created on every call and never consumed; freshness window defaults to 120 s
  and may be configured to 3600 s (F-11); duplicate suppression is the broker's (F-9).

## 7. Nonce / idempotency — PLANNED

- **Attacker capability:** forges or collides an idempotency key so a fresh order is treated as an existing one
  (suppression) or an existing one as fresh (duplication).
- **Entry point:** `ExecutionAuthorization.idempotency_key` (`"mz1-" + sha256(canonical{tenant_id, proposal_id, legs})[:40]`),
  `BrokerAdapter.find_order`, `proposal_id = proposal_id_for(payload)`.
- **Hard Rules:** E7; API-SURFACE §2.3 (`proposal_id` recomputed and verified), §2.8 (key recomputed and verified).
- **Invariants:** none named in §4.4; L5 adds: a client-supplied `proposal_id` that does not equal
  `proposal_id_for(payload)` is a validation error; two tenants with byte-identical proposals get different keys;
  a broker-found order whose legs differ from the authorization is `FAILED`, never `RECONCILED_EXISTING`.
- **Sweep:** 5.
- **Legacy `app/`:** key = `sha256(proposal_id)[:40]` over a caller-chosen string (F-10); mismatch check on symbol/side/qty
  exists and is a good pattern to keep.

## 8. Race conditions — PLANNED

- **Attacker capability:** issues concurrent execute calls (same decision, or many decisions against one budget) so
  checks that are individually correct are collectively violated.
- **Entry point:** `ExecutionGate.execute` concurrency, `AuthorizationRegistry.consume`, per-agent budgets and
  aggregate exposure (Addendum-1 `AgentState`, `AggregateState.pending_intents`).
- **Hard Rules:** E3, E5 (no silent resize), E9; API-SURFACE §3.5 (`consume` atomic, exactly once), §3.8 step 5.
- **Invariants:** `toctou_revalidation_occurs`, `kill_switch_blocks_at_mutation_boundary` (pending L0-B). L5 adds a
  threaded test (N threads, one auth → exactly one `SUBMITTED`) and a budget race (N agents, shared aggregate cap).
- **Sweep:** 5.
- **Legacy `app/`:** two concurrent executes both submit against a non-deduplicating adapter (F-9, pinned in
  `tests/security/test_legacy_api_disclosure.py`).

## 9. TOCTOU — PLANNED

- **Attacker capability:** changes the world between authorization and submission (fills elsewhere, drains buying
  power, escalates the response level, swaps the policy) so the executed order violates the policy it was approved under.
- **Entry point:** the window between `authorization.issue` and `broker.submit_order`.
- **Hard Rules:** E4, E6, E9, E5; Addendum-1 §A (state-bound authorization; `RESPONSE_LEVEL_ESCALATED`).
- **Invariants:** `toctou_revalidation_occurs`, `expired_authorization_blocks` (pending L0-B). L5 adds: fresh
  `recommended_quantity < scope.total_quantity` → `REAUTHORIZATION_REQUIRED` and **no** broker call; `revalidation.performed`
  is True on every path that reaches step 4.
- **Sweep:** 5.
- **Legacy `app/`:** `_fresh_risk_check` re-evaluates portfolio state (good) but re-uses the caller-supplied
  `estimated_price` (F-1) and the frozen kill switch (F-4).

## 10. Tenant escape — PLANNED

- **Attacker capability:** a tenant reads or writes another tenant's decisions, policy, ledger or kill switch, by id
  guessing, path traversal in `tenant_id`, or a query-filter bug.
- **Entry point:** every `/v1` route, `Ledger.for_tenant`, `SqliteLedger(root_dir)/<tenant_id>.sqlite`, Postgres
  per-tenant schema (infra), `PolicyStore.get(tenant_id, ...)`.
- **Hard Rules:** B3 (separate schemas, not query filters), §14 legal red line ("cross-tenant leakage is company-ending").
- **Invariants:** `cross_tenant_access_is_impossible` (pending L0-B). L5 adds: `TenantId` regex rejects `..`, `/`,
  `\`, NUL; `get(decision_id)` of another tenant is `NotFound` (not `TenantForbidden`, to avoid an existence oracle);
  chain verification of tenant A is unaffected by writes to tenant B.
- **Sweep:** 2, 4.
- **Legacy `app/`:** no tenant concept; one SQLite file, one broker account, global `/recent` (F-17).

## 11. Audit tampering — PLANNED

- **Attacker capability:** an insider with DB access edits or deletes a decision record, back-dates a control event,
  or re-chains after modification.
- **Entry point:** ledger storage (SQLite file, Postgres schema), `TenantLedger.append`, `verify_chain`.
- **Hard Rules:** A2 (append-only at schema level), A5 (customer-verifiable offline), A1 (canonical serialization),
  R-GRAD-2 (level changes chained).
- **Invariants:** `audit_record_cannot_be_modified`, `audit_record_cannot_be_deleted`, `hash_chain_verifies` (pending
  L0-B). L5 adds: direct `UPDATE`/`DELETE` via a raw connection raises `append-only`; flipping one byte of `record_json`
  makes `verify_chain` report `first_bad_sequence`; `python -m mizan.audit.verify_chain` exit code is non-zero on a bad chain.
- **Sweep:** 6.
- **Legacy `app/`:** no chain, no triggers, `execution_results`/`broker_orders` are upserts (F-5).

## 12. Privilege escalation — PLANNED

- **Attacker capability:** an agent or operator obtains authority above its policy: de-escalates the response ladder,
  flips the kill switch off, activates a weaker policy, or approves its own proposal.
- **Entry point:** `POST /v1/control/kill-switch`, `ControlEvent` (level down requires `actor.type == "human"`,
  R-GRAD-1), policy activation, approval queue (W10).
- **Hard Rules:** E1, E3 ("no admin override, no debug flag"), B7; Addendum-1 §B.6 validators.
- **Invariants:** `llm_cannot_overturn_hard_rejection`, `kill_switch_blocks_at_mutation_boundary` (pending L0-B). L5
  adds: a `ControlEvent` lowering the level with `actor.type == "system"` is a validation error; an agent bearer token
  cannot call `/v1/control/*`.
- **Sweep:** 4.
- **Legacy `app/`:** no roles; the kill switch is an environment variable read once (F-4).

## 13. Kill-switch bypass — PLANNED

- **Attacker capability:** submits an order while the switch is active, by racing the check, caching its state, or
  routing around the gate.
- **Entry point:** `ExecutionGate.execute` step 7 (`kill_switch.is_active()` immediately before the mutation),
  `EnvKillSwitch` (reads env on every call), `Mizan.protected` decorator.
- **Hard Rules:** E4 (checked immediately before the mutation, not at request entry), E3, R-GRAD-3 (Level 5 reachable
  independently of the policy engine).
- **Invariants:** `kill_switch_blocks_at_mutation_boundary` (pending L0-B). L5 adds: switch activated *between* step 6
  and step 7 (injected clock/hook) → `KILL_SWITCH_ACTIVE`, `broker.submit_order` never called, `kill_switch_checked_at`
  stamped; a `MockBroker` hook asserts the switch was read after the last authorization validation.
- **Sweep:** 7.
- **Legacy `app/`:** `ExecutionConfig.kill_switch` frozen at process start (F-4, pinned in
  `tests/security/test_legacy_input_poisoning.py`).

## 14. Broker compromise — PLANNED

- **Attacker capability:** the broker endpoint is spoofed, returns hostile objects, reports fills that did not happen, or
  a live endpoint is substituted for paper.
- **Entry point:** `AlpacaPaperBroker` construction (base-URL assertion), `_map_order`-style mapping, reconciliation,
  `BrokerOrder.raw_hash`.
- **Hard Rules:** B1 (paper and live are separate boundaries), B2, B4 (no cancel/replace), `environment: Literal["paper"]`
  everywhere.
- **Invariants:** none named in §4.4 (paper-only is enforced by contract types); L5 adds: `ALPACA_PAPER` any value but
  `true` → `LiveTradingForbidden` at construction; an SDK client whose base URL is not the paper URL is refused; a broker
  response with extra/unknown fields never reaches a contract model (`extra="forbid"`); TLS/hostname pinning is an L3/infra
  decision to record.
- **Sweep:** 7.
- **Legacy `app/`:** good baseline — base URL verified at adapter construction, raw SDK objects never escape,
  `TradingClient(..., paper=True)` hard-coded; `paper_mode_verified` is a constant `True` (F-19).

## 15. Market-data poisoning — PLANNED

- **Attacker capability:** the agent (or a compromised data vendor) supplies prices, volatility, liquidity or
  calendar data that make an unsafe order look safe.
- **Entry point:** `RiskContext.market_snapshot` (must come from the context provider, never the proposal),
  `Quote.source`, `TradeProposal.legs[].limit_price`, `signal_sources`, Addendum-1 `CalendarState`, `PathState`.
- **Hard Rules:** E2 (missing ≠ safe), E9 (re-check at execution), R-RUIN-4, R-MODEL-*; API-SURFACE §3.2
  (`MARKET_DATA_MISSING` / `PRICE_MISSING`), `erroneous_order` check (price deviation threshold).
- **Invariants:** `missing_price_blocks`, `missing_buying_power_blocks`, `missing_portfolio_state_blocks` (pending L0-B).
  L5 adds: a proposal `limit_price` far from the snapshot quote trips `erroneous_order`; notional is always computed from
  the snapshot quote, never from any proposal field; a market snapshot whose `as_of` is older than the policy's freshness
  window is `MARKET_DATA_MISSING`; the SDK cannot accept a caller-built `MarketSnapshot` for a decision that will execute.
- **Sweep:** 3, 7.
- **Legacy `app/`:** the valuation price (`estimated_price`) and the whole `MarketRiskSnapshot` are caller-supplied and
  unverified — a claimed $0.01 price authorizes 1,000 shares on a $10k account (F-1, F-2; pinned in
  `tests/security/test_legacy_input_poisoning.py`). This is the single most important thing not to carry forward.

---

## Appendix A — Sensitive data flow map (Sweep 2, legacy vs. new core)

| Data | Legacy: enters | Legacy: stored | Legacy: logged | Legacy: tenant boundary | New core (by design) |
|---|---|---|---|---|---|
| Broker credentials | `.env` → `load_dotenv()` (6 call sites, walks parent dirs) → `os.getenv` in `app/alpaca/client.py` | process memory; inside `TradingClient` | never (errors name the variable only) | NONE (one account) | env only, at adapter construction; never in any contract or record (B2) |
| LLM keys | `.env` → `FeatherlessRiskProvider.__init__`, `ClaudeRiskProvider.__init__`, `probe_featherless.py` | process memory; inside SDK client | never | NONE | env only, `mizan/advisory` |
| Portfolio positions / buying power / equity / cash | Alpaca `get_account`/`get_all_positions` → `PortfolioSnapshot` | `portfolio_snapshots` table (per proposal, immutable); served by `/portfolio`, `/proposals/{id}`, `/recent` to anyone; held in Streamlit `session_state` | demo scripts print it | NONE — global | `RiskContext.portfolio_snapshot` captured in the tenant's `DecisionRecord`; served only tenant-scoped |
| Account identifiers | not mapped (no account id in `PortfolioSnapshot`); `alpaca_order_id` is exposed | `broker_orders` | — | NONE | `BrokerOrder.broker_order_id` tenant-scoped; account ids never in contracts |
| Agent prompt (thesis, invalidation) | `POST /proposals/evaluate` body (unbounded length) | `proposals.payload_json`; forwarded verbatim to Featherless/Anthropic inside the user prompt together with the whole portfolio | `FEATHERLESS_DEBUG` prints the model's echo | NONE | `proposal.reasoning` ≤20k chars, hashed into `ModelIdentity.prompt_hash`; audit-only |
| LLM reasoning text | provider JSON → `AIRiskAnalysis.reasoning/hidden_risks/risk_thesis` | `ai_risk_analyses`, `audit_events` (unsanitised), joined into `GovernorDecision.reason` | stdout when debug | NONE | `AdvisoryOpinion.reasoning` + `raw_hash`; redacted at `append`; console must escape |
| Decision records | in-process pipeline | `governor_decisions` (immutable insert), `audit_events` (no chain, no triggers), `execution_results`/`broker_orders` (upserts) | demo scripts print timelines | NONE | hash-chained `DecisionRecord`, one chain per tenant, triggers, offline `verify_chain` |
