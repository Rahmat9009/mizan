/**
 * Domain types for the Mizan frontend.
 *
 * These mirror the backend contract in `docs/integration-contract.md` and the
 * pydantic models under `app/`. They are deliberately UI-shaped rather than
 * broker-shaped: nothing here reflects a raw Alpaca SDK payload.
 *
 * Step 2 fills these from `src/data`. Step 3 fills the same types from HTTP
 * without any component changing.
 *
 * The authorization contract (`DecisionOutcome`, `CheckMeasurement`,
 * `Authorization`, `ChainStamp`) follows MIZAN-UX-SPEC §8. Those fields do not
 * exist in the current backend contract; they are typed here so the frontend
 * asks for the right shape rather than inventing one later.
 */

import type { Decimal } from '@/lib/decimal';

export type { Decimal };

/* ---------------------------------------------------------------- provenance */

/**
 * Where a displayed value came from. Every metric that could be mistaken for
 * live market truth carries one of these.
 */
export type Provenance =
  | 'ALPACA_PAPER'
  | 'LIVE_PORTFOLIO'
  | 'LIVE_AGENT'
  | 'MIZAN_LEDGER'
  | 'MIZAN_POLICY'
  | 'AI_RISK_MODEL'
  | 'CALLER_SUPPLIED'
  | 'DEMO'
  | 'SYNTHETIC';

/**
 * A value that may legitimately be absent. An unavailable number is never
 * rendered as zero — `null` means "the backend cannot tell us", which is a
 * different fact from "the value is 0".
 */
export type Sourced<T> = {
  value: T | null;
  provenance: Provenance;
  /** Why the value is unavailable, when it is. */
  unavailableReason?: string;
  asOf?: string;
};

/* --------------------------------------------------------------- primitives */

export type Side = 'BUY' | 'SELL';

/**
 * The verdict. Exactly three values (MIZAN-UX-SPEC §1, Correction 2).
 *
 * REDUCE changes quantity and nothing else. A price the policy will not accept
 * produces REJECT and a new proposal, never a repriced order — repricing is a
 * trading decision, and this system holds no view on value.
 */
export type Decision = 'APPROVE' | 'REDUCE' | 'REJECT';

export type InstrumentType = 'equity' | 'option';
export type RiskSeverity = 'INFO' | 'WATCH' | 'HIGH' | 'BLOCK';

export type AutonomyMode = 'OBSERVE' | 'MANUAL' | 'AUTONOMOUS_PAPER';

/**
 * Machine-readable reason codes (Hard Rule A4).
 *
 * The code is the source of truth. Every sentence a user reads about why a
 * decision went the way it did is a rendering of one of these, never the
 * other way round, and never an LLM paraphrase.
 */
export type ReasonCode =
  | 'TRADE_ALLOCATION_LIMIT'
  | 'CONCENTRATION_LIMIT'
  | 'VOLATILITY_CEILING'
  | 'DRAWDOWN_FLOOR'
  | 'LIQUIDITY_FLOOR'
  | 'CONFIDENCE_FLOOR'
  | 'CORRELATED_EXPOSURE'
  | 'DEFINED_LOSS_LIMIT'
  | 'BUYING_POWER_LIMIT'
  | 'CONTRACT_COUNT_LIMIT'
  | 'MIN_DTE'
  | 'STRUCTURE_NOT_ALLOWED'
  | 'AGGREGATE_SECTOR_GUIDANCE'
  | 'AUTHORIZATION_EXPIRED'
  | 'BOUND_STATE_CHANGED'
  | 'REAUTHORIZATION_REQUIRED'
  | 'KILL_SWITCH_ACTIVE'
  | 'MARKET_CLOSED'
  | (string & {});

/* ----------------------------------------------------------------- pipeline */

/** The eight stages, upstream intelligence first. */
export type PipelineStageId =
  | 'research'
  | 'selection'
  | 'probability'
  | 'trader'
  | 'hard_risk'
  | 'ai_risk'
  | 'governor'
  | 'execution';

export type StageState =
  | 'IDLE'
  | 'RUNNING'
  | 'PASSED'
  | 'WATCH'
  | 'REDUCED'
  | 'BLOCKED'
  | 'ERROR'
  | 'COMPLETE';

/** Which side of the governance boundary a stage sits on. */
export type PipelineLayer = 'intelligence' | 'governance';

