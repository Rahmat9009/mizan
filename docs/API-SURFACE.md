# Mizan core — API surface (L0 interface spec)

**Status:** binding for Sprint 1 (L0). Frozen together with `contracts/` at the end of Sprint 1.
Lanes L1–L4 implement the modules in §3 exactly as signed here. Changes go through `ledger/requests.md`.

Source of truth for *data shapes* is `contracts/*.schema.json`. This document is the source of truth for
*module paths, function signatures and behavioural rules* that the invariant suite tests against.

---

## 0. Conventions (apply everywhere)

- Python 3.12, pydantic v2, one package root `mizan/`.
- **Numbers.** Every money, price, quantity, ratio, greek and notional value is a `DecimalStr` — a JSON *string*
  matching `^-?(0|[1-9]\d*)(\.\d+)?$`, normalised (no exponent, no trailing zeros, `-0` → `0`). Counts
  (`max_legs`, `leg_index`, `sequence`) and durations in seconds (`ttl_seconds`, `window_seconds`, `*_days`) are JSON
  integers. **JSON numbers are rejected for DecimalStr fields** (Hard Rule A6).
- **Time.** `Rfc3339` strings, UTC, canonical form `YYYY-MM-DDTHH:MM:SS.ssssssZ` (six fractional digits, always `Z`).
  Inputs in other RFC3339 forms are normalised on validation. Time is an *input* to the engine: `mizan.risk`,
  `mizan.governor`, `mizan.replay`, `mizan.policy`, `mizan.authorization` never read the wall clock. The execution gate
  takes an injectable `clock`.
- **Canonical JSON** (`mizan.contracts.canonical.canonical_json`): keys sorted by Unicode code point, separators
  `(",", ":")`, `ensure_ascii=False`, UTF-8; `float`, `NaN`, `Infinity` raise `TypeError`. Object hash =
  `sha256_hex(canonical_json(obj))`. Full spec in `contracts/CANONICAL.md`.
- **Models.** Every contract model: `model_config = ConfigDict(extra="forbid", frozen=True, strict=True)` and a
  `schema_version: Literal["1.0.0"]` field. Unknown fields are a validation error.
- **No float** in `mizan/contracts`, `mizan/policy`, `mizan/risk`, `mizan/governor`, `mizan/authorization`,
  `mizan/audit`, `mizan/replay`: no `float` name, no float literal, no `math` import. Arithmetic uses `decimal.Decimal`
  inside `mizan.contracts.canonical.DECIMAL_CONTEXT`.
- **`reasoning` is audit-only.** The attribute name `reasoning` may be read only in `mizan/advisory` and `mizan/audit`
  (and `mizan/console` for display). Never in `mizan/risk`, `mizan/governor`, `mizan/policy`, `mizan/authorization`,
  `mizan/execution`.
- **Reason codes** come from `contracts/reason_codes.json` via `mizan.contracts.reason_codes.ReasonCode`. Every REJECT or
  REDUCE carries at least one code (A4). Lists of reason codes are sorted and de-duplicated.
- **Paper only.** The literal value `"paper"` is the only member of every `environment` enum in the contracts. There is
  no field, flag, or code path that can express live (B1).

---

## 1. Package layout and lane ownership

```
mizan/contracts/      L0   frozen with contracts/  (generated types + canonical utils)
mizan/policy/         L1
mizan/risk/           L1
mizan/governor/       L2
mizan/advisory/       L2   LLM adapter — the only enforcement-adjacent module allowed to read proposal.reasoning
mizan/authorization/  L2
mizan/audit/          L2
mizan/replay/         L2
mizan/execution/      L3
mizan/adapters/       L3
mizan/sdk/            L3
mizan/api/            L3
mizan/console/        L4
app/                  legacy (Rahmat) — read-only salvage reference; its tests must keep passing
tests/contracts/      L0        tests/invariants/   READ-ONLY to all after S1
tests/fixtures/       L0        tests/policy/ tests/risk/  L1
tests/governor/ tests/advisory/ tests/authorization/ tests/audit/ tests/replay/   L2
tests/execution/ tests/adapters/ tests/sdk/ tests/api/   L3
tests/console/        L4        tests/security/  L5        tests/integration/  L6
```

L0 creates every `mizan/<lane-module>/__init__.py` as a **typed stub** whose functions raise
`NotImplementedError("L<n> implements this in Sprint 2")`. Lanes replace the stubs.

---

## 2. `mizan.contracts` (L0)

