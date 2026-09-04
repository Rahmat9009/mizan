import type { PolicyLimit } from '@/types/domain';

export interface PolicyTriage {
  total: number;
  breachCount: number;
  watchCount: number;
  withinLimitCount: number;
  unavailableCount: number;
  tightest: PolicyLimit | null;
}

/**
 * Single source of truth for policy limit triage and posture.
 * Derives status counts and tightest constraint from the exact same
 * PolicyLimit rows rendered in the control tables.
 */
export function derivePolicyTriage(limits: PolicyLimit[]): PolicyTriage {
  let breachCount = 0;
  let watchCount = 0;
  let withinLimitCount = 0;
  let unavailableCount = 0;

  for (const limit of limits) {
    switch (limit.status) {
      case 'BREACH':
        breachCount++;
        break;
      case 'WATCH':
        watchCount++;
        break;
      case 'UNAVAILABLE':
        unavailableCount++;
        break;
      case 'OK':
      default:
        withinLimitCount++;
        break;
    }
  }

  // Tightest constraint: highest utilisation amongst policies with a measurable budget
  const measurable = limits.filter((l) => l.utilisation !== null && Number.isFinite(l.utilisation));
  const tightest = measurable.length > 0
    ? [...measurable].sort((a, b) => (b.utilisation ?? 0) - (a.utilisation ?? 0))[0]
    : null;

  return {
    total: limits.length,
    breachCount,
    watchCount,
    withinLimitCount,
    unavailableCount,
    tightest,
  };
}

/**
 * Breach-first comparator for policy limits.
 * Prioritizes urgent operational states:
 *   1. BREACH
 *   2. WATCH
 *   3. OK (within limit)
 *   4. UNAVAILABLE (not measurable)
 * Secondary sort preserves highest utilisation first.
 */
const STATUS_PRIORITY: Record<PolicyLimit['status'], number> = {
  BREACH: 0,
  WATCH: 1,
  OK: 2,
  UNAVAILABLE: 3,
};

export function comparePolicyBreachFirst(a: PolicyLimit, b: PolicyLimit): number {
  const pA = STATUS_PRIORITY[a.status] ?? 2;
  const pB = STATUS_PRIORITY[b.status] ?? 2;
  if (pA !== pB) {
    return pA - pB;
  }
  // If both have utilisation, highest utilisation first
  if (a.utilisation !== null && b.utilisation !== null) {
    return b.utilisation - a.utilisation;
  }
  if (a.utilisation !== null) return -1;
  if (b.utilisation !== null) return 1;
  return a.label.localeCompare(b.label);
}