export interface PipelineStage {
  id: PipelineStageId;
  label: string;
  /** Short operator-facing name for the actor that owns this stage. */
  actor: string;
  layer: PipelineLayer;
  state: StageState;
  /** One line describing what happened here, shown on selection. */
  detail?: string;
  /** Quantity leaving this stage, when the stage changes size. */
  quantityOut?: number;
  latencyMs?: number;
  at?: string;
  provenance?: Provenance;
}

/* ---------------------------------------------------------------- proposals */

export interface EquityInstrument {
  type: 'equity';
  symbol: string;
  side: Side;
  quantity: number;
  estimatedPrice: number;
}

export type OptionStrategy =
  | 'LONG_CALL'
  | 'LONG_PUT'
  | 'VERTICAL_DEBIT_SPREAD'
  | 'VERTICAL_CREDIT_SPREAD'
  | 'IRON_CONDOR';

export interface OptionLeg {
  optionSymbol: string;
  side: Side;
  optionType: 'CALL' | 'PUT';
  strike: number;
  expiry: string;
  ratio: number;
}

export interface OptionInstrument {
  type: 'option';
  underlying: string;
  strategy: OptionStrategy;
  /** Contracts. */
  quantity: number;
  expiry: string;
  daysToExpiry: number;
  legs: OptionLeg[];
  contractMultiplier: number;
  /** Signed, per share. Positive = net debit paid. */
  netPremiumPerUnit: number | null;
  maxDefinedLoss: number | null;
  /** `null` means the profit is unbounded, which is a fact, not a gap. */
  maxProfit: number | null;
  /** False when the backend did not expose a maximum-profit amount. */
  maxProfitKnown?: boolean;
}

export type Instrument = EquityInstrument | OptionInstrument;

export type CheckUnit = 'ratio' | 'currency' | 'count' | 'days';

/**
 * The arithmetic a deterministic check actually performed.
 *
 * `actualIfRequested` and `actualIfAuthorized` are what make the visual "Why?"
 * panel possible (MIZAN-UX-SPEC §3): the panel draws these three values against
 * `threshold` and lets a reader see the bar cross the line. Without them the
 * only available explanation is prose, which is a paraphrase of enforcement
 * rather than the enforcement itself.
 */
export interface CheckMeasurement {
  unit: CheckUnit;
  /** Whether the threshold is an upper bound or a lower one. */
  bound: 'ceiling' | 'floor';
  threshold: Decimal;
  /** Portfolio state before this trade. `null` where the check is not stateful. */
  actualCurrent: Decimal | null;
  actualIfRequested: Decimal;
  /** `null` when nothing was authorized, i.e. a rejection. */
  actualIfAuthorized: Decimal | null;
  /**
   * True where the measured quantity does not move with order size — symbol
   * volatility, for instance. A failing size-invariant check cannot be reduced
   * into compliance, which is why it produces REJECT rather than REDUCE.
   */
  sizeInvariant: boolean;
}

export interface RiskCheck {
  /** Stable check identifier. Matches the engine's rule name. */
  rule: string;
  passed: boolean;
  severity: RiskSeverity;
  /** Human label for the limit, e.g. "Portfolio concentration". */
  label: string;
  /** The code this check emits when it fails. `null` when it cannot fail. */
  reasonCode: ReasonCode | null;
  /** Present wherever the check compares a number against a limit. */
  measurement: CheckMeasurement | null;
  /** A rendering of the code and the measurement, never the source of truth. */
  message: string;
  recommendedQuantity?: number | null;
}

export interface HardRiskReport {
  originalQuantity: number;
  recommendedQuantity: number;
  blocked: boolean;
  riskScore: number | null;
  reasons: string[];
  checks: RiskCheck[];
  provenance: Provenance;
}

/**
 * Historical reliability of an agent's self-reported confidence.
 *
 * Self-reported confidence is an estimate with unknown error (MIZAN-UX-SPEC §1,
 * Correction 3). Where a calibration record exists it is shown beside the raw
 * number; where it does not, the absence is stated rather than filled in.
 */
export interface Calibration {
  /** Decisions the calibration was computed over. */
  sampleSize: number;
  /** Mean over/under-confidence in percentage points. Positive = overconfident. */
  meanErrorPoints: Decimal;
  /** The raw confidence after the historical haircut. */
  calibratedConfidence: Decimal;
  provenance: Provenance;
}

export interface AiRiskAnalysis {
  recommendation: Decision;
  /** Agent-reported. Never rendered as though it were a calibrated probability. */
  confidence: number | null;
  /** `null` means no calibration record exists. It is never invented. */
  calibration: Calibration | null;
  recommendedQuantity: number;
  riskThesis: string;
  hiddenRisks: string[];
  reasoning: string[];
  /** Product-facing model label. The infrastructure provider is not exposed. */
  modelLabel: string;
  provenance: Provenance;
}

