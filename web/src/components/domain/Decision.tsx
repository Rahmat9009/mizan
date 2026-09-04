import { ArrowRight } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import type { Tone } from '@/components/ui/Badge';
import { cx, integer } from '@/lib/format';
import type { Decision, ExecutionState, OrderLifecycleState, RiskSeverity } from '@/types/domain';

export const DECISION_TONE: Record<Decision, Tone> = {
  APPROVE: 'ok',
  REDUCE: 'warn',
  REJECT: 'danger',
};

/**
 * The verdict, as a user reads it.
 *
 * The enum the engine emits is imperative (`REDUCE`); what happened to a
 * decision that has already been made is not. The three words below are the
 * only ones used in the interface — never "allowed", which is too permissive,
 * and never "granted", which implies discretion the system does not have.
 */
export const VERDICT_LABEL: Record<Decision, string> = {
  APPROVE: 'APPROVED',
  REDUCE: 'REDUCED',
  REJECT: 'REJECTED',
};

/**
 * Shape carries the verdict alongside the word and the colour.
 *
 * Around one man in twelve has a colour-vision deficiency, and this is sold to
 * trading desks. Three signals, always: glyph, word, colour.
 */
export const VERDICT_GLYPH: Record<Decision, string> = {
  APPROVE: '●',
  REDUCE: '▼',
  REJECT: '✕',
};

export function VerdictBadge({ decision, size = 'sm' }: { decision: Decision; size?: 'sm' | 'md' | 'lg' }) {
  return (
    <Badge tone={DECISION_TONE[decision]} size={size} glyph={VERDICT_GLYPH[decision]}>
      {VERDICT_LABEL[decision]}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: RiskSeverity }) {
  const tone: Tone =
    severity === 'BLOCK' ? 'danger' : severity === 'HIGH' ? 'danger' : severity === 'WATCH' ? 'warn' : 'neutral';
  return <Badge tone={tone}>{severity}</Badge>;
}

const EXECUTION_TONE: Record<ExecutionState, Tone> = {
  EXECUTION_DISABLED: 'neutral',
  BLOCKED: 'danger',
  AUTHORIZED: 'accent',
  STALE_AUTHORIZATION: 'warn',
  KILL_SWITCH_ACTIVE: 'danger',
  MARKET_CLOSED: 'warn',
  ASSET_NOT_TRADABLE: 'danger',
  REAUTHORIZATION_REQUIRED: 'warn',
  WOULD_SUBMIT: 'neutral',
  RECONCILED_EXISTING_ORDER: 'accent',
  SUBMITTED: 'ok',
  FAILED: 'danger',
  NOT_REACHED: 'neutral',
};

const EXECUTION_LABEL: Record<ExecutionState, string> = {
  EXECUTION_DISABLED: 'Execution disabled',
  BLOCKED: 'Blocked',
  AUTHORIZED: 'Authorized',
  STALE_AUTHORIZATION: 'Stale authorization',
  KILL_SWITCH_ACTIVE: 'Kill switch active',
  MARKET_CLOSED: 'Market closed',
  ASSET_NOT_TRADABLE: 'Asset not tradable',
  REAUTHORIZATION_REQUIRED: 'Re-authorization required',
  WOULD_SUBMIT: 'Would submit',
  RECONCILED_EXISTING_ORDER: 'Reconciled existing order',
  SUBMITTED: 'Submitted',
  FAILED: 'Failed',
  NOT_REACHED: 'Not reached',
};

export function ExecutionBadge({ state }: { state: ExecutionState }) {
  return <Badge tone={EXECUTION_TONE[state]}>{EXECUTION_LABEL[state]}</Badge>;
}

const ORDER_TONE: Record<OrderLifecycleState, Tone> = {
  DRY_RUN: 'neutral',
  WOULD_SUBMIT: 'neutral',
  SUBMITTED: 'accent',
  NEW: 'accent',
  PARTIALLY_FILLED: 'warn',
  FILLED: 'ok',
  REJECTED: 'danger',
  CANCELED: 'neutral',
  EXPIRED: 'neutral',
  PENDING: 'neutral',
  UNKNOWN: 'neutral',
};

const ORDER_LABEL: Record<OrderLifecycleState, string> = {
  DRY_RUN: 'Dry run',
  WOULD_SUBMIT: 'Would submit',
  SUBMITTED: 'Submitted',
  NEW: 'New',
  PARTIALLY_FILLED: 'Partially filled',
  FILLED: 'Filled',
  REJECTED: 'Rejected',
  CANCELED: 'Canceled',
  EXPIRED: 'Expired',
  PENDING: 'Pending',
  UNKNOWN: 'Unknown',
};

export function OrderStateBadge({ state }: { state: OrderLifecycleState }) {
  return <Badge tone={ORDER_TONE[state]}>{ORDER_LABEL[state]}</Badge>;
}

export const orderStateLabel = (state: OrderLifecycleState) => ORDER_LABEL[state];

/**
 * The quantity ledger: what an agent requested, and what policy authorized.
 *
 * The dense form of the signature element, for table rows and list items where
 * the full three-column card does not fit. When the two numbers differ the
 * requested figure stays legible rather than being replaced — an operator needs
 * to see what was asked for, not only what was permitted.
 */
export function QuantityLedger({
  proposed,
  approved,
  unit = 'shares',
  size = 'md',
}: {
  proposed: number;
  approved: number;
  unit?: string;
  size?: 'sm' | 'md' | 'lg';
}) {
  const changed = proposed !== approved;
  const blocked = approved === 0;

  return (
    <span
      className={cx('ledger', `ledger--${size}`, changed && 'ledger--changed', blocked && 'ledger--blocked')}
      aria-label={
        changed
          ? `${integer(proposed)} ${unit} requested, ${integer(approved)} ${unit} authorized`
          : `${integer(approved)} ${unit} authorized as requested`
      }
    >
      <span className="ledger__from">{integer(proposed)}</span>
      {changed && (
        <>
          <ArrowRight size={size === 'lg' ? 16 : 12} aria-hidden="true" className="ledger__arrow" />
          <span className="ledger__to">{integer(approved)}</span>
        </>
      )}
      <span className="ledger__unit">{unit}</span>
    </span>
  );
}
