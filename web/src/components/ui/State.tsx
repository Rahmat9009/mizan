import type { ReactNode } from 'react';
import { UNAVAILABLE } from '@/lib/format';

/** Placeholder shown while an adapter call is in flight. */
export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="statebox" role="status" aria-live="polite">
      <span className="statebox__pulse" aria-hidden="true" />
      <span>{label}…</span>
    </div>
  );
}

/** Shown when a read fails. It names the failure rather than blaming the user. */
export function LoadError({ error }: { error: Error }) {
  return (
    <div className="statebox statebox--error" role="alert">
      <strong>This view could not load.</strong>
      <span>{error.message}</span>
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="statebox statebox--empty">
      <strong>{title}</strong>
      {hint && <span>{hint}</span>}
    </div>
  );
}

/**
 * The one way an absent value is rendered.
 *
 * `reason` explains why the value is missing, which is the difference between
 * a gap in the data and a value that happens to be zero.
 */
export function Unavailable({ reason }: { reason?: string }) {
  return (
    <span className="u-unavailable" title={reason}>
      {UNAVAILABLE}
    </span>
  );
}
