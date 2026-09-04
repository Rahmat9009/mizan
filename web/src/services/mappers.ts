import { STAGE_META, STAGE_ORDER, stage } from '@/data/pipeline';
import { decimalCompare } from '@/lib/decimal';
import { humanise } from '@/lib/format';
import type {
  AgentCardModel,
  AppNotification,
  AuditEvent,
  Authorization,
  ChainStamp,
  ConnectionState,
  CrowdingReport,
  CheckUnit,
  GovernanceDay,
  Intervention,
  Order,
  OrderLifecycleState,
  PipelineStage,
  PolicyLimit,
  PortfolioSummary,
  Position,
  Proposal,
  ReasonCode,
  ResponseState,
  RiskAlert,
  RiskCheck,
  RiskSeverity,
  SafetyControls,
  SystemHealth,
  Provenance,
} from '@/types/domain';
import type {
  BackendAdvisory,
  BackendChainVerification,
  BackendCheck,
  BackendDecisionRecord,
  BackendExecution,
  BackendHealth,
  BackendPolicy,
  BackendPortfolioSnapshot,
} from './backendTypes';

const LIVE_RECORD = 'MIZAN_LEDGER' as const;
const UNAVAILABLE = 'Not supplied by the Mizan API.';

/** Convert a decimal only for UI geometry/counts. Exact money stays a string. */
function displayNumber(value: string | null | undefined): number {
  if (value === null || value === undefined) return 0;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function decimalParts(value: string): { negative: boolean; digits: bigint; scale: number } {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) throw new Error(`Invalid backend decimal: ${value}`);
  const fraction = match[3] ?? '';
  return {
    negative: match[1] === '-',
    digits: BigInt(`${match[2]}${fraction}`),
    scale: fraction.length,
  };
}

function decimalAdd(left: string, right: string): string {
  const a = decimalParts(left);
  const b = decimalParts(right);
  const scale = Math.max(a.scale, b.scale);
  const signed = (part: typeof a) =>
    (part.negative ? -part.digits : part.digits) * 10n ** BigInt(scale - part.scale);
  const total = signed(a) + signed(b);
  const negative = total < 0n;
  const digits = (negative ? -total : total).toString().padStart(scale + 1, '0');
  if (scale === 0) return `${negative ? '-' : ''}${digits}`;
  const integer = digits.slice(0, -scale);
  const fraction = digits.slice(-scale).replace(/0+$/, '');
  return `${negative ? '-' : ''}${integer}${fraction ? `.${fraction}` : ''}`;
}

function decimalSubtract(left: string, right: string): string {
  return decimalAdd(left, right.startsWith('-') ? right.slice(1) : `-${right}`);
}

function decimalSum(values: (string | null)[]): string | null {
  if (values.some((value) => value === null)) return null;
  return (values as string[]).reduce(decimalAdd, '0');
}

function sourceOf(source: string | null | undefined) {
  return source?.includes('alpaca:paper') ? ('ALPACA_PAPER' as const) : LIVE_RECORD;
}

function reasonCodes(codes: string[]): ReasonCode[] {
  return codes as ReasonCode[];
}

function optionSymbol(symbol: string, type: 'call' | 'put', expiry: string, strike: string): string {
  const date = expiry.replaceAll('-', '').slice(2);
  const parts = decimalParts(strike);
  const scaled = parts.digits * 10n ** BigInt(Math.max(0, 3 - parts.scale));
  const normalized = parts.scale > 3 ? scaled / 10n ** BigInt(parts.scale - 3) : scaled;
  return `${symbol}${date}${type === 'call' ? 'C' : 'P'}${normalized.toString().padStart(8, '0')}`;
}

function optionStrategy(strategy: string):
  | 'LONG_CALL'
  | 'LONG_PUT'
  | 'VERTICAL_DEBIT_SPREAD'
  | 'VERTICAL_CREDIT_SPREAD'
  | 'IRON_CONDOR' {
  const mapped: Record<string, 'LONG_CALL' | 'LONG_PUT' | 'VERTICAL_DEBIT_SPREAD' | 'VERTICAL_CREDIT_SPREAD' | 'IRON_CONDOR'> = {
    long_call: 'LONG_CALL',
    long_put: 'LONG_PUT',
    bull_call_spread: 'VERTICAL_DEBIT_SPREAD',
    bear_put_spread: 'VERTICAL_DEBIT_SPREAD',
    bull_put_spread: 'VERTICAL_CREDIT_SPREAD',
    bear_call_spread: 'VERTICAL_CREDIT_SPREAD',
    iron_condor: 'IRON_CONDOR',
    custom: 'VERTICAL_DEBIT_SPREAD',
  };
  return mapped[strategy] ?? 'VERTICAL_DEBIT_SPREAD';
}

function severityOf(check: BackendCheck): RiskSeverity {
  if (check.passed) return check.severity === 'warning' ? 'WATCH' : 'INFO';
  if (check.severity === 'blocking') return 'BLOCK';
  if (check.severity === 'warning') return 'HIGH';
  return 'WATCH';
}

function unitOf(checkId: string): CheckUnit {
  if (checkId.includes('days') || checkId.includes('expiry')) return 'days';
  if (checkId.includes('quantity') || checkId.includes('count') || checkId.includes('leg')) return 'count';
  if (checkId.includes('notional') || checkId.includes('capital') || checkId.includes('buying_power')) return 'currency';
  return 'ratio';
}