Modules: `types`, `canonical`, `reason_codes`, `errors`, `trade_proposal`, `risk_context`, `policy`,
`risk_evaluation`, `governor_decision`, `execution_authorization`, `execution_result`, `decision_record`.
`mizan/contracts/__init__.py` re-exports every public name below.

### 2.1 `types`
- `DecimalStr` — validated + normalised string (see §0). `PositiveDecimalStr` (> 0), `NonNegativeDecimalStr` (≥ 0),
  `RatioStr` (0 ≤ x ≤ 1).
- `dec(value: str) -> Decimal` and `dstr(value: Decimal) -> str` (normalising formatter). Both live in `types`.
- `Rfc3339` — validated + normalised timestamp string. `parse_ts(s) -> datetime` (tz=UTC), `format_ts(dt) -> str`.
- `DateStr` — `YYYY-MM-DD`.
- `TenantId` `^[a-z0-9][a-z0-9-]{0,62}$` · `AgentId` `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` ·
  `Symbol` `^[A-Z][A-Z0-9.-]{0,15}$` · `OccSymbol` `^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$` · `Sha256Hex` `^[0-9a-f]{64}$` ·
  `PolicyId` `^[a-z0-9][a-z0-9-]{0,62}$` · `SemVer`.

### 2.2 `canonical`
```python
canonical_json(obj: Any) -> str                 # pydantic models via model_dump(mode="json"); dict/list/str/int/bool/None
sha256_hex(data: str | bytes) -> str
normalize_decimal_str(s: str) -> str
proposal_id_for(payload: Mapping) -> str        # sha256_hex(canonical_json(payload without "proposal_id" and "reasoning"))
policy_hash_for(payload: Mapping) -> str        # sha256_hex(canonical_json(payload without "policy_hash"))
record_hash_for(payload: Mapping) -> str        # sha256_hex(canonical_json(payload without "audit_hash"))
authorization_hash_for(payload: Mapping) -> str # sha256_hex(canonical_json(payload without "authorization_hash"))
evaluation_id_for(payload: Mapping) -> str      # sha256_hex(canonical_json(payload without "evaluation_id"))
verdict_hash_for(verdict, reason_codes, authorized_total_quantity, authorized_legs, evaluation_id) -> str
uuid7() -> str                                  # RFC 9562 v7, lowercase; local implementation (py3.12 has none)
ZERO_HASH = "0" * 64
DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
redact(obj: Any) -> Any                          # recursive; keys matching SENSITIVE_KEY_PATTERNS -> "[REDACTED]"
SENSITIVE_KEY_PATTERNS: tuple[str, ...]         # apikey, api_key, secret, token, password, passwd, authorization,
                                                # credential(s), header(s), cookie, private_key, connection_string, dsn
ENGINE_VERSION: str                              # f"mizan-core/{mizan.__version__}"
library_versions() -> dict[str, str]             # {"python": ..., "pydantic": ..., "jsonschema": ..., "pyyaml": ...}
```

### 2.3 `trade_proposal`
```python
class AgentIdentity: agent_id: AgentId; agent_type: Literal["trader","analyst","portfolio_manager"]; agent_version: str; framework: Literal["tradingagents","ai-hedge-fund","custom"]
class ModelIdentity: provider: str; model: str; version: str; prompt_hash: Sha256Hex
class Leg:
    leg_index: int (>=0); side: Literal["buy","sell"]
    contract_type: Literal["call","put"] | None; strike: PositiveDecimalStr | None; expiry: DateStr | None
    quantity: PositiveDecimalStr; limit_price: PositiveDecimalStr | None; order_type: Literal["limit","market"]
class TradeProposal:
    schema_version: Literal["1.0.0"]; proposal_id: Sha256Hex
    agent: AgentIdentity; model: ModelIdentity
    created_at: Rfc3339; expires_at: Rfc3339          # expires_at > created_at
    intent: Literal["open","close","adjust"]
    symbol: Symbol; asset_class: Literal["equity","equity_option"]
    strategy: Literal["long_equity","short_equity","long_call","long_put","bull_call_spread","bear_put_spread",
                      "bull_put_spread","bear_call_spread","iron_condor","custom"]
    legs: list[Leg]  (1..4; leg_index == position, i.e. 0..n-1 ascending)
    reasoning: str = ""  (max 20000 chars)             # AUDIT ONLY
    market_snapshot_ref: str; portfolio_snapshot_ref: str
    # helpers
    @property total_quantity -> Decimal               # sum of leg quantities
    @property notional_estimate -> Decimal | None     # sum(qty*limit_price) when every leg has a limit price
    @classmethod build(**fields_without_proposal_id) -> TradeProposal   # computes proposal_id
```
Validators: `proposal_id == proposal_id_for(model_dump)` else error; equity ⇒ every leg has
`contract_type/strike/expiry = None`; equity_option ⇒ all three present on every leg; `order_type == "market"` ⇒
`limit_price is None`; `"limit"` ⇒ present; leg count matches strategy (long_* = 1, *_spread = 2, iron_condor = 4,
long/short_equity = 1, custom = 1..4).

