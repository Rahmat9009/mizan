import { cx, timeOf } from '@/lib/format';
import type { Authorization, ReasonCode } from '@/types/domain';

const SEAL_LABEL: Record<Authorization['status'], string> = {
  ACTIVE: 'VALID',
  USED: 'CONSUMED',
  EXPIRED: 'EXPIRED',
  INVALIDATED: 'INVALIDATED',
  NOT_ISSUED: 'NOT ISSUED',
};

const SEAL_GLYPH: Record<Authorization['status'], string> = {
  ACTIVE: '◆',
  USED: '●',
  EXPIRED: '✕',
  INVALIDATED: '✕',
  NOT_ISSUED: '─',
};

interface AuthorizationSealProps {
  authorization: Authorization | null;
  reasonCodes?: ReasonCode[];
  className?: string;
}

/**
 * Authorization Seal.
 *
 * Tangible record of permission bound to a specific state of the world:
 * proposal + portfolio state + market state + policy / time state = authorization valid for that world-state.
 *
 * State word is visually primary; IDs and state hashes sit in a secondary, subdued monospace tier.
 * Never hides INVALIDATED, EXPIRED, or REAUTHORIZATION_REQUIRED.
 */
export function AuthorizationSeal({
  authorization,
  reasonCodes = [],
  className,
}: AuthorizationSealProps) {
  if (!authorization || authorization.status === 'NOT_ISSUED') {
    return (
      <div className={cx('seal', 'seal--none', className)}>
        <span className="seal__status">
          <span aria-hidden="true">─</span> NO AUTHORIZATION ISSUED
        </span>
        <span className="seal__life">Nothing exists for execution to act on.</span>
      </div>
    );
  }

  const status = authorization.status;
  const isDead =
    status === 'EXPIRED' ||
    status === 'INVALIDATED' ||
    reasonCodes.includes('REAUTHORIZATION_REQUIRED') ||
    reasonCodes.includes('AUTHORIZATION_EXPIRED');

  return (
    <div className={cx('seal', `seal--${status.toLowerCase()}`, className)}>
      <span className="seal__status">
        <span aria-hidden="true">{SEAL_GLYPH[status]}</span> {SEAL_LABEL[status]}
      </span>

      <code className="seal__id u-mono">#{authorization.id}</code>

      <span className="seal__life u-mono">
        {status === 'ACTIVE'
          ? `valid ${authorization.ttlSeconds}s from ${timeOf(authorization.issuedAt)} UTC`
          : authorization.usedAt
            ? `used ${timeOf(authorization.usedAt)} UTC (valid ${authorization.ttlSeconds}s)`
            : `lapsed ${timeOf(authorization.expiresAt)} UTC unused`}
      </span>

      <span className="seal__bindings">
        <span className="seal__binding">
          <span className="seal__bindkey">portfolio state</span>
          <code className="u-mono u-break">{authorization.boundPortfolioState}</code>
        </span>
        <span className="seal__binding">
          <span className="seal__bindkey">market state</span>
          <code className="u-mono u-break">{authorization.boundMarketState}</code>
        </span>
      </span>

      {isDead && (
        <span className="seal__void" role="status">
          REAUTHORIZATION_REQUIRED
        </span>
      )}
    </div>
  );
}