function boundOf(checkId: string): 'ceiling' | 'floor' {
  return checkId.includes('minimum') || checkId.includes('min_') || checkId.includes('sufficiency') ? 'floor' : 'ceiling';
}

function mapCheck(check: BackendCheck): RiskCheck {
  return {
    rule: check.check_id,
    passed: check.passed,
    severity: severityOf(check),
    label: humanise(check.check_id),
    reasonCode: check.reason_code as ReasonCode | null,
    measurement:
      check.threshold !== null && check.actual !== null
        ? {
            unit: unitOf(check.check_id),
            bound: boundOf(check.check_id),
            threshold: check.threshold,
            actualCurrent: null,
            actualIfRequested: check.actual,
            // The backend reports the requested-state measurement only. A reduced-state value is not invented.
            actualIfAuthorized: null,
            sizeInvariant: check.recommended_quantity === null,
          }
        : null,
    message: check.detail || (check.passed ? 'Check passed.' : `Check failed: ${check.reason_code ?? check.check_id}.`),
    recommendedQuantity:
      check.recommended_quantity === null ? null : displayNumber(check.recommended_quantity),
  };
}

function mapAuthorization(record: BackendDecisionRecord, now: number): Authorization | null {
  const auth = record.authorization as BackendDecisionRecord['authorization'] & {
    issued_at?: string;
    bound_state?: { portfolio_state_hash: string; market_snapshot_id: string };
  };
  if (!auth) return null;
  const execution = record.execution;
  const expired = Date.parse(auth.expires_at) <= now;
  const invalidated = execution?.status === 'BLOCKED' || execution?.status === 'FAILED';
  const used = execution !== null && ['SUBMITTED', 'WOULD_SUBMIT', 'RECONCILED_EXISTING'].includes(execution.status);
  return {
    id: auth.auth_id,
    issuedAt: auth.issued_at ?? record.decision_timestamp,
    expiresAt: auth.expires_at,
    ttlSeconds: auth.ttl_seconds,
    boundPortfolioState: auth.bound_state?.portfolio_state_hash ?? 'Unavailable',
    boundMarketState: auth.bound_state?.market_snapshot_id ?? 'Unavailable',
    usedAt: used ? execution?.submitted_at ?? execution?.checked_at ?? null : null,
    status: used ? 'USED' : invalidated ? 'INVALIDATED' : expired ? 'EXPIRED' : 'ACTIVE',
    invalidatedAt: invalidated ? execution?.checked_at ?? null : null,
    invalidationCode: invalidated ? (execution?.reason_codes[0] as ReasonCode | undefined) ?? null : null,
    invalidationDetail: invalidated ? execution?.message ?? null : null,
  };
}

function mapExecution(record: BackendDecisionRecord, authorization: Authorization | null): Proposal['execution'] {
  const execution = record.execution;
  if (!execution) {
    if (!authorization) {
      return {
        state: 'NOT_REACHED',
        message: 'No authorization was issued, so the execution gate was never entered.',
        dryRun: false,
        gates: [{ id: 'authorization', label: 'Authorization present', passed: false, detail: 'No authorization was issued.' }],
      };
    }
    return {
      state: authorization.status === 'EXPIRED' ? 'STALE_AUTHORIZATION' : 'AUTHORIZED',
      message:
        authorization.status === 'EXPIRED'
          ? 'The recorded authorization has expired. No execution result is stored.'
          : 'Authorization is active. No execution result is stored.',
      dryRun: false,
      gates: [
        { id: 'authorization', label: 'Authorization present', passed: true, detail: `Authorization ${authorization.id}.` },
        {
          id: 'freshness',
          label: 'Decision freshness',
          passed: authorization.status === 'EXPIRED' ? false : true,
          detail: `Expires ${authorization.expiresAt}.`,
        },
        { id: 'execution', label: 'Execution attempted', passed: null, detail: 'No execution result is stored.' },
      ],
    };
  }
  const state = executionState(execution);
  return {
    state,
    message: execution.message,
    dryRun: execution.status === 'WOULD_SUBMIT',
    ...(execution.client_order_id ? { clientOrderId: execution.client_order_id } : {}),
    gates: [
      {
        id: 'authorization',
        label: 'Authorization validated',
        passed: execution.authorization_validated_at !== null,
        detail: execution.authorization_validated_at ?? 'Not validated.',
      },
      {
        id: 'kill_switch',
        label: 'Kill switch checked',
        passed: execution.kill_switch_checked_at !== null ? !execution.reason_codes.includes('KILL_SWITCH_ACTIVE') : null,
        detail: execution.kill_switch_checked_at ?? 'Not reached.',
      },
      {
        id: 'revalidation',
        label: 'Bound state revalidated',
        passed: execution.revalidation.performed ? execution.revalidation.supported : null,
        detail: execution.revalidation.performed ? (execution.revalidation.supported ? 'Fresh state supported the authorization.' : 'Fresh state did not support the authorization.') : 'Not reached.',
      },
    ],
  };
}

