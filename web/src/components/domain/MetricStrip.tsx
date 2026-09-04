import type { ReactNode } from 'react';
import { cx } from '@/lib/format';
import type { Provenance } from '@/types/domain';
import { ProvenanceBadge } from './ProvenanceBadge';

export interface MetricCell {
  label: string;
  value: ReactNode;
  /** Secondary line: a delta, a limit, or the reason a value is unavailable. */
  note?: ReactNode;
  tone?: 'neutral' | 'ok' | 'warn' | 'danger';
  provenance?: Provenance;
  /** Wider cell for the two control cells at the end of the strip. */
  span?: 1 | 2;
}

/**
 * The instrument cluster at the top of an operational page.
 *
 * Deliberately one bordered strip divided by hairlines rather than a row of
 * floating cards: these values are read together, and separate cards imply
 * they are separate concerns.
 */
export function MetricStrip({ cells, className }: { cells: MetricCell[]; className?: string }) {
  return (
    <div className={cx('metrics', className)}>
      {cells.map((cell) => (
        <div
          key={cell.label}
          className={cx('metrics__cell', cell.tone && cell.tone !== 'neutral' && `is-${cell.tone}`, cell.span === 2 && 'metrics__cell--wide')}
        >
          <div className="metrics__label">
            <span>{cell.label}</span>
            {cell.provenance && <ProvenanceBadge value={cell.provenance} size="xs" />}
          </div>
          <div className="metrics__value">{cell.value}</div>
          {cell.note && <div className="metrics__note">{cell.note}</div>}
        </div>
      ))}
    </div>
  );
}
