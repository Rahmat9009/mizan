/** Raw JSON emitted by the authoritative `mizan.api` v1 surface. */

export type DecimalString = string;

export interface BackendDecisionList {
  decisions: BackendDecisionSummary[];
}

export interface BackendDecisionSummary {
  decision_id: string;
  sequence: number;
  proposal_id: string;
  verdict: 'APPROVE' | 'REDUCE' | 'REJECT';
  reason_codes: string[];
  original: BackendQuantities;
  authorized: BackendAuthorized;
  policy: BackendPolicyRef;
  decision_timestamp: string;
  audit_hash: string;
  authorization: {
    auth_id: string;
    expires_at: string;
    ttl_seconds: number;
    environment: 'paper';
  } | null;
}

export interface BackendDecisionRecord extends BackendDecisionSummary {
  schema_version: string;
  tenant_id: string;
  agent_id: string;
  engine_version: string;
  audit_prev_hash: string;
  recorded_at: string;
  proposal: BackendProposal;
  risk_context: BackendRiskContext;
  risk_evaluation: BackendRiskEvaluation;
  governor_decision: BackendGovernorDecision;
  policy_snapshot: BackendPolicy;
  checks: BackendCheck[];
  llm_advisory: BackendAdvisory | null;
  execution: BackendExecution | null;
}

export interface BackendPolicyRef {
  policy_id: string;
  version: string;
  hash: string;
}

export interface BackendQuantities {
  total_quantity: DecimalString;
  total_notional: DecimalString | null;
}

export interface BackendAuthorized extends BackendQuantities {
  legs: { leg_index: number; quantity: DecimalString }[];
  reductions: {
    source: 'deterministic' | 'advisory';
    from_quantity: DecimalString;
    to_quantity: DecimalString;
    reason_code: string;
  }[];
}

export interface BackendAgentIdentity {
  agent_id: string;
  agent_type: string;
  agent_version: string;
  framework: string;
}

export interface BackendLeg {
  leg_index: number;
  side: 'buy' | 'sell';
  contract_type: 'call' | 'put' | null;
  strike: DecimalString | null;
  expiry: string | null;
  quantity: DecimalString;
  limit_price: DecimalString | null;
  order_type: 'limit' | 'market';
}

export interface BackendProposal {
  proposal_id: string;
  agent: BackendAgentIdentity;
  model: { provider: string; model: string; version: string; prompt_hash: string };
  created_at: string;
  expires_at: string;
  intent: 'open' | 'close' | 'adjust';
  symbol: string;
  asset_class: 'equity' | 'equity_option';
  strategy: string;
  legs: BackendLeg[];
  reasoning: string;
  confidence: DecimalString | null;
  signal_sources: string[];
  invalidation: { level: DecimalString; direction: 'below' | 'above'; target: DecimalString | null } | null;
}

export interface BackendCheck {
  check_id: string;
  passed: boolean;
  severity: 'blocking' | 'warning' | 'info';
  reason_code: string | null;
  threshold: DecimalString | null;
  actual: DecimalString | null;
  data_source: string | null;
  snapshot_ts: string | null;
  recommended_quantity: DecimalString | null;
  detail: string;
}

export interface BackendRiskEvaluation {
  verdict: 'PASS' | 'REDUCE' | 'REJECT';
  checks: BackendCheck[];
  original_quantity: DecimalString;
  recommended_quantity: DecimalString;
  original_notional: DecimalString | null;
  recommended_notional: DecimalString | null;
  data_complete: boolean;
  evaluated_at: string;
}

export interface BackendAdvisory {
  profile: string;
  invoked: boolean;
  available: boolean;
  recommendation: 'CONCUR' | 'REDUCE' | 'REJECT' | null;
  recommended_quantity: DecimalString | null;
  reasoning: string;
  authority_ceiling: string;
  provider_ref: string | null;
}

export interface BackendGovernorDecision {
  verdict: 'APPROVE' | 'REDUCE' | 'REJECT';
  reason_codes: string[];
  original: BackendQuantities;
  authorized: BackendAuthorized;
  llm_advisory: BackendAdvisory | null;
  decision_timestamp: string;
}

