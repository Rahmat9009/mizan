import { cx, integer, percent } from '@/lib/format';
import { decimalMoney } from '@/lib/decimal';
import type { Decision, DecisionOutcome } from '@/types/domain';

interface PreventionBarProps {
  outcome: DecisionOutcome;
  decision?: Decision | null;
  showNotional?: boolean;
  className?: string;
}

/**
 * Prevention Bar.
 *
 * Visualizes the derived gap: REQUESTED − AUTHORIZED.
 * It is NOT a fourth lifecycle stage; the lifecycle remains Requested → Authorized → Executed.
 *
 * Visual semantics:
 * - Authorized: solid green/authorized fill
 * - Prevented gap: restrained amber/hazard hatch for ordinary reduction (REDUCE),
 *   or danger hatch for policy block/rejection (REJECT).
 * - Full approval (APPROVE): 100% solid fill, zero hatched gap.
 * - Numeric statement is primary and derived strictly from data; patterned bar supports it.
 */
export function PreventionBar({
  outcome,
  decision,
  showNotional = true,
  className,
}: PreventionBarProps) {
  const { requested, authorized, unit, preventedNotional } = outcome;
  if (requested.quantity <= 0) return null;

  const reqQty = requested.quantity;
  const authQty = authorized?.quantity ?? null;

  const effectiveDecision: Decision | 'PENDING' =
    decision ??
    (authQty === null
      ? 'PENDING'
      : authQty === 0
        ? 'REJECT'
        : authQty < reqQty
          ? 'REDUCE'
          : 'APPROVE');

  const authShare = authQty !== null ? Math.min(Math.max(authQty / reqQty, 0), 1) : null;
  const preventedQty = authQty !== null ? Math.max(0, reqQty - authQty) : null;
  const preventedShare = preventedQty !== null ? preventedQty / reqQty : null;

  return (
    <div
      className={cx(
        'prevbar',
        `prevbar--${effectiveDecision.toLowerCase()}`,
        className,
      )}
    >
      <div
        className={cx(
          'prevbar__track',
          effectiveDecision === 'PENDING' && 'is-pending',
          effectiveDecision === 'APPROVE' && 'is-approved',
          effectiveDecision === 'REDUCE' && 'is-reduced',
          effectiveDecision === 'REJECT' && 'is-rejected',
        )}
        role="img"
        aria-label={
          effectiveDecision === 'PENDING'
            ? 'Awaiting policy authorization decision.'
            : effectiveDecision === 'APPROVE'
              ? `All ${integer(reqQty)} ${unit} authorized. Zero exposure prevented.`
              : effectiveDecision === 'REDUCE'
                ? `${integer(authQty ?? 0)} of ${integer(reqQty)} ${unit} (${percent(
                    authShare ?? 0,
                    0,
                  )}) authorized. ${integer(preventedQty ?? 0)} ${unit} (${percent(
                    preventedShare ?? 0,
                    0,
                  )}) did not receive authorization.`
                : `${integer(reqQty)} of ${integer(reqQty)} ${unit} (100%) not authorized by policy.`
        }
      >
        {authShare !== null && authShare > 0 && (
          <span
            className="prevbar__fill"
            style={{ width: `${authShare * 100}%` }}
          />
        )}
        {authShare !== null && authShare > 0 && authShare < 1 && (
          <span
            className="prevbar__stop"
            style={{ left: `${authShare * 100}%` }}
            aria-hidden="true"
          />
        )}
      </div>

      <p className="prevbar__caption">
        {effectiveDecision === 'PENDING' && (
          <span className="u-dim">Policy has not answered yet.</span>
        )}

        {effectiveDecision === 'APPROVE' && (
          <>
            <strong>0 of {integer(reqQty)} {unit}</strong> prevented · Full requested size authorized
          </>
        )}

        {effectiveDecision === 'REDUCE' && preventedQty !== null && (
          <>
            <strong>
              {integer(preventedQty)} of {integer(reqQty)} {unit}
              {preventedShare !== null && ` (${percent(preventedShare, 0)})`}
            </strong>{' '}
            did not receive authorization
            {showNotional && preventedNotional && (
              <span className="prevbar__notional">
                {' '}· <span className="u-mono">{decimalMoney(preventedNotional)}</span> of exposure prevented
              </span>
            )}
          </>
        )}

        {effectiveDecision === 'REJECT' && (
          <>
            <strong>
              {integer(reqQty)} of {integer(reqQty)} {unit} (100%)
            </strong>{' '}
            refused by policy · No quantity authorized
            {showNotional && preventedNotional && (
              <span className="prevbar__notional">
                {' '}· <span className="u-mono">{decimalMoney(preventedNotional)}</span> of exposure prevented
              </span>
            )}
          </>
        )}
      </p>
    </div>
  );
}