export interface OutcomeLeg {
  quantity: number;
  /** `null` when the backend did not calculate a notional. Never coerced to zero. */
  notional: Decimal | null;
}

/**
 * Requested → Authorized → Executed.
 *
 * The signature element (MIZAN-UX-SPEC §2). The gap between the first column
 * and the second is the product; it is never collapsed, and the requested
 * figure is never replaced by the authorized one.
 */
export interface DecisionOutcome {
  requested: OutcomeLeg;
  /** `null` before a verdict. A rejection authorizes zero, which is not null. */
  authorized: OutcomeLeg | null;
  /** `null` until the broker reports. */
  executed: OutcomeLeg | null;
  /** requested − authorized, computed by the backend so the UI never subtracts money. */
  preventedNotional: Decimal | null;
  unit: 'shares' | 'contracts';
}

export type AuthorizationStatus = 'ACTIVE' | 'USED' | 'EXPIRED' | 'INVALIDATED' | 'NOT_ISSUED';

/**
 * A permission with a lifetime and a binding.
 *
 * An authorization is valid for a state of the world, not for a period of time
 * alone: if the portfolio or market state it was bound to changes, it is
 * invalid before it expires (MIZAN-UX-SPEC §5.2).
 */
export interface Authorization {
  id: string;
  issuedAt: string;
  expiresAt: string;
  ttlSeconds: number;
  /** Hash of the portfolio state the permission was issued against. */
  boundPortfolioState: string;
  /** Hash of the market state the permission was issued against. */
  boundMarketState: string;
  usedAt: string | null;
  status: AuthorizationStatus;
  invalidatedAt: string | null;
  invalidationCode: ReasonCode | null;
  /** What changed, in the engine's own terms. */
  invalidationDetail: string | null;
}

/** Position of a record in the immutable hash chain. */
export interface ChainStamp {
  verified: boolean;
  position: number;
  recordHash: string;
  previousHash: string;
  verifiedAt: string;
  /** Time taken to re-verify from genesis. `null` if never re-verified. */
  verifyMs: number | null;
}

export interface GovernorResult {
  decision: Decision;
  originalQuantity: number;
  approvedQuantity: number;
  reason: string;
  riskScore: number | null;
  decidedAt: string;
  /** Which stage was ultimately responsible for the size change. */
  bindingConstraint: string | null;
}

export type ExecutionState =
  | 'EXECUTION_DISABLED'
  | 'BLOCKED'
  | 'AUTHORIZED'
  | 'STALE_AUTHORIZATION'
  | 'KILL_SWITCH_ACTIVE'
  | 'MARKET_CLOSED'
  | 'ASSET_NOT_TRADABLE'
  | 'REAUTHORIZATION_REQUIRED'
  | 'WOULD_SUBMIT'
  | 'RECONCILED_EXISTING_ORDER'
  | 'SUBMITTED'
  | 'FAILED'
  | 'NOT_REACHED';

export interface ExecutionReadiness {
  state: ExecutionState;
  message: string;
  /** Gate-by-gate result, in the order the execution service runs them. */
  gates: {
    id: string;
    label: string;
    passed: boolean | null;
    detail: string;
  }[];
  clientOrderId?: string;
  dryRun: boolean;
}

export interface MarketRiskInput {
  annualizedVolatility: number;
  maxDrawdown30d: number;
  liquidityScore: number;
  provenance: Provenance;
}

export interface Proposal {
  proposalId: string;
  instrument: Instrument;
  instrumentType: InstrumentType;
  strategyConfidence: number | null;
  thesis: string;
  invalidationCondition: string;
  researchSummary: string;
  sourceAgents: string[];
  createdAt: string;
  provenance: Provenance;
  marketRisk: MarketRiskInput | null;
  stage: PipelineStageId;
  stages: PipelineStage[];
  /** L1. What was asked for, what policy allowed, what reached the broker. */
  outcome: DecisionOutcome;
  /** L1/L2. The codes that produced the verdict, most binding first. */
  reasonCodes: ReasonCode[];
  hardRisk: HardRiskReport | null;
  aiRisk: AiRiskAnalysis | null;
  governor: GovernorResult | null;
  authorization: Authorization | null;
  execution: ExecutionReadiness | null;
  chain: ChainStamp | null;
  linkedOrderId: string | null;
}

/* ---------------------------------------------------------------- portfolio */

