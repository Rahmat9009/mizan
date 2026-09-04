/**
 * A fixed demo session clock.
 *
 * Mock timestamps are anchored rather than generated from `Date.now()` so the
 * audit timeline, order lifecycle and pipeline all agree with each other and
 * screenshots stay reproducible.
 */

/** 2026-09-02, 14:31 UTC — mid-session on a US trading day. */
export const SESSION_ANCHOR = Date.parse('2026-09-02T14:31:00.000Z');

/** ISO timestamp `minutes` before the session anchor. */
export function ago(minutes: number, seconds = 0): string {
  return new Date(SESSION_ANCHOR - minutes * 60_000 - seconds * 1000).toISOString();
}

/** ISO timestamp `minutes` after the session anchor. */
export function ahead(minutes: number): string {
  return new Date(SESSION_ANCHOR + minutes * 60_000).toISOString();
}