function executionState(execution: BackendExecution): NonNullable<Proposal['execution']>['state'] {
  if (execution.reason_codes.includes('KILL_SWITCH_ACTIVE')) return 'KILL_SWITCH_ACTIVE';
  if (execution.reason_codes.includes('AUTHORIZATION_EXPIRED')) return 'STALE_AUTHORIZATION';
  if (execution.reason_codes.includes('AUTHORIZATION_STATE_MISMATCH')) return 'REAUTHORIZATION_REQUIRED';
  if (execution.status === 'RECONCILED_EXISTING') return 'RECONCILED_EXISTING_ORDER';
  return execution.status;
}

function mapStages(record: BackendDecisionRecord): PipelineStage[] {
  const q = displayNumber(record.original.total_quantity);
  const authorized = displayNumber(record.authorized.total_quantity);
  const advisory = record.llm_advisory;
  const execution = record.execution;
  return [
    stage('research', 'COMPLETE', { detail: 'Upstream evidence is represented by the recorded signal sources.', at: record.proposal.created_at, provenance: 'LIVE_AGENT' }),
    stage('selection', 'COMPLETE', { detail: `${record.proposal.symbol} selected by the upstream agent.`, at: record.proposal.created_at, provenance: 'LIVE_AGENT' }),
    stage('probability', 'COMPLETE', { detail: record.proposal.confidence === null ? 'No confidence claim was supplied.' : `Agent confidence ${record.proposal.confidence}.`, at: record.proposal.created_at, provenance: 'LIVE_AGENT' }),
    stage('trader', 'COMPLETE', { detail: `TradeProposal recorded for ${q} units.`, quantityOut: q, at: record.proposal.created_at, provenance: 'LIVE_AGENT' }),
    stage('hard_risk', record.risk_evaluation.verdict === 'REJECT' ? 'BLOCKED' : record.risk_evaluation.verdict === 'REDUCE' ? 'REDUCED' : 'PASSED', { detail: `${record.risk_evaluation.checks.length} deterministic checks; ${record.risk_evaluation.verdict}.`, quantityOut: displayNumber(record.risk_evaluation.recommended_quantity), at: record.risk_evaluation.evaluated_at, provenance: sourceOf(record.risk_context.portfolio_snapshot?.source) }),
    stage('ai_risk', advisory?.invoked ? (advisory.available ? (advisory.recommendation === 'REJECT' ? 'BLOCKED' : advisory.recommendation === 'REDUCE' ? 'REDUCED' : 'PASSED') : 'ERROR') : 'IDLE', { detail: advisory?.invoked ? (advisory.available ? advisory.reasoning || 'Advisory completed.' : 'Advisory was unavailable.') : 'Advisory was not invoked.', at: record.decision_timestamp, provenance: 'AI_RISK_MODEL' }),
    stage('governor', record.verdict === 'REJECT' ? 'BLOCKED' : record.verdict === 'REDUCE' ? 'REDUCED' : 'PASSED', { detail: `${record.verdict}: ${authorized} of ${q} authorized.`, quantityOut: authorized, at: record.decision_timestamp, provenance: LIVE_RECORD }),
    stage('execution', execution ? (execution.status === 'BLOCKED' || execution.status === 'FAILED' ? 'BLOCKED' : 'COMPLETE') : 'IDLE', { detail: execution?.message ?? 'No execution result is stored.', at: execution?.checked_at, provenance: 'ALPACA_PAPER' }),
  ];
}

function latestStage(record: BackendDecisionRecord): Proposal['stage'] {
  return record.execution ? 'execution' : 'governor';
}