export interface BackendPosition {
  symbol: string;
  asset_class: 'equity' | 'equity_option';
  quantity: DecimalString;
  market_value: DecimalString;
  sector: string | null;
  occ_symbol: string | null;
  delta: DecimalString | null;
  gamma: DecimalString | null;
  vega: DecimalString | null;
}

export interface BackendPortfolioSnapshot {
  snapshot_id: string;
  as_of: string;
  equity: DecimalString;
  cash: DecimalString;
  buying_power: DecimalString | null;
  peak_equity: DecimalString | null;
  daily_pnl: DecimalString | null;
  positions: BackendPosition[];
  source: string;
  gross_exposure: DecimalString | null;
  net_exposure: DecimalString | null;
}

export interface BackendRiskContext {
  evaluated_at: string;
  response_level: 0 | 1 | 2 | 3 | 4 | 5;
  portfolio_snapshot: BackendPortfolioSnapshot | null;
  market_snapshot: {
    snapshot_id: string;
    as_of: string;
    source: string;
    quotes: Record<string, { price: DecimalString; bid: DecimalString | null; ask: DecimalString | null }>;
    option_quotes: Record<string, { mark: DecimalString; iv: DecimalString | null }>;
  } | null;
  aggregate_state: {
    as_of: string;
    crowding_score: DecimalString | null;
    days_to_liquidate_book: DecimalString | null;
    exposure_by_agent: Record<string, DecimalString>;
    exposure_by_model_provider: Record<string, DecimalString>;
    exposure_by_signal_source: Record<string, DecimalString>;
    pending_intents: { agent_id: string; symbol: string; notional: DecimalString }[];
  } | null;
}

export interface BackendAuthorization {
  auth_id: string;
  issued_at: string;
  expires_at: string;
  ttl_seconds: number;
  environment: 'paper';
  single_use: true;
  bound_state: {
    portfolio_state_hash: string;
    market_snapshot_id: string;
    market_snapshot_hash?: string;
  };
}

export interface BackendExecution {
  status: 'SUBMITTED' | 'WOULD_SUBMIT' | 'BLOCKED' | 'FAILED' | 'RECONCILED_EXISTING';
  reason_codes: string[];
  client_order_id: string | null;
  broker_order_id: string | null;
  checked_at: string;
  authorization_validated_at: string | null;
  kill_switch_checked_at: string | null;
  submitted_at: string | null;
  broker_status: string | null;
  message: string;
  broker: { name: string; environment: 'paper' };
  fills: { filled_quantity: DecimalString; avg_price: DecimalString; filled_at: string }[];
  revalidation: { performed: boolean; supported: boolean; state_changed: boolean };
}

export interface BackendPolicy {
  policy_id: string;
  policy_version: string;
  policy_hash: string;
  order: { max_notional: DecimalString; max_quantity: DecimalString; max_legs: number };
  portfolio: {
    max_single_symbol_pct: DecimalString;
    max_sector_concentration_pct: DecimalString | null;
    max_drawdown_pct: DecimalString;
    max_buying_power_utilization: DecimalString;
  };
  options: {
    max_portfolio_delta: DecimalString;
    max_portfolio_gamma: DecimalString;
    max_portfolio_vega: DecimalString;
    min_days_to_expiry: number;
    max_days_to_expiry: number;
    undefined_risk_requires_approval: true;
  } | null;
  authorization: { ttl_seconds: number };
  aggregate?: {
    max_portfolio_exposure_pct: DecimalString;
    max_correlated_intent_agents: number;
    max_exposure_per_model_provider_pct: DecimalString | null;
    max_exposure_per_signal_source_pct: DecimalString | null;
    crowding_score_threshold: DecimalString | null;
    max_days_to_liquidate_book: DecimalString | null;
  } | null;
}

export interface BackendHealth {
  status: 'ok';
  tenant_id?: string;
  environment?: 'paper';
  execution?: { enabled: boolean; dry_run: boolean; kill_switch_active: boolean };
  broker?: string | null;
}

export interface BackendChainVerification {
  ok: boolean;
  length: number;
  first_bad_sequence: number | null;
  detail: string;
  head_sequence: number | null;
  head_hash: string | null;
}