export interface Position {
  symbol: string;
  assetClass: 'us_equity' | 'us_option';
  side: 'LONG' | 'SHORT';
  quantity: number;
  marketValue: number;
  costBasis: number | null;
  currentPrice: number | null;
  unrealizedPl: number | null;
  unrealizedPlPct: number | null;
  /** Share of total equity. */
  weight: number | null;
  /** Capital genuinely at risk. For defined-risk options this is max loss. */
  riskContribution: number | null;
  riskBasis: 'MARKET_VALUE' | 'DEFINED_MAX_LOSS' | 'UNAVAILABLE';
  sourceStrategy: string;
  provenance: Provenance;
}

export interface PortfolioSummary {
  equity: Sourced<number>;
  cash: Sourced<number>;
  buyingPower: Sourced<number>;
  dailyPnl: Sourced<number>;
  dailyPnlPct: Sourced<number>;
  realizedPnl: Sourced<number>;
  unrealizedPnl: Sourced<number>;
  capitalAtRisk: Sourced<number>;
  source: 'ALPACA_PAPER' | 'MANUAL';
}

/* --------------------------------------------------------------------- risk */

export interface PolicyLimit {
  id: string;
  group: 'equity' | 'options' | 'portfolio';
  label: string;
  description: string;
  limitDisplay: string;
  /** 0–1 utilisation, or null when the backend cannot compute it. */
  utilisation: number | null;
  currentDisplay: string;
  status: 'OK' | 'WATCH' | 'BREACH' | 'UNAVAILABLE';
  provenance: Provenance;
}

export type InterventionKind =
  | 'REDUCE'
  | 'REJECT'
  | 'BLOCK'
  | 'REAUTHORIZE'
  | 'MARKET_CLOSED'
  | 'STALE_AUTHORIZATION';

export interface Intervention {
  id: string;
  at: string;
  proposalId: string;
  symbol: string;
  kind: InterventionKind;
  rule: string;
  before: string;
  after: string;
  actor: string;
}

export interface RiskAlert {
  id: string;
  severity: RiskSeverity;
  kind: string;
  symbol: string;
  proposalId: string | null;
  reason: string;
  policy: string;
  at: string;
}

export interface SafetyControls {
  paperOnly: true;
  executionEnabled: boolean;
  dryRun: boolean;
  killSwitch: boolean;
  autonomy: AutonomyMode;
  maxDecisionAgeSeconds: number | null;
}

/* -------------------------------------------------------- graduated response */

/**
 * The response ladder (MIZAN-UX-SPEC §5.3).
 *
 * A binary kill switch cannot express "tighten but keep trading". Level 0 is
 * normal operation; level 5 is a full stop. The level is global state, shown in
 * the chrome on every screen, and renders as state rather than as a button.
 *
 * Level names are provisional pending MIZAN-RISK-CANON, which is not in this
 * worktree.
 */
export type ResponseLevel = 0 | 1 | 2 | 3 | 4 | 5;

export interface ResponseState {
  level: ResponseLevel;
  /** Engaged at this level since. */
  since: string;
  /** Who engaged it, when a person did. `null` for automatic escalation. */
  engagedBy: string | null;
  agentsHalted: number;
  agentsActive: number;
  note: string;
}

/* ---------------------------------------------------------- quiet-state home */

/**
 * The boring day, as proof of value (MIZAN-UX-SPEC §5.5).
 *
 * Ninety-nine percent of the time nothing is wrong. `preventedNotional` is what
 * converts "nothing happened" into "here is what we stopped".
 */
export interface GovernanceDay {
  /** Session date, UTC. */
  date: string;
  decisionsGoverned: number;
  approved: number;
  reduced: number;
  rejected: number;
  requestedNotional: Decimal | null;
  authorizedNotional: Decimal | null;
  preventedNotional: Decimal | null;
  chainVerified: number;
  chainTotal: number;
  /** Items genuinely awaiting a person. Zero is the expected value. */
  needsAttention: number;
  provenance: Provenance;
}

/* ------------------------------------------------------------ agent crowding */

/**
 * Aggregate exposure across agents (MIZAN-UX-SPEC §5.1).
 *
 * The failure this screen exists to show: every agent is inside its own limits
 * while the book as a whole is not.
 */
export interface CrowdingMember {
  agentId: string;
  symbol: string;
  /** Share of portfolio equity held by this agent in this symbol. */
  weight: Decimal;
  /** Whether this agent is inside its own per-agent limits. */
  withinIndividualLimits: boolean;
}