export function mapDecision(record: BackendDecisionRecord, chain: BackendChainVerification, now = Date.now()): Proposal {
  const proposal = record.proposal;
  const authorization = mapAuthorization(record, now);
  const requestedNotional = record.original.total_notional;
  const authorizedNotional = record.authorized.total_notional;
  const prevented =
    requestedNotional !== null && authorizedNotional !== null
      ? decimalSubtract(requestedNotional, authorizedNotional)
      : null;
  const executedNotional = record.execution ? executionNotional(record.execution) : null;
  const executedQuantity = record.execution ? decimalSum(record.execution.fills.map((fill) => fill.filled_quantity)) : null;
  const firstLeg = proposal.legs[0];
  const instrument: Proposal['instrument'] =
    proposal.asset_class === 'equity'
      ? {
          type: 'equity',
          symbol: proposal.symbol,
          side: firstLeg.side.toUpperCase() as 'BUY' | 'SELL',
          quantity: displayNumber(record.original.total_quantity),
          estimatedPrice: displayNumber(firstLeg.limit_price),
        }
      : {
          type: 'option',
          underlying: proposal.symbol,
          strategy: optionStrategy(proposal.strategy),
          quantity: displayNumber(record.original.total_quantity),
          expiry: firstLeg.expiry ?? 'Unavailable',
          daysToExpiry: firstLeg.expiry ? Math.max(0, Math.ceil((Date.parse(firstLeg.expiry) - Date.parse(proposal.created_at)) / 86_400_000)) : 0,
          contractMultiplier: 100,
          netPremiumPerUnit: null,
          maxDefinedLoss: null,
          maxProfit: null,
          maxProfitKnown: false,
          legs: proposal.legs.map((leg) => ({
            optionSymbol: optionSymbol(proposal.symbol, leg.contract_type!, leg.expiry!, leg.strike!),
            side: leg.side.toUpperCase() as 'BUY' | 'SELL',
            optionType: leg.contract_type!.toUpperCase() as 'CALL' | 'PUT',
            strike: displayNumber(leg.strike),
            expiry: leg.expiry!,
            ratio: displayNumber(leg.quantity),
          })),
        };
  const failed = record.risk_evaluation.checks.filter((check) => !check.passed);
  const binding = failed[0]?.check_id ?? record.authorized.reductions[0]?.source ?? null;
  const advisory = mapAdvisory(record.llm_advisory, record);
  return {
    // One proposal can be evaluated more than once. The decision id is the
    // unique case-file key; the raw proposal id remains in the audit payload.
    proposalId: record.decision_id,
    instrument,
    instrumentType: proposal.asset_class === 'equity' ? 'equity' : 'option',
    strategyConfidence: proposal.confidence === null ? null : displayNumber(proposal.confidence),
    thesis: proposal.reasoning || 'No free-text thesis was supplied.',
    invalidationCondition: proposal.invalidation
      ? `${proposal.invalidation.direction} ${proposal.invalidation.level}${proposal.invalidation.target ? `; target ${proposal.invalidation.target}` : ''}`
      : 'Unavailable — no invalidation condition was supplied.',
    researchSummary: proposal.signal_sources.length
      ? `Recorded signal sources: ${proposal.signal_sources.join(', ')}.`
      : 'Unavailable — no signal sources were recorded.',
    sourceAgents: [proposal.agent.agent_id],
    createdAt: proposal.created_at,
    provenance: 'LIVE_AGENT',
    marketRisk: null,
    stage: latestStage(record),
    stages: mapStages(record),
    outcome: {
      requested: { quantity: displayNumber(record.original.total_quantity), notional: requestedNotional },
      authorized: { quantity: displayNumber(record.authorized.total_quantity), notional: authorizedNotional },
      executed:
        executedNotional !== null && executedQuantity !== null
          ? { quantity: displayNumber(executedQuantity), notional: executedNotional }
          : null,
      preventedNotional: prevented,
      unit: proposal.asset_class === 'equity_option' ? 'contracts' : 'shares',
    },
    reasonCodes: reasonCodes(record.reason_codes),
    hardRisk: {
      originalQuantity: displayNumber(record.risk_evaluation.original_quantity),
      recommendedQuantity: displayNumber(record.risk_evaluation.recommended_quantity),
      blocked: record.risk_evaluation.verdict === 'REJECT',
      riskScore: null,
      reasons: failed.map((check) => check.detail || check.reason_code || check.check_id),
      checks: record.risk_evaluation.checks.map(mapCheck),
      provenance: sourceOf(record.risk_context.portfolio_snapshot?.source),
    },
    aiRisk: advisory,
    governor: {
      decision: record.verdict,
      originalQuantity: displayNumber(record.original.total_quantity),
      approvedQuantity: displayNumber(record.authorized.total_quantity),
      reason: failed[0]?.detail || (record.reason_codes.length ? record.reason_codes.join(', ') : 'Authorized as requested.'),
      riskScore: null,
      decidedAt: record.decision_timestamp,
      bindingConstraint: binding,
    },
    authorization,
    execution: mapExecution(record, authorization),
    chain: mapChain(record, chain),
    linkedOrderId: record.execution?.client_order_id ?? null,
  };
}

function mapAdvisory(advisory: BackendAdvisory | null, record: BackendDecisionRecord): Proposal['aiRisk'] {
  if (!advisory?.invoked || !advisory.available || !advisory.recommendation) return null;
  return {
    recommendation: advisory.recommendation === 'CONCUR' ? 'APPROVE' : advisory.recommendation,
    confidence: null,
    calibration: null,
    recommendedQuantity: displayNumber(advisory.recommended_quantity ?? record.authorized.total_quantity),
    riskThesis: advisory.reasoning || 'No advisory reasoning was supplied.',
    hiddenRisks: [],
    reasoning: advisory.reasoning ? [advisory.reasoning] : [],
    modelLabel: advisory.profile,
    provenance: 'AI_RISK_MODEL',
  };
}

function mapChain(record: BackendDecisionRecord, chain: BackendChainVerification): ChainStamp {
  const verifiedAt = new Date().toISOString();
  return {
    verified: chain.ok && record.sequence <= chain.length,
    position: record.sequence,
    recordHash: record.audit_hash,
    previousHash: record.audit_prev_hash,
    verifiedAt,
    verifyMs: null,
  };
}

function executionNotional(execution: BackendExecution): string | null {
  if (!execution.fills.length) return null;
  return execution.fills.reduce(
    (sum, fill) => decimalAdd(sum, decimalMultiply(fill.filled_quantity, fill.avg_price)),
    '0',
  );
}

function decimalMultiply(left: string, right: string): string {
  const a = decimalParts(left);
  const b = decimalParts(right);
  const negative = a.negative !== b.negative;
  const scale = a.scale + b.scale;
  const digits = (a.digits * b.digits).toString().padStart(scale + 1, '0');
  const integer = scale ? digits.slice(0, -scale) : digits;
  const fraction = scale ? digits.slice(-scale).replace(/0+$/, '') : '';
  return `${negative ? '-' : ''}${integer}${fraction ? `.${fraction}` : ''}`;
}

