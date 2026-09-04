import type { CrowdingReport, GovernanceDay, ResponseLevel, ResponseState } from '@/types/domain';
import { ago } from './clock';

/**
 * Session-level governance state: the quiet-day summary, the response ladder,
 * and aggregate agent crowding.
 *
 * All DEMO data. The crowding figures are derived from the same positions the
 * portfolio view shows, so the two never contradict each other.
 */

/* ------------------------------------------------------------- the quiet day */

/**
 * The boring day, stated as what was stopped.
 *
 * Ninety-nine percent of the time nothing is wrong, and a governance product
 * that only looks alive during an incident stops being opened. The number that
 * matters here is the exposure that never reached the broker.
 *
 * This aggregates every decision governed in the session. The proposal ledger
 * shows the most recent handful of them, which is why the counts differ.
 */
export const GOVERNANCE_DAY: GovernanceDay = {
  date: '2026-09-02',
  decisionsGoverned: 1842,
  approved: 1737,
  reduced: 74,
  rejected: 31,
  requestedNotional: '4820140.00',
  authorizedNotional: '3170980.00',
  preventedNotional: '1649160.00',
  chainVerified: 1842,
  chainTotal: 1842,
  needsAttention: 0,
  provenance: 'DEMO',
};

/* --------------------------------------------------------- the response ladder */

/**
 * Six levels, not a switch.
 *
 * A binary kill switch cannot express "tighten but keep trading", which is the
 * state a desk actually spends its bad afternoons in. Level 5 is the full stop.
 */
export const RESPONSE_LEVELS: Record<
  ResponseLevel,
  { name: string; effect: string; tone: 'ok' | 'warn' | 'danger'; glyph: string }
> = {
  0: {
    name: 'NORMAL',
    effect: 'Every policy at its configured threshold. Agents operate within their limits.',
    tone: 'ok',
    glyph: '●',
  },
  1: {
    name: 'ELEVATED',
    effect: 'Monitoring is heightened and every intervention is notified. No limit changes.',
    tone: 'ok',
    glyph: '●',
  },
  2: {
    name: 'RESTRICTED',
    effect: 'Position and trade ceilings are tightened. Existing authorizations stand.',
    tone: 'warn',
    glyph: '◆',
  },
  3: {
    name: 'REDUCE ONLY',
    effect: 'New exposure is refused. Trades that reduce an existing position are still authorized.',
    tone: 'warn',
    glyph: '◆',
  },
  4: {
    name: 'HALT NEW',
    effect: 'No new authorization is issued. Outstanding authorizations may still be used.',
    tone: 'danger',
    glyph: '■',
  },
  5: {
    name: 'FULL STOP',
    effect: 'Every submission is refused, every outstanding authorization is void, all agents are halted.',
    tone: 'danger',
    glyph: '✕',
  },
};

export const RESPONSE_STATE: ResponseState = {
  level: 0,
  since: ago(392),
  engagedBy: null,
  agentsHalted: 0,
  agentsActive: 8,
  note: 'No escalation has been required in this session.',
};

/* ---------------------------------------------------------------- crowding */

/**
 * Aggregate exposure across agent strategies.
 *
 * The sentence this screen exists to make true: *each agent is within its
 * individual limits.* Every deterministic check below passes. The book still
 * carries more of one theme than portfolio guidance allows, and no per-trade
 * rule can see that, because no per-trade rule looks at the other agents.
 *
 * Weights are shares of account equity and match `POSITIONS` exactly.
 */
export const CROWDING: CrowdingReport = {
  status: 'ELEVATED',
  agentsTotal: 5,
  agentsCorrelated: 3,
  clusters: [
    {
      id: 'ai-datacenter',
      label: 'AI datacenter demand',
      exposure: '0.158',
      guidance: '0.150',
      breached: true,
      members: [
        { agentId: 'revision-momentum', symbol: 'NVDA', weight: '0.0609', withinIndividualLimits: true },
        { agentId: 'revision-momentum', symbol: 'AMD', weight: '0.0607', withinIndividualLimits: true },
        {
          agentId: 'cloud-backlog',
          symbol: 'MSFT 520/530C',
          weight: '0.0364',
          withinIndividualLimits: true,
        },
      ],
      projected: {
        label: 'if the AMD proposal in review is authorized at 30 shares',
        exposure: '0.243',
      },
    },
    {
      id: 'megacap-platform',
      label: 'Mega-cap platform',
      exposure: '0.223',
      guidance: '0.250',
      breached: false,
      members: [
        { agentId: 'installed-base-upgrade', symbol: 'AAPL', weight: '0.1423', withinIndividualLimits: true },
        { agentId: 'search-monetisation', symbol: 'GOOGL', weight: '0.0805', withinIndividualLimits: true },
      ],
      projected: null,
    },
  ],
  modelConcentration: {
    label: 'One contextual review model',
    share: '0.82',
    detail: '82% of authorized exposure was reviewed by a single model. A fault in it is not independent across agents.',
  },
  signalConcentration: {
    label: 'One upstream screen',
    share: '0.64',
    detail: '64% of the correlated exposure originates from the revision-momentum screen.',
  },
  /* No volume feed is connected, so time-to-unwind cannot be computed. It reads
     Unavailable rather than being estimated into existence. */
  unwindDays: null,
  provenance: 'DEMO',
};
