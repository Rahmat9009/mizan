import { AGENTS, CONNECTIONS, NOTIFICATIONS, SYSTEM_HEALTH } from '@/data/agents';
import { AUDIT_DESC } from '@/data/audit';
import { CROWDING, GOVERNANCE_DAY, RESPONSE_STATE } from '@/data/governance';
import { ORDERS } from '@/data/orders';
import { PORTFOLIO_SUMMARY, POSITIONS } from '@/data/portfolio';
import { PROPOSALS } from '@/data/proposals';
import { INTERVENTIONS, POLICY_LIMITS, RISK_ALERTS, SAFETY_CONTROLS } from '@/data/risk';
import type { AuditFilter, GovernorApi } from './types';

/**
 * Mock implementation of `GovernorApi`.
 *
 * A small artificial delay is kept so loading states are exercised during
 * development rather than only appearing for the first time against a real
 * backend.
 */

const LATENCY_MS = 90;

function resolve<T>(value: T): Promise<T> {
  return new Promise((r) => setTimeout(() => r(value), LATENCY_MS));
}

function matchesFilter(
  event: (typeof AUDIT_DESC)[number],
  filter: AuditFilter | undefined,
): boolean {
  if (!filter) return true;
  if (filter.proposalId && event.proposalId !== filter.proposalId) return false;
  if (filter.symbol && event.symbol !== filter.symbol) return false;
  if (filter.actor && event.actor !== filter.actor) return false;
  if (filter.outcome && event.outcome !== filter.outcome) return false;
  if (filter.query) {
    const needle = filter.query.toLowerCase();
    const hay = `${event.summary} ${event.action} ${event.symbol} ${event.proposalId}`.toLowerCase();
    if (!hay.includes(needle)) return false;
  }
  return true;
}

export function createMockClient(): GovernorApi {
  return {
    getPortfolioSummary: () => resolve(PORTFOLIO_SUMMARY),
    getPositions: () => resolve(POSITIONS),

    listProposals: () => resolve(PROPOSALS),
    getProposal: (id) => resolve(PROPOSALS.find((p) => p.proposalId === id) ?? null),

    listOrders: () => resolve(ORDERS),
    getOrder: (id) => resolve(ORDERS.find((o) => o.clientOrderId === id) ?? null),

    listAuditEvents: (filter) => resolve(AUDIT_DESC.filter((e) => matchesFilter(e, filter))),
    getProposalAudit: (proposalId) =>
      resolve(
        [...AUDIT_DESC]
          .filter((e) => e.proposalId === proposalId)
          .sort((a, b) => a.at.localeCompare(b.at)),
      ),

    getPolicyLimits: () => resolve(POLICY_LIMITS),
    getInterventions: () => resolve(INTERVENTIONS),
    getRiskAlerts: () => resolve(RISK_ALERTS),
    getSafetyControls: () => resolve(SAFETY_CONTROLS),
    setKillSwitch: (active) => resolve({ ...SAFETY_CONTROLS, killSwitch: active }),

    getGovernanceDay: () => resolve(GOVERNANCE_DAY),
    getResponseState: () => resolve(RESPONSE_STATE),
    getCrowding: () => resolve(CROWDING),

    listAgents: () => resolve(AGENTS),
    listConnections: () => resolve(CONNECTIONS),
    getSystemHealth: () => resolve(SYSTEM_HEALTH),
    listNotifications: () => resolve(NOTIFICATIONS),
  };
}
