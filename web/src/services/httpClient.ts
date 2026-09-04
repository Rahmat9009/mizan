import type { GovernorApi } from './types';
import type {
  BackendChainVerification,
  BackendDecisionList,
  BackendDecisionRecord,
  BackendHealth,
  BackendPolicy,
} from './backendTypes';
import {
  filterAudit,
  findOrder,
  findProposal,
  mapAgents,
  mapAudit,
  mapConnections,
  mapCrowding,
  mapDecision,
  mapGovernanceDay,
  mapInterventions,
  mapNotifications,
  mapOrders,
  mapPolicy,
  mapPortfolio,
  mapPositions,
  mapResponse,
  mapRiskAlerts,
  mapSafety,
  mapSystemHealth,
} from './mappers';

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function request<T>(baseUrl: string, path: string, requestInit: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...requestInit,
    headers: {
      accept: 'application/json',
      ...(requestInit.body ? { 'content-type': 'application/json' } : {}),
      ...(requestInit.headers ?? {}),
    },
  });

  if (!response.ok) {
    let code = 'http_error';
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as {
        error?: { code?: string; message?: string };
        code?: string;
        message?: string;
      };
      code = body.error?.code ?? body.code ?? code;
      message = body.error?.message ?? body.message ?? message;
    } catch {
      // Non-JSON error body. The status line remains the safest available detail.
    }
    throw new ApiError(code, message, response.status);
  }

  return (await response.json()) as T;
}

export function createHttpClient(baseUrl: string): GovernorApi {
  const get = <T>(path: string) => request<T>(baseUrl, path);
  const post = <T>(path: string, body: unknown) =>
    request<T>(baseUrl, path, { method: 'POST', body: JSON.stringify(body) });

  let recordsPromise: Promise<BackendDecisionRecord[]> | null = null;
  let verifyPromise: Promise<BackendChainVerification> | null = null;
  let healthPromise: Promise<BackendHealth> | null = null;
  let policyPromise: Promise<BackendPolicy> | null = null;

  const records = () => {
    if (!recordsPromise) {
      recordsPromise = get<BackendDecisionList>('/decisions?limit=200').then(({ decisions }) =>
        Promise.all(decisions.map((decision) => get<BackendDecisionRecord>(`/decisions/${decision.decision_id}`))),
      );
    }
    return recordsPromise;
  };
  const verify = () => (verifyPromise ??= get<BackendChainVerification>('/audit/verify'));
  const health = () => (healthPromise ??= get<BackendHealth>('/health'));
  const policy = () => (policyPromise ??= get<BackendPolicy>('/policy'));

  return {
    getPortfolioSummary: async () => mapPortfolio(await records()),
    getPositions: async () => mapPositions(await records()),
    listProposals: async () => {
      const [all, chain] = await Promise.all([records(), verify()]);
      return all.map((record) => mapDecision(record, chain));
    },
    getProposal: async (id) => findProposal(await records(), await verify(), id),
    listOrders: async () => mapOrders(await records()),
    getOrder: async (id) => findOrder(await records(), id),
    listAuditEvents: async (filter) => filterAudit(mapAudit(await records()), filter),
    getProposalAudit: async (proposalId) => filterAudit(mapAudit(await records()), { proposalId }),
    getPolicyLimits: async () => mapPolicy(await policy()),
    getInterventions: async () => mapInterventions(await records()),
    getRiskAlerts: async () => mapRiskAlerts(await records()),
    getSafetyControls: async () => mapSafety(await health()),
    setKillSwitch: async (active) => {
      await post<{ kill_switch: { active: boolean } }>('/control/kill-switch', { active });
      healthPromise = null;
      return mapSafety(await health());
    },
    getGovernanceDay: async () => mapGovernanceDay(await records(), await verify()),
    getResponseState: async () => mapResponse(await records(), await health()),
    getCrowding: async () => mapCrowding(await records()),
    listAgents: async () => mapAgents(await records()),
    listConnections: async () => mapConnections(await health(), await verify()),
    getSystemHealth: async () => mapSystemHealth(await health(), await verify(), await records()),
    listNotifications: async () => mapNotifications(await records()),
  };
}
