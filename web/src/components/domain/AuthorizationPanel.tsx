import { useEffect, useState } from 'react';
import { Info } from 'lucide-react';
import { REASON_TEXT } from '@/data/checks';
import { cx, integer, timeOf } from '@/lib/format';
import { usePrefersReducedMotion } from '@/lib/hooks';
import type { Authorization, DecisionOutcome } from '@/types/domain';

/**
 * An authorization, with its lifetime and its bindings.
 *
 * A permission here is not valid for a period of time; it is valid for a state
 * of the world. It carries the hash of the portfolio state and the hash of the
 * market state it was issued against, and if either moves the permission is
 * void before the clock runs out.
 *
 * The countdown ticks only for a permission that is genuinely live. Records in
 * the demo dataset are historical, so they render their consumed lifetime
 * rather than pretending to count down — no fake liveness.
 */

interface Props {
  authorization: Authorization | null;
  outcome: DecisionOutcome;
  /** Instrument summary line, e.g. "20 NVDA · policy v18". */
  subject: string;
}

export function AuthorizationPanel({ authorization, outcome, subject }: Props) {
  const now = useNow(authorization?.status === 'ACTIVE');
  const reduced = usePrefersReducedMotion();

  if (!authorization || authorization.status === 'NOT_ISSUED') {
    return (
      <div className="auth auth--none">
        <p className="auth__nonetitle">No authorization was issued</p>
        <p className="auth__nonebody">
          {authorization?.invalidationDetail ??
            'The proposal has not reached a verdict, so no permission exists to execute against.'}
        </p>
      </div>
    );
  }

  const issued = Date.parse(authorization.issuedAt);
  const expires = Date.parse(authorization.expiresAt);
  const span = Math.max(expires - issued, 1);
  const live = authorization.status === 'ACTIVE';

  const consumedMs = live
    ? Math.min(Math.max(now - issued, 0), span)
    : authorization.usedAt
      ? Math.min(Math.max(Date.parse(authorization.usedAt) - issued, 0), span)
      : span;
  const consumed = consumedMs / span;
  const remainingSeconds = live ? Math.max(Math.ceil((expires - now) / 1000), 0) : null;

  const invalid = authorization.status === 'EXPIRED' || authorization.status === 'INVALIDATED';

  return (
    <div className={cx('auth', `auth--${authorization.status.toLowerCase()}`)}>
      <header className="auth__head">
        <span className="auth__label">Authorization</span>
        <code className="auth__id u-mono">#{authorization.id}</code>
        <span className={cx('auth__status', invalid && 'is-invalid')}>
          <span aria-hidden="true">{invalid ? '✕' : authorization.status === 'USED' ? '●' : '◆'}</span>{' '}
          {STATUS_TEXT[authorization.status]}
        </span>
      </header>

      <p className="auth__subject">{subject}</p>

      <div
        className="auth__track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(consumed * 100)}
        aria-label={`Authorization lifetime, ${Math.round(consumed * 100)} percent consumed`}
      >
        {/* Scaled rather than resized: this bar animates every tick of a live
            countdown, and transitioning `width` re-runs layout on each frame. */}
        <span
          className={cx('auth__fill', invalid && 'is-invalid', reduced && 'is-static')}
          style={{ transform: `scaleX(${consumed})` }}
        />
      </div>

      <p className="auth__timing u-mono">
        {live && remainingSeconds !== null ? (
          <>expires in {remainingSeconds}s</>
        ) : authorization.usedAt ? (
          <>
            valid {authorization.ttlSeconds}s from {timeOf(authorization.issuedAt)} · used at{' '}
            {timeOf(authorization.usedAt)} UTC
          </>
        ) : (
          <>
            valid {authorization.ttlSeconds}s from {timeOf(authorization.issuedAt)} · lapsed{' '}
            {timeOf(authorization.expiresAt)} UTC unused
          </>
        )}
      </p>

      <dl className="auth__bindings">
        <div className="auth__binding">
          <dt>Bound to portfolio state</dt>
          <dd className="u-mono">{authorization.boundPortfolioState}</dd>
        </div>
        <div className="auth__binding">
          <dt>Bound to market state</dt>
          <dd className="u-mono">{authorization.boundMarketState}</dd>
        </div>
      </dl>

      <p className="auth__note">
        <Info size={12} aria-hidden="true" />
        If either state changes, this authorization becomes invalid — even before it expires.
      </p>

      {invalid && (
        <div className="authinvalid" role="note">
          <p className="authinvalid__title">
            <span aria-hidden="true">✕</span> AUTHORIZATION INVALID
          </p>
          {authorization.invalidationDetail && <p className="authinvalid__body">{authorization.invalidationDetail}</p>}
          <p className="authinvalid__result">
            Result: <code className="u-mono">REAUTHORIZATION_REQUIRED</code>
            {authorization.invalidationCode && (
              <span className="u-dim"> · {REASON_TEXT[authorization.invalidationCode] ?? authorization.invalidationCode.replaceAll('_', ' ').toLowerCase()}</span>
            )}
          </p>
          <p className="authinvalid__foot">
            {integer(outcome.requested.quantity)} {outcome.unit} requested. Nothing reached the broker.
          </p>
        </div>
      )}
    </div>
  );
}

const STATUS_TEXT: Record<Authorization['status'], string> = {
  ACTIVE: 'ACTIVE',
  USED: 'USED',
  EXPIRED: 'EXPIRED',
  INVALIDATED: 'INVALIDATED',
  NOT_ISSUED: 'NOT ISSUED',
};

/** Ticks once a second, and only while something is genuinely counting down. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}