### 2.4 `risk_context`
```python
class Quote: symbol: Symbol; price: PositiveDecimalStr; bid: PositiveDecimalStr|None; ask: PositiveDecimalStr|None; as_of: Rfc3339; source: str
class OptionQuote: occ_symbol: OccSymbol; mark: PositiveDecimalStr; delta: DecimalStr|None; gamma: DecimalStr|None; vega: DecimalStr|None; theta: DecimalStr|None; as_of: Rfc3339; source: str
class MarketSnapshot: snapshot_id: str; as_of: Rfc3339; quotes: dict[Symbol, Quote]; option_quotes: dict[OccSymbol, OptionQuote] = {}; sectors: dict[Symbol, str] = {}; source: str
class Position: symbol: Symbol; asset_class: Literal["equity","equity_option"]; quantity: DecimalStr (signed); market_value: DecimalStr; sector: str|None; occ_symbol: OccSymbol|None; delta: DecimalStr|None; gamma: DecimalStr|None; vega: DecimalStr|None
class PortfolioGreeks: delta: DecimalStr|None; gamma: DecimalStr|None; vega: DecimalStr|None
class PortfolioSnapshot: snapshot_id: str; as_of: Rfc3339; equity: PositiveDecimalStr; cash: DecimalStr; buying_power: NonNegativeDecimalStr | None; peak_equity: PositiveDecimalStr|None; daily_pnl: DecimalStr|None; positions: list[Position]; greeks: PortfolioGreeks|None; source: str
class RecentOrder: proposal_id: Sha256Hex; symbol: Symbol; side: Literal["buy","sell"]; total_quantity: DecimalStr; submitted_at: Rfc3339; status: str
class PolicyRef: policy_id: PolicyId; version: SemVer; hash: Sha256Hex
class RiskContext:
    schema_version; context_id: str; tenant_id: TenantId; agent_id: AgentId
    evaluated_at: Rfc3339                             # the engine's "now"
    policy: PolicyRef
    market_snapshot: MarketSnapshot | None            # None => MARKET_DATA_MISSING
    portfolio_snapshot: PortfolioSnapshot | None      # None => PORTFOLIO_STATE_MISSING
    recent_orders: list[RecentOrder] = []
    engine_version: str
```
Missing data is `None`/absent, never zero (E2).

### 2.5 `policy`
```python
CHECK_IDS: tuple[str, ...] = (
  "market_data_presence", "portfolio_state_presence", "proposal_expiry",        # always-on, cannot be disabled
  "restricted_symbol", "restricted_strategy", "leg_limit", "position_limit", "capital_threshold",
  "buying_power_sufficiency", "buying_power_utilization", "concentration_limit", "sector_concentration",
  "drawdown_limit", "duplicate_order", "erroneous_order",
  "days_to_expiry", "options_delta_limit", "options_gamma_limit", "options_vega_limit",
)
ALWAYS_ON_CHECKS = CHECK_IDS[:3]
class OrderLimits: max_notional: PositiveDecimalStr; max_quantity: PositiveDecimalStr; max_legs: int (1..4)
class PortfolioLimits: max_single_symbol_pct: RatioStr; max_sector_concentration_pct: RatioStr|None; max_drawdown_pct: RatioStr; max_buying_power_utilization: RatioStr
class OptionsLimits: max_portfolio_delta: DecimalStr; max_portfolio_gamma: DecimalStr; max_portfolio_vega: DecimalStr; min_days_to_expiry: int (>=0); max_days_to_expiry: int (> min)
class Restricted: symbols: list[Symbol] = []; strategies: list[str] = []
class CheckConfig: enabled: bool = True; severity: Literal["blocking","warning"] = "blocking"; window_seconds: int|None; price_deviation_threshold: RatioStr|None; quantity_deviation_threshold: DecimalStr|None
class AdvisoryConfig: enabled: bool; profile: str; authority_ceiling: Literal["reduce_or_reject"]
class AuthorizationConfig: ttl_seconds: int = 15   (5..30)
class FailClosed: on_missing_market_data: Literal[True] = True; on_missing_portfolio_state: Literal[True] = True; on_engine_degraded: Literal[True] = True; on_advisory_unavailable: bool = False
class Policy:
    schema_version; policy_id: PolicyId; policy_version: SemVer; policy_hash: Sha256Hex; tenant_id: TenantId
    order: OrderLimits; portfolio: PortfolioLimits; options: OptionsLimits | None; restricted: Restricted
    checks: dict[str, CheckConfig]   # keys ⊆ CHECK_IDS; missing key == default CheckConfig(); ALWAYS_ON_CHECKS may not be disabled
    advisory: AdvisoryConfig; authorization: AuthorizationConfig; fail_closed: FailClosed
    @property ref -> PolicyRef
    @classmethod build(**fields_without_hash) -> Policy
```
Validator: `policy_hash == policy_hash_for(model_dump)`. Note `FailClosed.on_missing_*` are `Literal[True]`: the
contract cannot express turning them off.

