import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { RESPONSE_STATE } from '@/data/governance';
import { api, API_MODE } from '@/services/api';
import type { AutonomyMode, ResponseLevel } from '@/types/domain';

/**
 * Interface preferences and the operator controls that the shell keeps visible.
 *
 * The safety toggles here are local UI state in Step 2. They are modelled as
 * one context so Step 3 can back them with the backend's real execution
 * configuration without changing any consumer.
 */

export type Theme = 'dark' | 'light';
export type Density = 'comfortable' | 'compact';

interface AppState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;

  density: Density;
  setDensity: (density: Density) => void;

  autonomy: AutonomyMode;
  setAutonomy: (mode: AutonomyMode) => void;

  /**
   * Where the graduated response ladder sits, 0 (normal) to 5 (full stop).
   * This is the source of truth; `killSwitch` is the level-5 view of it.
   */
  responseLevel: ResponseLevel;
  setResponseLevel: (level: ResponseLevel) => void;
  /** When the current level was engaged, and by whom. */
  responseSince: string;
  responseEngagedBy: string | null;

  killSwitch: boolean;
  setKillSwitch: (on: boolean) => void;

  executionEnabled: boolean;
  setExecutionEnabled: (on: boolean) => void;

  dryRun: boolean;
  setDryRun: (on: boolean) => void;

  paletteOpen: boolean;
  setPaletteOpen: (open: boolean) => void;
}

const AppContext = createContext<AppState | null>(null);

const THEME_KEY = 'governor.theme';
const DENSITY_KEY = 'governor.density';

/**
 * The signed-in operator, for stamping escalations.
 *
 * A placeholder until authentication exists. It is deliberately a real-looking
 * identity rather than "you", because the record has to name someone.
 */
const OPERATOR = 'operator@desk';

function readStored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return allowed.includes(raw as T) ? (raw as T) : fallback;
  } catch {
    return fallback;
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => readStored(THEME_KEY, ['dark', 'light'] as const, 'dark'));
  const [density, setDensityState] = useState<Density>(() =>
    readStored(DENSITY_KEY, ['comfortable', 'compact'] as const, 'comfortable'),
  );
  const [autonomy, setAutonomyState] = useState<AutonomyMode>(API_MODE === 'http' ? 'OBSERVE' : 'MANUAL');
  const [responseLevel, setResponseLevelState] = useState<ResponseLevel>(API_MODE === 'http' ? 0 : RESPONSE_STATE.level);
  const [responseSince, setResponseSince] = useState(API_MODE === 'http' ? new Date(0).toISOString() : RESPONSE_STATE.since);
  const [responseEngagedBy, setResponseEngagedBy] = useState<string | null>(API_MODE === 'http' ? null : RESPONSE_STATE.engagedBy);
  const [executionEnabled, setExecutionEnabledState] = useState(API_MODE !== 'http');
  const [dryRun, setDryRunState] = useState(API_MODE === 'http');
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Storage unavailable (private window, blocked site data). The choice
      // still applies for this session.
    }
  }, [theme]);

  useEffect(() => {
    document.documentElement.dataset.density = density;
    try {
      localStorage.setItem(DENSITY_KEY, density);
    } catch {
      // As above.
    }
  }, [density]);

  useEffect(() => {
    if (API_MODE !== 'http') return;
    void Promise.all([api.getSafetyControls(), api.getResponseState()]).then(([controls, response]) => {
      setAutonomyState(controls.autonomy);
      setExecutionEnabledState(controls.executionEnabled);
      setDryRunState(controls.dryRun);
      setResponseLevelState(response.level);
      setResponseSince(response.since);
      setResponseEngagedBy(response.engagedBy);
    });
  }, []);

  const setTheme = useCallback((next: Theme) => setThemeState(next), []);
  const toggleTheme = useCallback(() => setThemeState((t) => (t === 'dark' ? 'light' : 'dark')), []);
  const setDensity = useCallback((next: Density) => setDensityState(next), []);

  /**
   * Changing the level stamps who changed it and when.
   *
   * The ladder has to render as state, not only as a control: "FULL STOP
   * ACTIVE, engaged 14:32:09 by rahmat@…" is the thing an operator needs to
   * read on arriving at a screen, and it cannot be reconstructed from a
   * boolean.
   */
  const setResponseLevel = useCallback((level: ResponseLevel) => {
    if (API_MODE === 'http') {
      // The production API exposes the full-stop switch, not arbitrary response-level mutation.
      if (level !== 0 && level !== 5) return;
      void api.setKillSwitch(level === 5).then((controls) => {
        setResponseLevelState(controls.killSwitch ? 5 : 0);
        setResponseSince(new Date().toISOString());
        setResponseEngagedBy(null);
      });
      return;
    }
    setResponseLevelState((current) => {
      if (current === level) return current;
      setResponseSince(new Date().toISOString());
      setResponseEngagedBy(level === 0 ? null : OPERATOR);
      return level;
    });
  }, []);

  const killSwitch = responseLevel === 5;
  const setKillSwitch = useCallback(
    (on: boolean) => setResponseLevel(on ? 5 : 0),
    [setResponseLevel],
  );
  const setAutonomy = useCallback((next: AutonomyMode) => {
    if (API_MODE === 'mock') setAutonomyState(next);
  }, []);
  const setExecutionEnabled = useCallback((next: boolean) => {
    if (API_MODE === 'mock') setExecutionEnabledState(next);
  }, []);
  const setDryRun = useCallback((next: boolean) => {
    if (API_MODE === 'mock') setDryRunState(next);
  }, []);

  const value = useMemo<AppState>(
    () => ({
      theme,
      setTheme,
      toggleTheme,
      density,
      setDensity,
      autonomy,
      setAutonomy,
      responseLevel,
      setResponseLevel,
      responseSince,
      responseEngagedBy,
      killSwitch,
      setKillSwitch,
      executionEnabled,
      setExecutionEnabled,
      dryRun,
      setDryRun,
      paletteOpen,
      setPaletteOpen,
    }),
    [
      theme,
      setTheme,
      toggleTheme,
      density,
      setDensity,
      autonomy,
      setAutonomy,
      responseLevel,
      setResponseLevel,
      responseSince,
      responseEngagedBy,
      killSwitch,
      setKillSwitch,
      executionEnabled,
      setExecutionEnabled,
      dryRun,
      setDryRun,
      paletteOpen,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp must be used inside AppProvider');
  return ctx;
}

export const AUTONOMY_LABEL: Record<AutonomyMode, string> = {
  OBSERVE: 'Observe',
  MANUAL: 'Manual approval',
  AUTONOMOUS_PAPER: 'Autonomous paper',
};

export const AUTONOMY_BLURB: Record<AutonomyMode, string> = {
  OBSERVE: 'Evaluate and record. Nothing is ever sent to the broker.',
  MANUAL: 'The Governor decides the size. A person confirms execution.',
  AUTONOMOUS_PAPER:
    'Authorized trades proceed on their own, still through every deterministic gate and still paper only.',
};
