import { createHttpClient } from './httpClient';
import { createMockClient } from './mockClient';
import type { ApiMode, GovernorApi } from './types';

/**
 * The single place the data source is chosen.
 *
 * `VITE_API_MODE=http` points the app at the FastAPI backend through the dev
 * proxy; anything else uses the demo dataset. Components import `api` and stay
 * unaware of which one they got.
 */

const configuredMode = import.meta.env.VITE_API_MODE ?? 'mock';
if (configuredMode !== 'mock' && configuredMode !== 'http') {
  throw new Error(`Unsupported VITE_API_MODE=${configuredMode}. Expected "mock" or "http".`);
}
const mode: ApiMode = configuredMode;
const baseUrl: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1';

export const API_MODE = mode;
export const API_BASE_URL = baseUrl;

export const api: GovernorApi = mode === 'http' ? createHttpClient(baseUrl) : createMockClient();

export type { GovernorApi } from './types';
export { ApiError } from './httpClient';