function latestSnapshot(records: BackendDecisionRecord[]): BackendPortfolioSnapshot | null {
  return records.find((record) => record.risk_context.portfolio_snapshot)?.risk_context.portfolio_snapshot ?? null;
}

function unavailable<T>(provenance: Provenance = LIVE_RECORD) {
  return { value: null as T | null, provenance, unavailableReason: UNAVAILABLE };
}

export function mapPortfolio(records: BackendDecisionRecord[]): PortfolioSummary {
  const snapshot = latestSnapshot(records);
  if (!snapshot) {
    return {
      source: 'MANUAL', equity: unavailable<number>(), cash: unavailable<number>(), buyingPower: unavailable<number>(),
      dailyPnl: unavailable<number>(), dailyPnlPct: unavailable<number>(), realizedPnl: unavailable<number>(),
      unrealizedPnl: unavailable<number>(), capitalAtRisk: unavailable<number>(),
    };
  }
  const provenance = sourceOf(snapshot.source);
  const sourced = (value: string | null, reason = UNAVAILABLE) => ({
    value: value === null ? null : displayNumber(value), provenance, asOf: snapshot.as_of,
    ...(value === null ? { unavailableReason: reason } : {}),
  });
  const dailyPct =
    snapshot.daily_pnl === null || decimalCompare(snapshot.equity, '0') === 0
      ? null
      : displayNumber(snapshot.daily_pnl) / displayNumber(snapshot.equity);
  return {
    source: snapshot.source.includes('alpaca:paper') ? 'ALPACA_PAPER' : 'MANUAL',
    equity: sourced(snapshot.equity), cash: sourced(snapshot.cash), buyingPower: sourced(snapshot.buying_power),
    dailyPnl: sourced(snapshot.daily_pnl),
    dailyPnlPct: { value: dailyPct, provenance, asOf: snapshot.as_of, ...(dailyPct === null ? { unavailableReason: UNAVAILABLE } : {}) },
    realizedPnl: unavailable<number>(provenance), unrealizedPnl: unavailable<number>(provenance),
    capitalAtRisk: unavailable<number>(provenance),
  };
}

export function mapPositions(records: BackendDecisionRecord[]): Position[] {
  const snapshot = latestSnapshot(records);
  if (!snapshot) return [];
  const equity = displayNumber(snapshot.equity);
  return snapshot.positions.map((position) => {
    const marketValue = displayNumber(position.market_value);
    return {
      symbol: position.occ_symbol ?? position.symbol,
      assetClass: position.asset_class === 'equity_option' ? 'us_option' : 'us_equity',
      side: decimalCompare(position.quantity, '0') < 0 ? 'SHORT' : 'LONG',
      quantity: Math.abs(displayNumber(position.quantity)),
      marketValue,
      costBasis: null,
      currentPrice: null,
      unrealizedPl: null,
      unrealizedPlPct: null,
      weight: equity ? Math.abs(marketValue / equity) : null,
      riskContribution: position.asset_class === 'equity' ? Math.abs(marketValue) : null,
      riskBasis: position.asset_class === 'equity' ? 'MARKET_VALUE' : 'UNAVAILABLE',
      sourceStrategy: 'Unavailable',
      provenance: sourceOf(snapshot.source),
    };
  });
}

export function mapPolicy(policy: BackendPolicy): PolicyLimit[] {
  const make = (id: string, group: PolicyLimit['group'], label: string, description: string, value: string, suffix = ''): PolicyLimit => ({
    id, group, label, description, limitDisplay: `${value}${suffix}`, utilisation: null,
    currentDisplay: 'Unavailable', status: 'UNAVAILABLE', provenance: 'MIZAN_POLICY',
  });
  const limits = [
    make('max_notional', 'equity', 'Maximum order notional', 'Maximum notional accepted for one proposal.', policy.order.max_notional, ' USD'),
    make('max_quantity', 'equity', 'Maximum quantity', 'Maximum total quantity accepted for one proposal.', policy.order.max_quantity),
    make('max_legs', 'options', 'Maximum legs', 'Maximum option legs in one atomic structure.', String(policy.order.max_legs)),
    make('max_single_symbol_pct', 'portfolio', 'Single-symbol concentration', 'Maximum share of equity in one symbol.', policy.portfolio.max_single_symbol_pct),
    make('max_drawdown_pct', 'portfolio', 'Maximum drawdown', 'Maximum drawdown admitted by policy.', policy.portfolio.max_drawdown_pct),
    make('max_buying_power_utilization', 'portfolio', 'Buying-power utilization', 'Maximum fraction of buying power one proposal may consume.', policy.portfolio.max_buying_power_utilization),
  ];
  if (policy.portfolio.max_sector_concentration_pct !== null) limits.push(make('max_sector_concentration_pct', 'portfolio', 'Sector concentration', 'Maximum share of equity in one sector.', policy.portfolio.max_sector_concentration_pct));
  if (policy.options) {
    limits.push(
      make('max_portfolio_delta', 'options', 'Portfolio delta', 'Maximum absolute portfolio delta.', policy.options.max_portfolio_delta),
      make('max_portfolio_gamma', 'options', 'Portfolio gamma', 'Maximum absolute portfolio gamma.', policy.options.max_portfolio_gamma),
      make('max_portfolio_vega', 'options', 'Portfolio vega', 'Maximum absolute portfolio vega.', policy.options.max_portfolio_vega),
      make('min_days_to_expiry', 'options', 'Minimum days to expiry', 'Minimum remaining option lifetime.', String(policy.options.min_days_to_expiry), ' days'),
      make('max_days_to_expiry', 'options', 'Maximum days to expiry', 'Maximum remaining option lifetime.', String(policy.options.max_days_to_expiry), ' days'),
    );
  }
  return limits;
}