### 2.6 `risk_evaluation`
```python
class CheckResult: check_id: str (in CHECK_IDS); passed: bool; severity: Literal["blocking","warning","info"]; reason_code: ReasonCode|None; threshold: DecimalStr|None; actual: DecimalStr|None; data_source: str|None; snapshot_ts: Rfc3339|None; recommended_quantity: DecimalStr|None; detail: str
class RiskEvaluation:
    schema_version; evaluation_id: Sha256Hex; proposal_id: Sha256Hex; context_id: str; tenant_id: TenantId
    policy: PolicyRef; engine_version: str; evaluated_at: Rfc3339
    verdict: Literal["PASS","REDUCE","REJECT"]; reason_codes: list[ReasonCode]; checks: list[CheckResult]
    original_quantity: DecimalStr; recommended_quantity: DecimalStr
    original_notional: DecimalStr|None; recommended_notional: DecimalStr|None
    data_complete: bool
    @classmethod build(**fields_without_evaluation_id) -> RiskEvaluation
```
Validators: `evaluation_id == evaluation_id_for(dump)`; REJECT ⇒ `recommended_quantity == "0"`; REDUCE ⇒
`0 < recommended < original`; PASS ⇒ `recommended == original`; any failed check with severity `blocking` ⇒ REJECT;
REJECT/REDUCE ⇒ `reason_codes` non-empty; `checks` ordered by `CHECK_IDS`.

### 2.7 `governor_decision`
```python
class AdvisoryOpinion:
    profile: str; invoked: bool; available: bool
    recommendation: Literal["CONCUR","REDUCE","REJECT"] | None     # there is NO value meaning "increase" or "approve"
    recommended_quantity: DecimalStr | None; reasoning: str = ""; authority_ceiling: Literal["reduce_or_reject"]
    provider_ref: str | None; raw_hash: Sha256Hex | None
class Quantities: total_quantity: DecimalStr; total_notional: DecimalStr | None
class AuthorizedLegQuantity: leg_index: int; quantity: DecimalStr
class Reduction: source: Literal["deterministic","advisory"]; from_quantity: DecimalStr; to_quantity: DecimalStr; reason_code: ReasonCode
class Authorized: total_quantity: DecimalStr; total_notional: DecimalStr|None; legs: list[AuthorizedLegQuantity]; reductions: list[Reduction]
class GovernorDecision:
    schema_version; decision_id: str (uuid7); proposal_id; evaluation_id; tenant_id; agent_id
    policy: PolicyRef; engine_version; decision_timestamp: Rfc3339
    verdict: Literal["APPROVE","REDUCE","REJECT"]; reason_codes: list[ReasonCode]
    original: Quantities; authorized: Authorized; llm_advisory: AdvisoryOpinion | None
    verdict_hash: Sha256Hex
```
Validators: `available is False ⇒ recommendation is None`; `REDUCE ⇒ recommended_quantity present`;
`authorized.total_quantity <= original.total_quantity` (the type cannot express "more"); REJECT ⇒ authorized `"0"`
and no legs; APPROVE ⇒ authorized == original; `verdict_hash == verdict_hash_for(...)`.

### 2.8 `execution_authorization`
```python
class AuthorizedLeg: leg_index: int; side: Literal["buy","sell"]; symbol: Symbol; occ_symbol: OccSymbol|None; contract_type; strike; expiry; quantity: PositiveDecimalStr; limit_price: PositiveDecimalStr|None; order_type
class AuthorizationScope: symbol; asset_class; intent; legs: list[AuthorizedLeg]; total_quantity: PositiveDecimalStr; max_notional: DecimalStr|None
class ExecutionAuthorization:
    schema_version; auth_id: str (uuid7); decision_id; proposal_id; tenant_id; agent_id; policy: PolicyRef; engine_version
    issued_at: Rfc3339; expires_at: Rfc3339; ttl_seconds: int (5..30)
    scope: AuthorizationScope
    idempotency_key: str          # "mz1-" + sha256_hex(canonical_json({"tenant_id","proposal_id","legs": scope.legs}))[:40]
    environment: Literal["paper"]; single_use: Literal[True]
    authorization_hash: Sha256Hex
    @classmethod build(...)
```
Validators: `expires_at - issued_at == ttl_seconds`; idempotency_key recomputed and checked; hash checked.

