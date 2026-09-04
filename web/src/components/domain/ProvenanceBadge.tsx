import { cx } from '@/lib/format';
import type { Provenance } from '@/types/domain';

/**
 * Where a value came from.
 *
 * Every number in this product that a reader could mistake for live market
 * truth carries one of these. The underlying AI infrastructure provider is
 * deliberately not a value here: that is an implementation detail and appears
 * only in Settings diagnostics.
 */

const META: Record<Provenance, { label: string; kind: 'live' | 'derived' | 'supplied' | 'demo'; title: string }> = {
  ALPACA_PAPER: {
    label: 'Alpaca Paper',
    kind: 'live',
    title: 'Read from the Alpaca paper brokerage account.',
  },
  LIVE_PORTFOLIO: {
    label: 'Live portfolio',
    kind: 'live',
    title: 'Computed from the current paper portfolio snapshot.',
  },
  LIVE_AGENT: {
    label: 'Live agent',
    kind: 'live',
    title: 'Produced by an upstream market-intelligence agent.',
  },
  MIZAN_LEDGER: {
    label: 'Mizan ledger',
    kind: 'live',
    title: 'Read from an immutable decision record in the Mizan audit ledger.',
  },
  MIZAN_POLICY: {
    label: 'Mizan policy',
    kind: 'derived',
    title: 'Read from the active versioned policy exposed by the Mizan API.',
  },
  AI_RISK_MODEL: {
    label: 'AI risk model',
    kind: 'derived',
    title: 'Contextual judgement from the AI risk review, not a measurement.',
  },
  CALLER_SUPPLIED: {
    label: 'Caller supplied',
    kind: 'supplied',
    title: 'Asserted by the upstream caller. This backend has no feed for it.',
  },
  DEMO: {
    label: 'Demo',
    kind: 'demo',
    title: 'Demonstration data. Not a broker or market value.',
  },
  SYNTHETIC: {
    label: 'Synthetic',
    kind: 'demo',
    title: 'Generated for illustration. Not observed anywhere.',
  },
};

export function ProvenanceBadge({
  value,
  size = 'sm',
  className,
}: {
  value: Provenance;
  size?: 'xs' | 'sm';
  className?: string;
}) {
  const meta = META[value];
  return (
    <span
      className={cx('prov', `prov--${meta.kind}`, `prov--${size}`, className)}
      title={meta.title}
      data-provenance={value}
    >
      <span className="u-sr-only">Source: </span>
      {meta.label}
    </span>
  );
}

export const PROVENANCE_LABEL = (value: Provenance) => META[value].label;
export const PROVENANCE_TITLE = (value: Provenance) => META[value].title;
