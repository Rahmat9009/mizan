import type { ReactNode } from 'react';
import { cx } from '@/lib/format';

export type Tone = 'ok' | 'warn' | 'danger' | 'neutral' | 'accent' | 'brass' | 'paper';

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  /**
   * Shape carries the same information as colour, so a badge stays readable
   * when colour is unavailable to the reader.
   */
  shape?: 'square' | 'round' | 'diamond' | 'bar' | 'none';
  /**
   * A literal marker character, used where a specific glyph carries the
   * meaning — ● approved, ▼ reduced, ✕ rejected. Overrides `shape`.
   */
  glyph?: string;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  title?: string;
}

/**
 * A status chip.
 *
 * Colour is never the only signal: every badge renders a text label, and the
 * marker shape differs per tone so the states remain distinguishable
 * without colour vision.
 */
export function Badge({ tone = 'neutral', children, shape, glyph, size = 'sm', className, title }: BadgeProps) {
  const marker = shape ?? DEFAULT_SHAPE[tone];
  return (
    <span className={cx('badge', `badge--${tone}`, `badge--${size}`, className)} title={title}>
      {glyph ? (
        <span className="badge__glyph" aria-hidden="true">
          {glyph}
        </span>
      ) : (
        marker !== 'none' && <i className={cx('badge__marker', `badge__marker--${marker}`)} aria-hidden="true" />
      )}
      <span className="badge__text">{children}</span>
    </span>
  );
}

const DEFAULT_SHAPE: Record<Tone, NonNullable<BadgeProps['shape']>> = {
  ok: 'round',
  warn: 'diamond',
  danger: 'square',
  neutral: 'bar',
  accent: 'round',
  brass: 'diamond',
  paper: 'diamond',
};

/** A bare status dot for dense table cells, always paired with adjacent text. */
export function StatusDot({ tone = 'neutral', label }: { tone?: Tone; label: string }) {
  return (
    <>
      <i className={cx('dot', `dot--${tone}`)} aria-hidden="true" />
      <span className="u-sr-only">{label}</span>
    </>
  );
}