### 2.9 `execution_result`
```python
class RevalidationReport: performed: bool; fresh_context_id: str|None; fresh_evaluation_id: Sha256Hex|None; fresh_recommended_quantity: DecimalStr|None; supported: bool
class Fill: filled_quantity: DecimalStr; avg_price: DecimalStr; filled_at: Rfc3339
class BrokerRef: name: str; environment: Literal["paper"]
class ExecutionResult:
    schema_version; result_id: str (uuid7); auth_id; decision_id; proposal_id; tenant_id
    status: Literal["SUBMITTED","WOULD_SUBMIT","BLOCKED","FAILED","RECONCILED_EXISTING"]
    reason_codes: list[ReasonCode]; broker: BrokerRef
    client_order_id: str|None; broker_order_id: str|None
    checked_at: Rfc3339; authorization_validated_at: Rfc3339|None; kill_switch_checked_at: Rfc3339|None; submitted_at: Rfc3339|None
    revalidation: RevalidationReport; fills: list[Fill] = []; broker_status: str|None; message: str
```
Validators: SUBMITTED/RECONCILED_EXISTING ⇒ `client_order_id` and `broker_order_id` present; BLOCKED ⇒ reason_codes
non-empty and `broker_order_id is None`; WOULD_SUBMIT ⇒ `broker_order_id is None`.

### 2.10 `decision_record`
```python
class DecisionRecord:
    schema_version; decision_id: str; sequence: int (>=1); tenant_id; agent_id; proposal_id
    engine_version: str; library_versions: dict[str, str]
    policy: PolicyRef; policy_snapshot: Policy
    decision_timestamp: Rfc3339; verdict; reason_codes; checks: list[CheckResult]
    proposal: TradeProposal; risk_context: RiskContext; risk_evaluation: RiskEvaluation; governor_decision: GovernorDecision
    authorization: ExecutionAuthorization | None; execution: ExecutionResult | None
    original: Quantities; authorized: Authorized; llm_advisory: AdvisoryOpinion | None
    recorded_at: Rfc3339
    audit_prev_hash: Sha256Hex          # ZERO_HASH for sequence 1
    audit_hash: Sha256Hex               # record_hash_for(dump)
    @classmethod build(**fields_without_audit_hash) -> DecisionRecord
```
Validators: hash check; `verdict/reason_codes/original/authorized/llm_advisory` equal the embedded
`governor_decision`'s; `decision_id == governor_decision.decision_id`; `sequence == 1 ⇒ audit_prev_hash == ZERO_HASH`.

### 2.11 `reason_codes` and `errors`
- `contracts/reason_codes.json`:
  `{"version": "1.0.0", "codes": {"<CODE>": {"category": str, "default_severity": "blocking|warning|info", "description": str, "check_id": str|null}}}`.
  `ReasonCode(str, Enum)` is generated at import time from that file; `REASON_CODE_VERSION`. Test asserts parity.
- `contracts/error_codes.json` + `mizan.contracts.errors`:
  `class MizanError(Exception)`: `code: ErrorCode`, `http_status: int`, `message: str` (safe, generic), `correlation_id: str`,
  `reason_codes: list[ReasonCode]`. Subclasses: `ValidationFailed(422)`, `NotFound(404)`, `TenantForbidden(403)`,
  `PolicyError(422)`, `EngineError(503)`, `LedgerError(503)`, `ChainIntegrityError(409)`, `AuthorizationError(409)`,
  `ExecutionBlocked(409)`, `BrokerError(503)`, `KillSwitchActive(423)`, `LiveTradingForbidden(403)`,
  `ConfigurationError(500)`, `RateLimited(429)`.

---

## 3. Lane public APIs (stubbed by L0, implemented by lanes)

### 3.1 `mizan.policy` (L1)
```python
load_policy(text: str, *, fmt: Literal["yaml","json"] = "yaml") -> Policy   # Decimal-preserving YAML loader (floats never constructed); computes policy_hash if absent, verifies if present
validate_policy(payload: Mapping) -> Policy
policy_hash(policy: Policy) -> str
diff_policies(old: Policy, new: Policy) -> list[PolicyChange]   # PolicyChange(path: str, old: Any, new: Any)
class PolicyStore(Protocol):
    get(tenant_id, policy_id, version: str|None = None) -> Policy        # raises NotFound
    get_by_hash(tenant_id, policy_hash) -> Policy
    put(policy) -> None; activate(tenant_id, policy_id, version) -> None; active(tenant_id, policy_id) -> Policy
class InMemoryPolicyStore(PolicyStore)
```