export interface CrowdingCluster {
  id: string;
  label: string;
  /** Combined share of equity. */
  exposure: Decimal;
  /**
   * The portfolio-level guidance this aggregate is measured against.
   *
   * Guidance, not a hard limit: no single deterministic check fails on it,
   * which is the entire point of the screen.
   */
  guidance: Decimal;
  breached: boolean;
  members: CrowdingMember[];
  /** Where the cluster lands if the proposals in flight are authorized. */
  projected: { label: string; exposure: Decimal } | null;
}

export interface ConcentrationFact {
  label: string;
  share: Decimal;
  detail: string;
}

export interface CrowdingReport {
  status: 'NORMAL' | 'ELEVATED' | 'BREACH' | 'UNAVAILABLE';
  agentsTotal: number;
  agentsCorrelated: number;
  clusters: CrowdingCluster[];
  /** Share of exposure originating from a single model provider. */
  modelConcentration: ConcentrationFact | null;
  /** Share of exposure originating from a single data source. */
  signalConcentration: ConcentrationFact | null;
  /** Days to unwind the correlated book at normal volume. `null` if not modelled. */
  unwindDays: Decimal | null;
  provenance: Provenance;
}

/* ------------------------------------------------------------------- orders */

export type OrderLifecycleState =
  | 'DRY_RUN'
  | 'WOULD_SUBMIT'
  | 'SUBMITTED'
  | 'NEW'
  | 'PARTIALLY_FILLED'
  | 'FILLED'
  | 'REJECTED'
  | 'CANCELED'
  | 'EXPIRED'
  | 'PENDING'
  | 'UNKNOWN';

export interface OrderLifecycleEvent {
  at: string;
  label: string;
  detail: string;
  state: 'done' | 'current' | 'pending' | 'failed';
  actor: string;
}

export interface Order {
  clientOrderId: string;
  brokerOrderId: string | null;
  proposalId: string;
  symbol: string;
  underlying: string | null;
  assetClass: 'us_equity' | 'us_option';
  orderClass: 'simple' | 'mleg';
  side: Side;
  proposedQuantity: number;
  approvedQuantity: number;
  filledQuantity: number;
  filledAvgPrice: number | null;
  lifecycle: OrderLifecycleState;
  brokerStatus: string | null;
  submittedAt: string | null;
  updatedAt: string;
  executionMode: 'ALPACA_PAPER' | 'ALPACA_PAPER_DRY_RUN';
  timeline: OrderLifecycleEvent[];
  provenance: Provenance;
}

/* -------------------------------------------------------------------- audit */

export type AuditActor =
  | 'research_agent'
  | 'selection_agent'
  | 'probability_agent'
  | 'trader_agent'
  | 'risk_engine'
  | 'ai_risk_agent'
  | 'governor'
  | 'execution'
  | 'broker';

export interface AuditEvent {
  eventId: string;
  proposalId: string;
  orderId: string | null;
  actor: AuditActor;
  stage: PipelineStageId | 'broker';
  action: string;
  summary: string;
  at: string;
  outcome: 'ok' | 'watch' | 'blocked' | 'info';
  symbol: string;
  /** Sanitised structured payload. Credentials never appear here. */
  payload: Record<string, unknown>;
  provenance: Provenance;
}

/* ------------------------------------------------------------------- agents */

export type AgentStatus = 'ACTIVE' | 'IDLE' | 'ERROR' | 'NOT_CONFIGURED';

export interface AgentCardModel {
  id: PipelineStageId;
  name: string;
  layer: PipelineLayer;
  role: string;
  status: AgentStatus;
  currentAction: string | null;
  lastAction: string;
  lastActionAt: string;
  latencyMs: number | null;
  currentProposalId: string | null;
  /** Confidence for intelligence agents, risk score for governance stages. */
  metric: { label: string; value: string } | null;
  recentEvents: { at: string; text: string }[];
  provenance: Provenance;
}

/* ------------------------------------------------------------- connections */

export type ConnectionStatus = 'CONNECTED' | 'NOT_CONFIGURED' | 'DEGRADED' | 'ERROR';

export interface ConnectionState {
  id: string;
  label: string;
  description: string;
  status: ConnectionStatus;
  detail: string;
}

export interface SystemHealth {
  backendVersion: string;
  database: ConnectionStatus;
  auditStorage: ConnectionStatus;
  broker: ConnectionStatus;
  marketOpen: boolean | null;
  nextOpen: string | null;
  nextClose: string | null;
}

/* ------------------------------------------------------------ notifications */

export interface AppNotification {
  id: string;
  at: string;
  severity: 'info' | 'ok' | 'warn' | 'danger';
  title: string;
  body: string;
  href?: string;
  read: boolean;
}