export function mapInterventions(records: BackendDecisionRecord[]): Intervention[] {
  return records.filter((record) => record.verdict !== 'APPROVE' || record.execution?.status === 'BLOCKED').map((record) => ({
    id: `intervention-${record.decision_id}`,
    at: record.execution?.checked_at ?? record.decision_timestamp,
    proposalId: record.decision_id,
    symbol: record.proposal.symbol,
    kind: record.execution?.status === 'BLOCKED' ? 'BLOCK' : record.verdict === 'REJECT' ? 'REJECT' : 'REDUCE',
    rule: record.risk_evaluation.checks.find((check) => !check.passed)?.check_id ?? record.reason_codes[0] ?? 'governor',
    before: record.original.total_quantity,
    after: record.authorized.total_quantity,
    actor: record.execution?.status === 'BLOCKED' ? 'execution' : 'governor',
  }));
}

export function mapRiskAlerts(records: BackendDecisionRecord[]): RiskAlert[] {
  return records.flatMap((record) => record.risk_evaluation.checks.filter((check) => !check.passed).map((check) => ({
    id: `${record.decision_id}-${check.check_id}`,
    severity: severityOf(check), kind: humanise(check.check_id), symbol: record.proposal.symbol,
    proposalId: record.decision_id, reason: check.detail || check.reason_code || 'Policy check failed.',
    policy: `${record.policy.policy_id}@${record.policy.version}`, at: check.snapshot_ts ?? record.decision_timestamp,
  })));
}

export function mapSafety(health: BackendHealth): SafetyControls {
  if (!health.execution || health.environment !== 'paper') throw new Error('Authenticated health state is unavailable.');
  return {
    paperOnly: true,
    executionEnabled: health.execution.enabled,
    dryRun: health.execution.dry_run,
    killSwitch: health.execution.kill_switch_active,
    autonomy: health.execution.enabled ? (health.execution.dry_run ? 'OBSERVE' : 'AUTONOMOUS_PAPER') : 'OBSERVE',
    maxDecisionAgeSeconds: null,
  };
}

export function mapGovernanceDay(records: BackendDecisionRecord[], chain: BackendChainVerification): GovernanceDay {
  const date = records[0]?.decision_timestamp.slice(0, 10) ?? new Date().toISOString().slice(0, 10);
  const sameDay = records.filter((record) => record.decision_timestamp.startsWith(date));
  const requested = decimalSum(sameDay.map((record) => record.original.total_notional));
  const authorized = decimalSum(sameDay.map((record) => record.authorized.total_notional));
  return {
    date, decisionsGoverned: sameDay.length,
    approved: sameDay.filter((record) => record.verdict === 'APPROVE').length,
    reduced: sameDay.filter((record) => record.verdict === 'REDUCE').length,
    rejected: sameDay.filter((record) => record.verdict === 'REJECT').length,
    requestedNotional: requested, authorizedNotional: authorized,
    preventedNotional: requested !== null && authorized !== null ? decimalSubtract(requested, authorized) : null,
    chainVerified: chain.ok ? chain.length : Math.max(0, (chain.first_bad_sequence ?? 1) - 1),
    chainTotal: chain.length, needsAttention: 0, provenance: LIVE_RECORD,
  };
}

export function mapResponse(records: BackendDecisionRecord[], health: BackendHealth): ResponseState {
  const latest = records[0];
  const level = health.execution?.kill_switch_active ? 5 : latest?.risk_context.response_level ?? 0;
  return {
    level, since: latest?.decision_timestamp ?? new Date().toISOString(), engagedBy: null,
    agentsHalted: level === 5 ? 1 : 0, agentsActive: level === 5 ? 0 : 1,
    note: level === 5 ? 'Kill switch is active in the backend.' : `Latest recorded response level is ${level}.`,
  };
}

export function mapCrowding(records: BackendDecisionRecord[]): CrowdingReport {
  const state = records.find((record) => record.risk_context.aggregate_state)?.risk_context.aggregate_state;
  if (!state) {
    return { status: 'UNAVAILABLE', agentsTotal: 0, agentsCorrelated: 0, clusters: [], modelConcentration: null, signalConcentration: null, unwindDays: null, provenance: LIVE_RECORD };
  }
  const agents = Object.entries(state.exposure_by_agent);
  return {
    status: state.crowding_score !== null && decimalCompare(state.crowding_score, '0') > 0 ? 'ELEVATED' : 'NORMAL',
    agentsTotal: agents.length, agentsCorrelated: 0,
    clusters: [], modelConcentration: concentration(state.exposure_by_model_provider, 'model provider'),
    signalConcentration: concentration(state.exposure_by_signal_source, 'signal source'),
    unwindDays: state.days_to_liquidate_book, provenance: LIVE_RECORD,
  };
}

function concentration(values: Record<string, string>, kind: string) {
  const top = Object.entries(values).sort((a, b) => decimalCompare(b[1], a[1]))[0];
  return top ? { label: top[0], share: top[1], detail: `Largest recorded ${kind} exposure.` } : null;
}

