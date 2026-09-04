import { cx, percent, UNAVAILABLE } from '@/lib/format';
import type { Tone } from './Badge';

interface MeterProps {
  /** 0–1. Values above 1 render a breach segment rather than clipping. */
  value: number | null;
  label: string;
  limitText: string;
  currentText: string;
  tone?: Tone;
  /** Shown when the backend cannot compute a utilisation. */
  unavailableNote?: string;
}

/**
 * Policy utilisation.
 *
 * A meter that cannot be computed says so instead of drawing an empty bar,
 * because an empty bar reads as "zero risk" rather than "not measured".
 */
export function Meter({ value, label, limitText, currentText, tone = 'accent', unavailableNote }: MeterProps) {
  const breach = value !== null && value > 1;
  const fill = value === null ? 0 : Math.min(value, 1);
  const overflow = breach ? Math.min(value - 1, 1) : 0;
  const effectiveTone: Tone = breach ? 'danger' : tone;

  return (
    <div className="meter">
      <div className="meter__row">
        <span className="meter__label">{label}</span>
        <span className="meter__value">{currentText}</span>
      </div>
      {value === null ? (
        <p className="meter__unavailable">
          <span className="u-unavailable">{UNAVAILABLE}</span>
          {unavailableNote ? ` — ${unavailableNote}` : ''}
        </p>
      ) : (
        <div
          className="meter__track"
          role="meter"
          aria-valuenow={Math.round(value * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${label}: ${percent(value, 0)} of policy`}
        >
          <div className={cx('meter__fill', `meter__fill--${effectiveTone}`)} style={{ width: `${fill * 100}%` }} />
          {breach && <div className="meter__overflow" style={{ width: `${overflow * 100}%` }} />}
        </div>
      )}
      <div className="meter__row meter__row--foot">
        <span className="u-dim">{limitText}</span>
        <span className={cx('meter__pct', breach && 'u-neg')}>
          {value === null ? '' : `${percent(value, 0)} of limit`}
        </span>
      </div>
    </div>
  );
}
