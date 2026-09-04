import { decimalCompare, decimalFixed, decimalPercent, decimalToRatio } from '@/lib/decimal';
import type { Decimal } from '@/lib/decimal';
import { cx } from '@/lib/format';
import type { CheckUnit } from '@/types/domain';

/**
 * The calibrated scale.
 *
 * One axis, one limit, several readings. It replaces the pattern of stacking
 * independent bars each carrying a private threshold marker: three bars with
 * three markers read as three unrelated charts, and a reader has to verify that
 * the markers are at the same place before the comparison means anything.
 *
 * Here the limit is a single rule drawn through every reading, and the region
 * beyond it is a hatched zone rather than a line. "Past the limit" becomes a
 * *place on the instrument* instead of a per-row event, so the picture the
 * product exists to show — the requested reading standing in the hatched zone,
 * the authorized reading standing outside it — resolves in one glance and
 * survives greyscale.
 *
 * Every printed figure comes from its decimal string. The floats here position
 * geometry and are never displayed.
 */

export type ReadingKind = 'current' | 'requested' | 'authorized';

export interface Reading {
  kind: ReadingKind;
  /** Row label, e.g. "If requested 40". */
  label: string;
  value: Decimal;
}

interface ScaleProps {
  unit: CheckUnit;
  bound: 'ceiling' | 'floor';
  threshold: Decimal;
  readings: Reading[];
  /** Overrides the default "limit"/"minimum" wording. */
  boundLabel?: string;
  /** Width of the label gutter. `sm` suits dense panels. */
  size?: 'md' | 'sm';
  className?: string;
}

/**
 * Shape carries the reading's role alongside its position, so the three
 * readings stay distinguishable without colour — the same rule the verdict
 * glyphs follow.
 */
const KIND_GLYPH: Record<ReadingKind, string> = {
  current: '─',
  requested: '◆',
  authorized: '●',
};

/**
 * The limit always stands at the same place on the instrument.
 *
 * This is the axis's one rule and it exists to prevent an active misreading.
 * If each instrument scaled to its own largest value, two checks stacked in the
 * same panel would draw 18.3% against a 12.5% ceiling *shorter* than 12.2%
 * against a 10.0% ceiling — the reader would compare the pictures and reach the
 * opposite of the truth. Anchoring every limit at one x makes the axis read
 * "distance from your own ceiling", which is the comparison that is actually
 * meaningful across unlike units, and lets a single limit rule run through a
 * whole panel of checks. The exact figure is printed beside every reading, so
 * nothing is lost to the normalisation.
 */
const LIMIT_X = 58;

export function Scale({
  unit,
  bound,
  threshold,
  readings,
  boundLabel,
  size = 'md',
  className,
}: ScaleProps) {
  const limit = decimalToRatio(threshold);
  const values = readings.map((r) => decimalToRatio(r.value));
  const negatives = values.some((n) => n !== null && n < 0) || (limit !== null && limit < 0);

  const limitPct = LIMIT_X;
  const word = boundLabel ?? (bound === 'ceiling' ? 'limit' : 'minimum');

  /* A scale that cannot be drawn honestly is printed as figures instead of
     being forced onto an axis that would misrepresent it. */
  if (negatives || limit === null || limit <= 0) {
    return (
      <ul className={cx('scale', 'scale--plain', `scale--${size}`, className)}>
        {readings.map((r) => (
          <li key={r.label} className="scale__row">
            <span className="scale__rowlabel">
              <span className="scale__glyph" aria-hidden="true">
                {KIND_GLYPH[r.kind]}
              </span>
              {r.label}
            </span>
            <span className="scale__plainvalue u-mono">{format(r.value, unit)}</span>
            <Verdict ok={satisfies(r.value, threshold, bound)} />
          </li>
        ))}
        <li className="scale__row scale__row--limit">
          <span className="scale__rowlabel">{word}</span>
          <span className="scale__plainvalue u-mono">{format(threshold, unit)}</span>
        </li>
      </ul>
    );
  }

  return (
    <div className={cx('scale', `scale--${size}`, className)}>
      <div
        className="scale__body"
        style={{
          ['--limit' as string]: `${limitPct}%`,
          /* Unitless so the zone and the limit rule can be positioned against
             the track column rather than the whole instrument. */
          ['--limit-f' as string]: `${limitPct / 100}`,
        }}
      >
        {/* The limit is named once, at the head of the instrument, so no reading
            can ever collide with it. Dedicated marker lane above readings. */}
        <p className="scale__head" aria-label={`${word} threshold: ${format(threshold, unit)}`}>
          <span className={cx('scale__limitlabel', 'u-mono', limitPct > 62 && 'is-inside')}>
            <span className="scale__pill">
              <span className="scale__limitword">{word}</span>
              <strong className="scale__limitval">{format(threshold, unit)}</strong>
            </span>
          </span>
        </p>

        {/* The zone policy does not permit, drawn once behind every reading. */}
        <span
          className={cx('scale__zone', bound === 'ceiling' ? 'scale__zone--above' : 'scale__zone--below')}
          aria-hidden="true"
        />
        <span className="scale__limit" aria-hidden="true">
          <span className="scale__limitline" />
        </span>

        <ul className="scale__rows">
          {readings.map((r) => {
            const ok = satisfies(r.value, threshold, bound);
            /* Position is the reading as a fraction of its own limit, so the
               limit rule is common to every instrument in the panel. */
            const pct = Math.min(((decimalToRatio(r.value) ?? 0) / limit) * LIMIT_X, 100);
            return (
              <li key={r.label} className="scale__row">
                <span className="scale__rowlabel">
                  <span className="scale__glyph" aria-hidden="true">
                    {KIND_GLYPH[r.kind]}
                  </span>
                  {r.label}
                </span>
                <span className="scale__track">
                  <span
                    className={cx('scale__stem', ok ? 'is-ok' : 'is-over')}
                    style={{ width: `${pct}%` }}
                  >
                    <span className={cx('scale__mark', `scale__mark--${r.kind}`)} aria-hidden="true" />
                  </span>
                </span>
                <span className="scale__value u-mono">{format(r.value, unit)}</span>
                <Verdict ok={ok} />
              </li>
            );
          })}
        </ul>

        <p className="scale__axis">
          <span className="scale__axisline" aria-hidden="true" />
          <span className="scale__zero u-mono" aria-hidden="true">
            0
          </span>
          <span className="scale__axisnote">
            measured against this check&rsquo;s own {word}
          </span>
        </p>
      </div>
    </div>
  );
}

function Verdict({ ok }: { ok: boolean }) {
  return (
    <span className={cx('scale__verdict', ok ? 'is-ok' : 'is-bad')}>
      <span aria-hidden="true">{ok ? '✓' : '✕'}</span>
      <span className="u-sr-only">{ok ? 'within limit' : 'outside limit'}</span>
    </span>
  );
}

function satisfies(value: Decimal, threshold: Decimal, bound: 'ceiling' | 'floor'): boolean {
  const cmp = decimalCompare(value, threshold);
  return bound === 'ceiling' ? cmp <= 0 : cmp >= 0;
}

export function format(value: Decimal, unit: CheckUnit): string {
  switch (unit) {
    case 'ratio':
      return decimalPercent(value) ?? value;
    case 'days':
      return `${decimalFixed(value, 0) ?? value} days`;
    case 'count':
      return decimalFixed(value, 0) ?? value;
    case 'currency':
      return decimalFixed(value, 2) ?? value;
  }
}
