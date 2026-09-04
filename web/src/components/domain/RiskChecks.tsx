import { Check, Minus, X } from 'lucide-react';
import { decimalFixed, decimalPercent } from '@/lib/decimal';
import type { Decimal } from '@/lib/decimal';
import { cx, humanise, integer } from '@/lib/format';
import type { CheckMeasurement, RiskCheck } from '@/types/domain';
import { SeverityBadge } from './Decision';

/**
 * Level 3: every deterministic rule, with the arithmetic it performed.
 *
 * Each row carries the operator-facing label, the rule identifier the engine
 * actually ran, and — where the rule measures something — its threshold and its
 * actuals. That triple is what lets a decision be traced back to policy rather
 * than to a summary sentence.
 */
export function RiskCheckList({ checks }: { checks: RiskCheck[] }) {
  return (
    <ul className="checks">
      {checks.map((check) => (
        <li key={check.rule} className={cx('checks__row', !check.passed && 'checks__row--failed')}>
          <span className={cx('checks__icon', check.passed ? 'is-ok' : check.severity === 'BLOCK' ? 'is-danger' : 'is-warn')}>
            {check.passed ? <Check size={12} aria-hidden="true" /> : <X size={12} aria-hidden="true" />}
          </span>
          <div className="checks__body">
            <div className="checks__head">
              <span className="checks__label">{check.label}</span>
              <SeverityBadge severity={check.severity} />
              <span className="u-sr-only">{check.passed ? 'Passed' : 'Failed'}</span>
            </div>
            <p className="checks__message">{check.message}</p>
            {check.measurement && <Measurement m={check.measurement} />}
            <p className="checks__ids">
              <code className="checks__rule">{check.rule}</code>
              {!check.passed && check.reasonCode && (
                <>
                  <span aria-hidden="true"> → </span>
                  <code className="checks__reason">{check.reasonCode}</code>
                </>
              )}
            </p>
            {check.recommendedQuantity !== undefined && check.recommendedQuantity !== null && (
              <p className="checks__rec">
                Policy maximum under this rule <strong>{integer(check.recommendedQuantity)}</strong>
              </p>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Threshold and actuals, as the engine recorded them. */
function Measurement({ m }: { m: CheckMeasurement }) {
  const fmt = (v: Decimal) =>
    m.unit === 'ratio' ? (decimalPercent(v) ?? v) : m.unit === 'days' ? `${decimalFixed(v, 0)}d` : (decimalFixed(v, m.unit === 'count' ? 0 : 2) ?? v);

  return (
    <dl className="checks__measure">
      <div>
        <dt>{m.bound === 'ceiling' ? 'Limit' : 'Minimum'}</dt>
        <dd className="u-mono">{fmt(m.threshold)}</dd>
      </div>
      {m.actualCurrent !== null && (
        <div>
          <dt>Current</dt>
          <dd className="u-mono">{fmt(m.actualCurrent)}</dd>
        </div>
      )}
      <div>
        <dt>If requested</dt>
        <dd className="u-mono">{fmt(m.actualIfRequested)}</dd>
      </div>
      {m.actualIfAuthorized !== null && (
        <div>
          <dt>If authorized</dt>
          <dd className="u-mono">{fmt(m.actualIfAuthorized)}</dd>
        </div>
      )}
    </dl>
  );
}

/**
 * The execution gate, gate by gate.
 *
 * A gate that was never evaluated shows a dash, not a tick: "not checked" and
 * "checked and passed" are different facts and must not look alike.
 */
export function GateList({ gates }: { gates: { id: string; label: string; passed: boolean | null; detail: string }[] }) {
  return (
    <ol className="gates">
      {gates.map((gate) => (
        <li key={gate.id} className={cx('gates__row', gate.passed === false && 'gates__row--failed', gate.passed === null && 'gates__row--skipped')}>
          <span className={cx('gates__icon', gate.passed === true ? 'is-ok' : gate.passed === false ? 'is-danger' : 'is-neutral')}>
            {gate.passed === true ? (
              <Check size={12} aria-hidden="true" />
            ) : gate.passed === false ? (
              <X size={12} aria-hidden="true" />
            ) : (
              <Minus size={12} aria-hidden="true" />
            )}
          </span>
          <div className="gates__body">
            <span className="gates__label">
              {gate.label}
              <span className="u-sr-only">
                {gate.passed === true ? ' — passed' : gate.passed === false ? ' — failed' : ' — not evaluated'}
              </span>
            </span>
            <span className="gates__detail">{gate.detail}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

/** Renders a sanitised audit payload as a readable key/value block. */
export function PayloadTable({ payload }: { payload: Record<string, unknown> }) {
  const entries = Object.entries(payload);
  if (entries.length === 0) return <p className="u-dim">No payload recorded.</p>;
  return (
    <dl className="payload">
      {entries.map(([key, value]) => (
        <div key={key} className="payload__row">
          <dt>{humanise(key)}</dt>
          <dd className="u-mono">{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