export function mapOrders(records: BackendDecisionRecord[]): Order[] {
  return records.filter((record) => record.execution?.client_order_id).map((record) => {
    const execution = record.execution!;
    const quantity = decimalSum(execution.fills.map((fill) => fill.filled_quantity));
    const lifecycle: OrderLifecycleState = execution.status === 'WOULD_SUBMIT'
      ? 'WOULD_SUBMIT'
      : execution.status === 'SUBMITTED' || execution.status === 'RECONCILED_EXISTING'
        ? 'SUBMITTED'
        : 'UNKNOWN';
    return {
      clientOrderId: execution.client_order_id!, brokerOrderId: execution.broker_order_id,
      proposalId: record.decision_id, symbol: record.proposal.symbol,
      underlying: record.proposal.asset_class === 'equity_option' ? record.proposal.symbol : null,
      assetClass: record.proposal.asset_class === 'equity_option' ? 'us_option' : 'us_equity',
      orderClass: record.proposal.asset_class === 'equity_option' && record.proposal.legs.length > 1 ? 'mleg' : 'simple',
      side: record.proposal.legs[0].side.toUpperCase() as 'BUY' | 'SELL',
      proposedQuantity: displayNumber(record.original.total_quantity), approvedQuantity: displayNumber(record.authorized.total_quantity),
      filledQuantity: displayNumber(quantity), filledAvgPrice: execution.fills[0] ? displayNumber(execution.fills[0].avg_price) : null,
      lifecycle,
      brokerStatus: execution.broker_status, submittedAt: execution.submitted_at, updatedAt: execution.checked_at,
      executionMode: execution.status === 'WOULD_SUBMIT' ? 'ALPACA_PAPER_DRY_RUN' : 'ALPACA_PAPER',
      timeline: [{ at: execution.checked_at, label: execution.status, detail: execution.message, state: execution.status === 'FAILED' || execution.status === 'BLOCKED' ? 'failed' : 'done', actor: 'Mizan execution gate' }],
      provenance: 'ALPACA_PAPER',
    };
  });
}

export function mapAudit(records: BackendDecisionRecord[]): AuditEvent[] {
  return records.flatMap((record) => {
    const base = { proposalId: record.decision_id, symbol: record.proposal.symbol, provenance: LIVE_RECORD } as const;
    const events: AuditEvent[] = [
      { ...base, eventId: `${record.decision_id}-proposal`, orderId: null, actor: 'trader_agent', stage: 'trader', action: 'proposal_recorded', summary: `Requested ${record.original.total_quantity} ${record.proposal.asset_class === 'equity_option' ? 'contracts' : 'shares'}.`, at: record.proposal.created_at, outcome: 'info', payload: record.proposal as unknown as Record<string, unknown> },
      { ...base, eventId: `${record.decision_id}-risk`, orderId: null, actor: 'risk_engine', stage: 'hard_risk', action: 'deterministic_evaluation', summary: `${record.risk_evaluation.verdict}; ${record.risk_evaluation.checks.length} checks.`, at: record.risk_evaluation.evaluated_at, outcome: record.risk_evaluation.verdict === 'REJECT' ? 'blocked' : record.risk_evaluation.verdict === 'REDUCE' ? 'watch' : 'ok', payload: record.risk_evaluation as unknown as Record<string, unknown> },
      { ...base, eventId: `${record.decision_id}-governor`, orderId: record.execution?.client_order_id ?? null, actor: 'governor', stage: 'governor', action: 'governor_decision', summary: `${record.verdict}: ${record.authorized.total_quantity} of ${record.original.total_quantity} authorized.`, at: record.decision_timestamp, outcome: record.verdict === 'REJECT' ? 'blocked' : record.verdict === 'REDUCE' ? 'watch' : 'ok', payload: record.governor_decision as unknown as Record<string, unknown> },
    ];
    if (record.llm_advisory?.invoked) events.splice(2, 0, { ...base, eventId: `${record.decision_id}-advisory`, orderId: null, actor: 'ai_risk_agent', stage: 'ai_risk', action: 'advisory_review', summary: record.llm_advisory.available ? record.llm_advisory.recommendation ?? 'Advisory completed.' : 'Advisory unavailable.', at: record.decision_timestamp, outcome: record.llm_advisory.available ? 'info' : 'watch', payload: record.llm_advisory as unknown as Record<string, unknown> });
    if (record.execution) events.push({ ...base, eventId: `${record.decision_id}-execution`, orderId: record.execution.client_order_id, actor: 'execution', stage: 'execution', action: 'execution_result', summary: record.execution.message, at: record.execution.checked_at, outcome: record.execution.status === 'BLOCKED' || record.execution.status === 'FAILED' ? 'blocked' : 'ok', payload: record.execution as unknown as Record<string, unknown> });
    return events;
  }).sort((a, b) => b.at.localeCompare(a.at));
}

