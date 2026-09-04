import type { ReactNode } from 'react';
import { ArrowRight, Link2 } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { REASON_TEXT } from '@/data/checks';
import { decimalMoney } from '@/lib/decimal';
import { cx, integer, timeOf } from '@/lib/format';
import type { Authorization, ChainStamp, Decision, DecisionOutcome, OutcomeLeg, ReasonCode } from '@/types/domain';
import { VerdictBadge } from './Decision';
import { AuthorizationSeal } from './AuthorizationSeal';
import { PreventionBar } from './PreventionBar';

export { AuthorizationSeal, PreventionBar };

/**
 * Requested → Authorized → Executed.
 *
 * The signature element of the product. Three columns, always, in that order.
 * The gap between the first and the second is the thing the product exists to
 * create, so the requested figure is never replaced, struck through or hidden
 * once policy has spoken — an operator needs to see what the agent asked for.
 *
 * The vocabulary is fixed: an agent *requests*, policy *authorizes*, the broker
 * *executes*. Nothing here is negotiated, countered or allowed.
 */

interface LedgerProps {
  outcome: DecisionOutcome;
  size?: 'md' | 'lg';
  /** Suppresses the third column where execution is not part of the story. */
  showExecuted?: boolean;
}

export function AuthorizationLedger({ outcome, size = 'md', showExecuted = true }: LedgerProps) {
  const { requested, authorized, executed, unit } = outcome;
  const reduced = authorized !== null && authorized.quantity < requested.quantity;
  const rejected = authorized !== null && authorized.quantity === 0;

  return (
    <div className={cx('ledger3wrap', `ledger3wrap--${size}`)}>
      <ol className={cx('ledger3', `ledger3--${size}`)}>
        <Column
          stage="Requested"
          state="filled"
          quantity={`${integer(requested.quantity)} ${unit}`}
          notional={decimalMoney(requested.notional)}
        />

        <Arrow />

        <Column
          stage="Authorized"
          state={authorized === null ? 'pending' : rejected ? 'zero' : reduced ? 'reduced' : 'filled'}
          quantity={
            authorized === null
              ? 'Awaiting verdict'
              : rejected
                ? `0 ${unit}`
                : `${integer(authorized.quantity)} ${unit}`
          }
          notional={authorized === null ? null : decimalMoney(authorized.notional)}
        />

        {showExecuted && (
          <>
            <Arrow />
            <Column
              stage="Executed"
              state={executed === null ? 'pending' : 'filled'}
              quantity={
                executed === null
                  ? authorized === null
                    ? 'Not reached'
                    : rejected
                      ? 'Nothing to execute'
                      : 'Not executed'
                  : `${integer(executed.quantity)} filled`
              }
              notional={executed === null ? null : decimalMoney(executed.notional)}
            />
          </>
        )}
      </ol>
    </div>
  );
}

function Column({
  stage,
  state,
  quantity,
  notional,
}: {
  stage: string;
  state: 'filled' | 'reduced' | 'zero' | 'pending';
  quantity: string;
  notional: string | null;
}) {
  return (
    <li className={cx('ledger3__col', `is-${state}`)}>
      <p className="ledger3__stage">{stage}</p>
      <p className="ledger3__qty">{quantity}</p>
      <p className="ledger3__notional u-mono">{notional ?? <span className="u-dim">—</span>}</p>
    </li>
  );
}

function Arrow() {
  return (
    <li className="ledger3__arrow" aria-hidden="true">
      <ArrowRight size={14} />
    </li>
  );
}

/* --------------------------------------------------------------- reason codes */

/**
 * The codes that produced the verdict.
 *
 * The code is what the engine emitted and what the audit record stores; the
 * phrase beside it is a rendering of that code and never the source of truth.
 */
export function ReasonCodes({ codes, limit }: { codes: ReasonCode[]; limit?: number }) {
  if (codes.length === 0) return null;
  const shown = limit ? codes.slice(0, limit) : codes;
  return (
    <ul className="reasoncodes">
      {shown.map((code) => (
        <li key={code} className="reasoncodes__item">
          <span className="reasoncodes__marker" aria-hidden="true">
            ▸
          </span>
          <code className="reasoncodes__code">{code}</code>
          <span className="reasoncodes__text">{REASON_TEXT[code] ?? code.replaceAll('_', ' ').toLowerCase()}</span>
        </li>
      ))}
      {limit && codes.length > limit && (
        <li className="reasoncodes__item u-dim">+{codes.length - limit} more</li>
      )}
    </ul>
  );
}

/* ------------------------------------------------------------------ the card */

interface DecisionCardProps {
  title: ReactNode;
  /** Proposal or decision identifier, rendered as a record reference. */
  recordId: string;
  at: string;
  decision: Decision | null;
  outcome: DecisionOutcome;
  reasonCodes: ReasonCode[];
  chain: ChainStamp | null;
  /** One line of identity: agent, policy version, model. */
  identity?: ReactNode;
  /** Authorization object for the seal. */
  authorization?: Authorization | null;
  /** Authorization lifetime line, when there is one to state. */
  authorizationLine?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

/**
 * Level 1 of the disclosure ladder: the verdict, and nothing that needs
 * explaining before the verdict has landed.
 */
export function DecisionCard({
  title,
  recordId,
  at,
  decision,
  outcome,
  reasonCodes,
  chain,
  identity,
  authorization,
  authorizationLine,
  actions,
  className,
}: DecisionCardProps) {
  return (
    <article className={cx('dcard', decision && `dcard--${decision.toLowerCase()}`, className)}>
      <header className="dcard__head">
        <h2 className="dcard__title">{title}</h2>
        <div className="dcard__meta">
          <time className="dcard__time u-mono" dateTime={at} title={`${at} (UTC)`}>
            {timeOf(at)} UTC
          </time>
          {chain && (
            <code className="dcard__record u-mono" title={`Record ${chain.position} · ${chain.recordHash}`}>
              #{chain.recordHash.slice(0, 6)}
            </code>
          )}
        </div>
      </header>

      <div className="dcard__verdictbar">
        {decision ? <VerdictBadge decision={decision} size="md" /> : <Badge tone="accent" size="md">IN REVIEW</Badge>}
        {chain && (
          <span className={cx('chainstamp', chain.verified ? 'is-ok' : 'is-bad')}>
            <Link2 size={12} aria-hidden="true" />
            {chain.verified ? 'Stored record hash verified' : 'Verification failed'}
            <span className="u-dim u-mono"> · {integer(chain.position)}</span>
          </span>
        )}
      </div>

      <div className="dcard__body">
        <AuthorizationLedger outcome={outcome} size="lg" />
        <PreventionBar outcome={outcome} decision={decision} />
        <ReasonCodes codes={reasonCodes} />
      </div>

      {authorization !== undefined && (
        <AuthorizationSeal authorization={authorization} reasonCodes={reasonCodes} />
      )}

      {(identity || authorizationLine) && (
        <footer className="dcard__foot">
          {identity && <p className="dcard__identity">{identity}</p>}
          {authorizationLine && <p className="dcard__auth">{authorizationLine}</p>}
          <p className="dcard__ref u-mono u-dim">{recordId}</p>
        </footer>
      )}

      {actions && <div className="dcard__actions">{actions}</div>}
    </article>
  );
}

export type { OutcomeLeg };
