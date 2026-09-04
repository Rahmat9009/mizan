import type { CheckMeasurement, CheckUnit, ReasonCode, RiskCheck, RiskSeverity } from '@/types/domain';
import type { Decimal } from '@/lib/decimal';

/**
 * The deterministic policy catalogue.
 *
 * Each entry is what the engine knows about one rule: the label an operator
 * reads, the reason code it emits when it fails, and the kind of quantity it
 * measures. The demo data supplies only the numbers, so a check can never drift
 * away from its own code or its own units.
 */

interface CheckMeta {
  label: string;
  reasonCode: ReasonCode | null;
  unit: CheckUnit;
  bound: 'ceiling' | 'floor';
  /** Whether the measured quantity moves with order size. */
  sizeInvariant: boolean;
}

export const CHECK_META: Record<string, CheckMeta> = {
  max_trade_allocation: {
    label: 'Single-trade allocation',
    reasonCode: 'TRADE_ALLOCATION_LIMIT',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  max_position_concentration: {
    label: 'Portfolio concentration',
    reasonCode: 'CONCENTRATION_LIMIT',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  correlated_exposure: {
    label: 'Correlated end-market exposure',
    reasonCode: 'CORRELATED_EXPOSURE',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  daily_drawdown_floor: {
    label: 'Session drawdown floor',
    reasonCode: 'DRAWDOWN_FLOOR',
    unit: 'ratio',
    bound: 'floor',
    sizeInvariant: true,
  },
  max_drawdown_30d: {
    label: '30-day maximum drawdown',
    reasonCode: 'DRAWDOWN_FLOOR',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: true,
  },
  symbol_volatility: {
    label: 'Symbol volatility',
    reasonCode: 'VOLATILITY_CEILING',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: true,
  },
  liquidity_floor: {
    label: 'Liquidity',
    reasonCode: 'LIQUIDITY_FLOOR',
    unit: 'ratio',
    bound: 'floor',
    sizeInvariant: true,
  },
  strategy_confidence: {
    label: 'Strategy confidence',
    reasonCode: 'CONFIDENCE_FLOOR',
    unit: 'ratio',
    bound: 'floor',
    sizeInvariant: true,
  },
  options_max_defined_loss_pct_equity: {
    label: 'Defined loss against equity',
    reasonCode: 'DEFINED_LOSS_LIMIT',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  options_max_defined_loss_pct_buying_power: {
    label: 'Defined loss against buying power',
    reasonCode: 'BUYING_POWER_LIMIT',
    unit: 'ratio',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  options_max_contracts: {
    label: 'Contract count',
    reasonCode: 'CONTRACT_COUNT_LIMIT',
    unit: 'count',
    bound: 'ceiling',
    sizeInvariant: false,
  },
  options_min_dte: {
    label: 'Days to expiry',
    reasonCode: 'MIN_DTE',
    unit: 'days',
    bound: 'floor',
    sizeInvariant: true,
  },
  structure_no_naked_short: {
    label: 'Structure cover',
    reasonCode: 'STRUCTURE_NOT_ALLOWED',
    unit: 'count',
    bound: 'floor',
    sizeInvariant: true,
  },
  options_strategy_allowlist: {
    label: 'Strategy allowlist',
    reasonCode: 'STRUCTURE_NOT_ALLOWED',
    unit: 'count',
    bound: 'floor',
    sizeInvariant: true,
  },
  options_declared_economics: {
    label: 'Declared economics',
    reasonCode: null,
    unit: 'currency',
    bound: 'ceiling',
    sizeInvariant: true,
  },
};

interface Numbers {
  threshold: Decimal;
  /** Portfolio state before the trade. Omit for checks that are per-trade. */
  current?: Decimal;
  ifRequested: Decimal;
  /** Omit where nothing was authorized. */
  ifAuthorized?: Decimal;
}

/**
 * Builds one check result.
 *
 * `numbers` is optional: a few rules are structural rather than numeric, and a
 * structural rule renders as a pass/fail statement with no bar chart. Passing
 * no numbers is how the data says "there is nothing to plot here", which is
 * different from plotting zero.
 */
export function check(
  rule: keyof typeof CHECK_META | (string & {}),
  passed: boolean,
  severity: RiskSeverity,
  message: string,
  numbers?: Numbers,
  recommendedQuantity?: number | null,
): RiskCheck {
  const meta = CHECK_META[rule];
  if (!meta) throw new Error(`Unknown check rule: ${rule}`);

  const measurement: CheckMeasurement | null = numbers
    ? {
        unit: meta.unit,
        bound: meta.bound,
        threshold: numbers.threshold,
        actualCurrent: numbers.current ?? null,
        actualIfRequested: numbers.ifRequested,
        actualIfAuthorized: numbers.ifAuthorized ?? null,
        sizeInvariant: meta.sizeInvariant,
      }
    : null;

  return {
    rule,
    passed,
    severity,
    label: meta.label,
    reasonCode: meta.reasonCode,
    measurement,
    message,
    ...(recommendedQuantity === undefined ? {} : { recommendedQuantity }),
  };
}

/** Reason codes rendered as the short operator-facing phrase they stand for. */
export const REASON_TEXT: Partial<Record<ReasonCode, string>> = {
  TRADE_ALLOCATION_LIMIT: 'Single-trade allocation ceiling',
  CONCENTRATION_LIMIT: 'Portfolio concentration ceiling',
  VOLATILITY_CEILING: 'Symbol volatility ceiling',
  DRAWDOWN_FLOOR: 'Drawdown limit',
  LIQUIDITY_FLOOR: 'Liquidity floor',
  CONFIDENCE_FLOOR: 'Strategy confidence floor',
  CORRELATED_EXPOSURE: 'Correlated end-market ceiling',
  DEFINED_LOSS_LIMIT: 'Defined-loss ceiling',
  BUYING_POWER_LIMIT: 'Buying-power ceiling',
  CONTRACT_COUNT_LIMIT: 'Contract-count ceiling',
  MIN_DTE: 'Minimum days to expiry',
  STRUCTURE_NOT_ALLOWED: 'Structure not permitted',
  AGGREGATE_SECTOR_GUIDANCE: 'Aggregate sector guidance',
  AUTHORIZATION_EXPIRED: 'Authorization expired',
  BOUND_STATE_CHANGED: 'Bound state changed',
  REAUTHORIZATION_REQUIRED: 'Reauthorization required',
  KILL_SWITCH_ACTIVE: 'Full stop engaged',
  MARKET_CLOSED: 'Market closed',
};