export function mapAgents(records: BackendDecisionRecord[]): AgentCardModel[] {
  const latest = records[0];
  const proposals = records.length;
  return STAGE_ORDER.map((id) => {
    const meta = STAGE_META[id];
    const isUpstream = meta.layer === 'intelligence';
    const configured =
      id === 'trader' ||
      id === 'hard_risk' ||
      id === 'governor' ||
      id === 'execution' ||
      (id === 'ai_risk' && records.some((record) => record.llm_advisory?.invoked));
    return {
      id, name: id === 'trader' ? latest?.proposal.agent.agent_id ?? meta.actor : meta.actor,
      layer: meta.layer, role: meta.blurb, status: configured ? 'IDLE' : 'NOT_CONFIGURED', currentAction: null,
      lastAction: latest ? stageAction(id, latest) : 'No recorded action.', lastActionAt: latest?.decision_timestamp ?? new Date(0).toISOString(),
      latencyMs: null, currentProposalId: null,
      metric: id === 'governor' ? { label: 'Recorded decisions', value: String(proposals) } : null,
      recentEvents: records.slice(0, 3).map((record) => ({ at: record.decision_timestamp, text: stageAction(id, record) })),
      provenance: isUpstream ? 'LIVE_AGENT' : LIVE_RECORD,
    };
  });
}

function stageAction(id: AgentCardModel['id'], record: BackendDecisionRecord): string {
  if (id === 'trader') return `Proposed ${record.proposal.symbol} at ${record.original.total_quantity}.`;
  if (id === 'hard_risk') return `${record.risk_evaluation.verdict} after ${record.risk_evaluation.checks.length} checks.`;
  if (id === 'ai_risk') return record.llm_advisory?.invoked ? record.llm_advisory.recommendation ?? 'Advisory unavailable.' : 'Advisory not invoked.';
  if (id === 'governor') return `${record.verdict}: ${record.authorized.total_quantity} of ${record.original.total_quantity}.`;
  if (id === 'execution') return record.execution?.message ?? 'No execution result stored.';
  return `Upstream detail is not present in TradeProposal ${record.proposal_id}.`;
}

export function mapConnections(health: BackendHealth, chain: BackendChainVerification): ConnectionState[] {
  return [
    { id: 'mizan_api', label: 'Mizan API', description: 'Authoritative tenant-scoped governance API.', status: health.status === 'ok' ? 'CONNECTED' : 'ERROR', detail: health.tenant_id ? `Authenticated for tenant ${health.tenant_id}.` : 'Only anonymous liveness is available.' },
    { id: 'audit_ledger', label: 'Hash-chained audit ledger', description: 'Append-only per-tenant decision record.', status: chain.ok ? 'CONNECTED' : 'ERROR', detail: chain.ok ? `${chain.length} stored links verified; head ${chain.head_hash?.slice(0, 12) ?? 'Unavailable'}.` : chain.detail },
    { id: 'alpaca_paper', label: 'Alpaca Paper', description: 'Paper-only broker boundary.', status: health.broker ? 'CONNECTED' : 'NOT_CONFIGURED', detail: health.broker ? `Backend broker: ${health.broker}. Environment: paper.` : 'No broker is configured for this API process.' },
  ];
}

export function mapSystemHealth(health: BackendHealth, chain: BackendChainVerification, records: BackendDecisionRecord[]): SystemHealth {
  return {
    backendVersion: records[0]?.engine_version ?? 'Mizan API v1',
    database: records.length ? 'CONNECTED' : 'NOT_CONFIGURED', auditStorage: chain.ok ? 'CONNECTED' : 'ERROR',
    broker: health.broker ? 'CONNECTED' : 'NOT_CONFIGURED', marketOpen: null, nextOpen: null, nextClose: null,
  };
}

export function mapNotifications(records: BackendDecisionRecord[]): AppNotification[] {
  return records.filter((record) => record.verdict !== 'APPROVE' || record.execution).slice(0, 12).map((record) => ({
    id: `notification-${record.decision_id}`, at: record.execution?.checked_at ?? record.decision_timestamp,
    severity: record.verdict === 'REJECT' ? 'danger' : record.verdict === 'REDUCE' ? 'warn' : 'info',
    title: `${record.proposal.symbol} ${record.verdict.toLowerCase()}`,
    body: record.execution?.message ?? `${record.authorized.total_quantity} of ${record.original.total_quantity} authorized.`,
    href: `/app/proposals/${record.decision_id}`, read: true,
  }));
}

export function findOrder(records: BackendDecisionRecord[], clientOrderId: string): Order | null {
  return mapOrders(records).find((order) => order.clientOrderId === clientOrderId) ?? null;
}

export function findProposal(records: BackendDecisionRecord[], chain: BackendChainVerification, proposalId: string): Proposal | null {
  const record = records.find((item) => item.proposal_id === proposalId || item.decision_id === proposalId);
  return record ? mapDecision(record, chain) : null;
}

export function filterAudit(events: AuditEvent[], filter?: { proposalId?: string; symbol?: string; actor?: string; outcome?: AuditEvent['outcome']; query?: string }) {
  if (!filter) return events;
  return events.filter((event) => {
    if (filter.proposalId && event.proposalId !== filter.proposalId) return false;
    if (filter.symbol && event.symbol !== filter.symbol) return false;
    if (filter.actor && event.actor !== filter.actor) return false;
    if (filter.outcome && event.outcome !== filter.outcome) return false;
    if (filter.query && !`${event.summary} ${event.action} ${event.symbol} ${event.proposalId}`.toLowerCase().includes(filter.query.toLowerCase())) return false;
    return true;
  });
}
