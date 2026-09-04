import type { Sourced } from '@/types/domain';

/** The single string used everywhere a value genuinely cannot be shown. */
export const UNAVAILABLE = 'Unavailable';

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

export function money(value: number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

/** Compact money for metric tiles: $59,950 rather than $59,950.00. */
export function moneyCompact(value: number | null | undefined): string {
  return money(value, 0);
}

export function signedMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${money(Math.abs(value))}`;
}

export function percent(value: number | null | undefined, fractionDigits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  return `${(value * 100).toFixed(fractionDigits)}%`;
}

export function signedPercent(value: number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${(Math.abs(value) * 100).toFixed(fractionDigits)}%`;
}

export function decimal(value: number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  return value.toFixed(fractionDigits);
}

export function integer(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return UNAVAILABLE;
  return value.toLocaleString('en-US');
}

export function latency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return UNAVAILABLE;
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** Reads a `Sourced` value through a formatter, honouring unavailability. */
export function fromSource<T>(
  source: Sourced<T>,
  format: (value: T) => string,
): { text: string; available: boolean } {
  if (source.value === null || source.value === undefined) {
    return { text: UNAVAILABLE, available: false };
  }
  return { text: format(source.value), available: true };
}

const TIME_FMT = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});

const DATE_TIME_FMT = new Intl.DateTimeFormat('en-US', {
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});

/** 14:31:00 — used inside a single session's views. */
export function timeOf(iso: string): string {
  return TIME_FMT.format(new Date(iso));
}

/** 02 Sep, 14:31:00 — used where events may span days. */
export function stampOf(iso: string): string {
  return DATE_TIME_FMT.format(new Date(iso)).replace(',', '');
}

/** "17m ago" relative to a reference instant. */
export function relative(iso: string, now: number): string {
  // The mock experience uses its fixed session clock. HTTP mode compares live
  // records with the actual clock so future/ago labels cannot drift with demo data.
  const reference = import.meta.env.VITE_API_MODE === 'http' ? Date.now() : now;
  const deltaMs = reference - Date.parse(iso);
  if (!Number.isFinite(deltaMs)) return UNAVAILABLE;
  const abs = Math.abs(deltaMs);
  const suffix = deltaMs >= 0 ? 'ago' : 'from now';
  const minutes = Math.round(abs / 60_000);
  if (abs < 60_000) return `${Math.round(abs / 1000)}s ${suffix}`;
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ${suffix}`;
  return `${Math.round(hours / 24)}d ${suffix}`;
}

/** Turns SNAKE_CASE and snake_case into readable words. */
export function humanise(token: string): string {
  return token
    .replace(/[_-]+/g, ' ')
    .toLowerCase()
    .replace(/^\w/, (c) => c.toUpperCase());
}

/** Shortens a long identifier for a dense cell without losing its ends. */
export function shortId(id: string, head = 10, tail = 4): string {
  if (id.length <= head + tail + 1) return id;
  // `slice(-0)` returns the whole string, so an empty tail is handled explicitly.
  return tail === 0 ? `${id.slice(0, head)}…` : `${id.slice(0, head)}…${id.slice(-tail)}`;
}
