import type {
  AgentCardModel,
  AppNotification,
  AuditEvent,
  ConnectionState,
  CrowdingReport,
  GovernanceDay,
  Intervention,
  Order,
  PolicyLimit,
  PortfolioSummary,
  Position,
  Proposal,
  ResponseState,
  RiskAlert,
  SafetyControls,
  SystemHealth,
} from '@/types/domain';

/**
 * The only surface a component may talk to.
 *
 * Components never know whether a value arrived from `src/data` or from the
 * FastAPI backend. Step 3 swaps the implementation behind this interface and
 * nothing in `src/routes` or `src/components` changes.
 */
export interface GovernorApi {
  getPortfolioSummary(): Promise<PortfolioSummary>;
  getPositions(): Promise<Position[]>;

  listProposals(): Promise<Proposal[]>;
  getProposal(proposalId: string): Promise<Proposal | null>;

  listOrders(): Promise<Order[]>;
  getOrder(clientOrderId: string): Promise<Order | null>;

  listAuditEvents(filter?: AuditFilter): Promise<AuditEvent[]>;
  getProposalAudit(proposalId: string): Promise<AuditEvent[]>;

  getPolicyLimits(): Promise<PolicyLimit[]>;
  getInterventions(): Promise<Intervention[]>;
  getRiskAlerts(): Promise<RiskAlert[]>;
  getSafetyControls(): Promise<SafetyControls>;
  /** The only runtime safety control exposed by the authoritative API. */
  setKillSwitch(active: boolean): Promise<SafetyControls>;

  /** The quiet-state summary: what was governed today and what was stopped. */
  getGovernanceDay(): Promise<GovernanceDay>;
  /** Where the response ladder currently sits. */
  getResponseState(): Promise<ResponseState>;
  /** Aggregate exposure across agent strategies. */
  getCrowding(): Promise<CrowdingReport>;

  listAgents(): Promise<AgentCardModel[]>;
  listConnections(): Promise<ConnectionState[]>;
  getSystemHealth(): Promise<SystemHealth>;
  listNotifications(): Promise<AppNotification[]>;
}

export interface AuditFilter {
  proposalId?: string;
  symbol?: string;
  actor?: string;
  outcome?: AuditEvent['outcome'];
  query?: string;
}

/** Which implementation is live. Surfaced in Settings so it is never a guess. */
export type ApiMode = 'mock' | 'http';