### 3.2 `mizan.risk` (L1)
```python
evaluate(proposal: TradeProposal, context: RiskContext, policy: Policy) -> RiskEvaluation
```
Pure: no I/O, no clock, no LLM, no float, no `reasoning`. Behavioural rules:
- `policy.tenant_id != context.tenant_id` → REJECT `TENANT_MISMATCH`; `policy.policy_hash != context.policy.hash` → REJECT `POLICY_HASH_MISMATCH`.
- `market_snapshot is None` or no quote for `proposal.symbol` (and, for options, no option quote for every leg's OCC symbol) → REJECT `MARKET_DATA_MISSING` / `PRICE_MISSING`.
- `portfolio_snapshot is None` → REJECT `PORTFOLIO_STATE_MISSING`; `buying_power is None` → REJECT `BUYING_POWER_MISSING`.
- `proposal.expires_at <= context.evaluated_at` → REJECT `PROPOSAL_EXPIRED`.
- Reductions floor to whole units at the binding cap; a REDUCE that reaches zero is a REJECT.
- Runs every check in `CHECK_IDS` order and records a `CheckResult` for each (disabled checks record `passed=True, severity="info"`).

### 3.3 `mizan.governor` (L2)
```python
govern(proposal: TradeProposal, evaluation: RiskEvaluation, policy: Policy, advisory: AdvisoryOpinion | None, *, context: RiskContext) -> GovernorDecision
```
- Evaluation REJECT → REJECT regardless of advisory; add `HARD_REJECTION_UPHELD`.
- Advisory `None` or `available=False`: if `policy.fail_closed.on_advisory_unavailable` → REJECT `ADVISORY_UNAVAILABLE`; else deterministic verdict stands (add `ADVISORY_UNAVAILABLE` as info when `policy.advisory.enabled`).
- Advisory REJECT → REJECT `ADVISORY_REJECT`. Advisory REDUCE with qty < deterministic recommendation → authorized = qty (`ADVISORY_REDUCE`); qty ≥ recommendation → authorized = recommendation and add `ADVISORY_CLAMPED` when qty > recommendation. CONCUR → recommendation.
- `authorized.total_quantity <= evaluation.recommended_quantity` always. `decision_timestamp = context.evaluated_at`. `decision_id = uuid7()`.
- Never reads `proposal.reasoning` or `advisory.reasoning` for control flow.

### 3.4 `mizan.advisory` (L2)
```python
class AdvisoryProvider(Protocol):
    def advise(self, proposal, evaluation, context, policy) -> AdvisoryOpinion: ...     # may raise
def get_advisory(provider: AdvisoryProvider | None, proposal, evaluation, context, policy, *, timeout_seconds: int = 10) -> AdvisoryOpinion
class OfflineAdvisoryProvider(AdvisoryProvider)     # deterministic, no network, for tests/demos
class OpenAICompatibleAdvisoryProvider(AdvisoryProvider)   # Featherless/OpenAI JSON mode, strict schema, extra=forbid
```
`get_advisory` never raises: any exception, timeout, malformed/extra-field/truncated JSON, or out-of-range quantity yields
`AdvisoryOpinion(invoked=True, available=False, recommendation=None)`. Quantities above `evaluation.recommended_quantity` are
clamped and flagged before the opinion is returned.

### 3.5 `mizan.authorization` (L2)
```python
issue(decision: GovernorDecision, proposal: TradeProposal, policy: Policy, *, now: datetime) -> ExecutionAuthorization   # raises AuthorizationError if decision.verdict == "REJECT"
validate(auth: ExecutionAuthorization, *, now: datetime, decision: GovernorDecision | None = None, proposal: TradeProposal | None = None) -> None
      # raises AuthorizationError with reason_codes: AUTHORIZATION_EXPIRED | AUTHORIZATION_NOT_YET_VALID | AUTHORIZATION_INVALID | AUTHORIZATION_SCOPE_MISMATCH
class AuthorizationRegistry(Protocol):
    consume(auth_id: str) -> bool        # atomic; True exactly once per auth_id
class InMemoryAuthorizationRegistry(AuthorizationRegistry)   # thread-safe
```

### 3.6 `mizan.audit` (L2)
```python
class ChainVerification: ok: bool; length: int; first_bad_sequence: int | None; detail: str
class TenantLedger(Protocol):
    tenant_id: str
    def append(self, *, proposal, risk_context, risk_evaluation, governor_decision, policy_snapshot, authorization=None, execution=None, recorded_at: datetime) -> DecisionRecord
    def get(self, decision_id: str) -> DecisionRecord           # NotFound for unknown AND for other tenants' ids
    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]
    def verify_chain(self) -> ChainVerification
    # There is no update() and no delete(). By construction.
class Ledger(Protocol):
    def for_tenant(self, tenant_id: str) -> TenantLedger
class InMemoryLedger(Ledger)
class SqliteLedger(Ledger)      # SqliteLedger(root_dir: Path): one database file per tenant  <root>/<tenant_id>.sqlite;
                                # table decision_records(sequence INTEGER PRIMARY KEY, decision_id TEXT UNIQUE, audit_prev_hash, audit_hash, record_json);
                                # triggers BEFORE UPDATE / BEFORE DELETE -> RAISE(ABORT, 'append-only')
verify_chain_records(records: Iterable[DecisionRecord]) -> ChainVerification     # pure; offline customer verification (A5)
# CLI:  python -m mizan.audit.verify_chain <sqlite-file | records.jsonl>
```
`append` computes `sequence = last + 1`, `audit_prev_hash = last.audit_hash or ZERO_HASH`, applies `redact`, builds the
record (hash verified by the contract), persists. Persistence is append-only at the storage layer, not only at the API.

### 3.7 `mizan.replay` (L2)
```python
class ReplayResult: decision_id: str; mode: Literal["exact","policy","counterfactual"]; identical: bool
    original_verdict; replayed_verdict; original_reason_codes; replayed_reason_codes; original_verdict_hash; replayed_verdict_hash
    replayed_evaluation: RiskEvaluation; replayed_decision: GovernorDecision
replay(record: DecisionRecord, *, policy: Policy | None = None, advisory: AdvisoryOpinion | Literal["as_recorded"] = "as_recorded") -> ReplayResult
```
`exact` (no overrides): re-run `risk.evaluate` + `governor.govern` on `record.proposal`, `record.risk_context`,
`record.policy_snapshot`, `record.llm_advisory` → `identical` must be True (A1). `policy` mode: different policy → verdict may differ.

### 3.8 `mizan.execution` (L3)
```python
class KillSwitch(Protocol):
    def is_active(self) -> bool: ...
class InMemoryKillSwitch(KillSwitch)          # .activate() / .deactivate()
class EnvKillSwitch(KillSwitch)               # reads MIZAN_KILL_SWITCH from the environment on EVERY call
class ExecutionConfig:                        # frozen dataclass
    paper: Literal[True] = True; enabled: bool = False; dry_run: bool = True
    @classmethod from_environment() -> ExecutionConfig     # ALPACA_PAPER anything but true -> LiveTradingForbidden; MIZAN_EXECUTION_ENABLED; MIZAN_EXECUTION_DRY_RUN
class ExecutionGate:
    def __init__(self, *, broker: BrokerAdapter, kill_switch: KillSwitch, registry: AuthorizationRegistry,
                 context_provider: ContextProvider, policy: Policy, config: ExecutionConfig, clock: Callable[[], datetime]): ...
    def execute(self, auth: ExecutionAuthorization, proposal: TradeProposal, decision: GovernorDecision) -> ExecutionResult: ...
```
`execute()` performs, in this order, each failure returning `BLOCKED` with the code shown and **no broker mutation**:
1. `config.enabled` else `EXECUTION_DISABLED`.
2. `authorization.validate(auth, now=clock(), decision=decision, proposal=proposal)` → `AUTHORIZATION_EXPIRED` / `AUTHORIZATION_SCOPE_MISMATCH` / ….
3. Idempotency: `broker.find_order(auth.idempotency_key)`; found → `RECONCILED_EXISTING` (`IDEMPOTENT_ORDER_EXISTS`), stop.
4. TOCTOU re-validation (E9): `fresh = context_provider.build(...)`, `fresh_eval = risk.evaluate(proposal, fresh, policy)`;
   `fresh_eval.verdict == "REJECT"` or `fresh_eval.recommended_quantity < auth.scope.total_quantity` → `REAUTHORIZATION_REQUIRED` (+ `TOCTOU_STATE_CHANGED`). Never resize (E5). `revalidation.performed` is always True when reached.
5. `registry.consume(auth.auth_id)` False → `AUTHORIZATION_ALREADY_USED`.
6. `authorization.validate(auth, now=clock())` again — freshness immediately before submission (E6).
7. `kill_switch.is_active()` — the **last** check, immediately before the mutation (E4) → `KILL_SWITCH_ACTIVE`.
8. `config.dry_run` → `WOULD_SUBMIT`; else `broker.submit_order(OrderRequest(...))` → `SUBMITTED`.
`kill_switch_checked_at` and `authorization_validated_at` are stamped from `clock()`.

### 3.9 `mizan.adapters` (L3)
```python
class OrderRequest (frozen): client_order_id: str; symbol: Symbol; asset_class; intent; legs: list[AuthorizedLeg]; time_in_force: Literal["day"] = "day"; environment: Literal["paper"] = "paper"
class BrokerOrder (frozen): broker_order_id: str; client_order_id: str; status: str; submitted_at: Rfc3339; filled_quantity: DecimalStr = "0"; avg_price: DecimalStr | None = None; raw_hash: Sha256Hex | None = None
class BrokerAdapter(Protocol):
    name: str; environment: Literal["paper"]
    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot: ...
    def get_market_snapshot(self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: datetime) -> MarketSnapshot: ...
    def find_order(self, client_order_id: str) -> BrokerOrder | None: ...
    def submit_order(self, request: OrderRequest) -> BrokerOrder: ...
    def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    # NO cancel / replace / close_position / close_all (B4)
class MockBroker(BrokerAdapter)                       # mizan/adapters/mock.py — scriptable snapshots, records submitted orders, hooks
class AlpacaPaperBroker(BrokerAdapter)                # mizan/adapters/alpaca_paper.py — from_environment(); LiveTradingForbidden unless ALPACA_PAPER=true; asserts SDK base URL is the paper URL
class ContextProvider(Protocol):
    def build(self, *, tenant_id, agent_id, proposal: TradeProposal, policy: Policy, now: datetime, recent_orders: Sequence[RecentOrder] = ()) -> RiskContext: ...
class BrokerContextProvider(ContextProvider)          # builds a RiskContext from a BrokerAdapter's snapshots
# mizan/adapters/tradingagents.py — TradingAgents adapter (W8)
```

### 3.10 `mizan.sdk` (L3)
```python
class Mizan:
    def __init__(self, *, tenant_id, agent: AgentIdentity, policy: Policy | str, broker: BrokerAdapter | None = None,
                 ledger: Ledger | None = None, advisory: AdvisoryProvider | None = None, kill_switch: KillSwitch | None = None,
                 config: ExecutionConfig | None = None, clock: Callable[[], datetime] | None = None): ...
    def evaluate(self, proposal: TradeProposal) -> DecisionRecord          # context → risk → advisory → governor → authorization (if not REJECT) → ledger.append ; no execution
    def execute(self, decision_id: str) -> ExecutionResult
    def protected(self, fn)                                                # decorator: fn(order) runs only after evaluate+execute approve
    def replay(self, decision_id: str, **kw) -> ReplayResult
    def verify_chain(self) -> ChainVerification
    def get_decision(self, decision_id: str) -> DecisionRecord
```

### 3.11 `mizan.api` (L3)
`create_app(mizan: Mizan | Callable[[str], Mizan]) -> FastAPI`. Routes under `/v1`: `POST /proposals/evaluate`,
`POST /decisions/{decision_id}/execute`, `GET /decisions/{decision_id}`, `GET /decisions`, `POST /decisions/{decision_id}/replay`,
`GET /audit/verify`, `GET /policy`, `POST /control/kill-switch`, `GET /health`. Bearer token per agent → tenant binding;
every route tenant-scoped; errors `{"error": {"code", "message", "correlation_id"}}`, never a traceback.

### 3.12 `mizan.console` (L4)
Reads only through `mizan.sdk` / `mizan.api` read models. `mizan/console/streamlit_app.py` is the disposable debug UI.

---

## 4. Shared test fixtures (L0 owns `tests/fixtures/`)

```python
from tests.fixtures import (
    make_policy, make_proposal, make_option_proposal, make_context, make_market_snapshot, make_portfolio_snapshot,
    make_evaluation, make_decision, make_authorization, FIXED_NOW, FIXED_NOW_STR, TENANT_A, TENANT_B, AGENT_ID,
    killer_demo_reject_proposal, killer_demo_approve_proposal, injection_reasoning,
)
```
Every builder accepts `**overrides` (top-level field overrides) and returns a valid contract object.
`FIXED_NOW = datetime(2026, 9, 2, 17, 40, 0, tzinfo=UTC)`. Additive changes only, via `ledger/requests.md`.
