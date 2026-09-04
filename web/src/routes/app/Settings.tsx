import { Badge } from '@/components/ui/Badge';
import type { Tone } from '@/components/ui/Badge';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError } from '@/components/ui/State';
import { cx, stampOf } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api, API_BASE_URL, API_MODE } from '@/services/api';
import { AUTONOMY_BLURB, AUTONOMY_LABEL, useApp } from '@/state/app';
import type { AutonomyMode, ConnectionStatus } from '@/types/domain';

const CONNECTION_TONE: Record<ConnectionStatus, Tone> = {
  CONNECTED: 'ok',
  DEGRADED: 'warn',
  NOT_CONFIGURED: 'neutral',
  ERROR: 'danger',
};

const CONNECTION_LABEL: Record<ConnectionStatus, string> = {
  CONNECTED: 'Connected',
  DEGRADED: 'Degraded',
  NOT_CONFIGURED: 'Not configured',
  ERROR: 'Error',
};

const AUTONOMY_ORDER: AutonomyMode[] = ['OBSERVE', 'MANUAL', 'AUTONOMOUS_PAPER'];

export function Settings() {
  const connections = useAsync(() => api.listConnections(), []);
  const health = useAsync(() => api.getSystemHealth(), []);
  const limits = useAsync(() => api.getPolicyLimits(), []);
  const {
    autonomy,
    setAutonomy,
    killSwitch,
    setKillSwitch,
    executionEnabled,
    setExecutionEnabled,
    dryRun,
    setDryRun,
    theme,
    setTheme,
    density,
    setDensity,
  } = useApp();

  return (
    <div className="page page--settings">
      <PageHeader
        eyebrow="System"
        title="Settings"
        description="Operating mode, policy, connections and diagnostics. Credentials are never displayed here or anywhere else."
      />

      <Panel eyebrow="Group 1" title="Trading & autonomy">
        <div className="setting">
          <div className="setting__text">
            <h3>Autonomy mode</h3>
            <p>{AUTONOMY_BLURB[autonomy]}</p>
          </div>
          <div className="setting__control">
            <div className="radioset radioset--stack" role="radiogroup" aria-label="Autonomy mode">
              {AUTONOMY_ORDER.map((mode) => (
                <button
                  key={mode}
                  role="radio"
                  aria-checked={autonomy === mode}
                  className={cx('radioset__opt', autonomy === mode && 'is-active')}
                  onClick={() => setAutonomy(mode)}
                >
                  {AUTONOMY_LABEL[mode]}
                </button>
              ))}
            </div>
          </div>
        </div>

        <SettingSwitch
          title="Execution enabled"
          body="When off, the Governor still decides but nothing is ever authorized for submission."
          checked={executionEnabled}
          onChange={setExecutionEnabled}
        />
        <SettingSwitch
          title="Dry run"
          body="Records what would be submitted, including the client order ID, without sending it to the broker."
          checked={dryRun}
          onChange={setDryRun}
        />
        <SettingSwitch
          title="Kill switch"
          body="Refuses every submission at the execution gate, whatever the Governor decided."
          checked={killSwitch}
          onChange={setKillSwitch}
          tone="danger"
        />
      </Panel>

      <Panel
        eyebrow="Group 2"
        title="Risk policy"
        description="These are the deterministic values the engine enforces. Editing them is a Step 3 capability; they are read-only here."
        flush
      >
        <ul className="policylist">
          {limits.loading && <Loading label="Loading active policy" />}
          {limits.error && <LoadError error={limits.error} />}
          {(limits.data ?? []).map((limit) => (
            <li key={limit.id} className="policylist__row">
              <div>
                <span className="policylist__label">{limit.label}</span>
                <code className="policylist__id u-mono">{limit.id}</code>
              </div>
              <span className="policylist__value">{limit.limitDisplay}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel eyebrow="Group 3" title="Connections" description="State only. No key, token or header is ever rendered.">
        {connections.loading && <Loading />}
        <ul className="connlist">
          {(connections.data ?? []).map((c) => (
            <li key={c.id} className="connlist__row">
              <div className="connlist__main">
                <span className="connlist__label">{c.label}</span>
                <p className="connlist__desc">{c.description}</p>
                <p className="connlist__detail">{c.detail}</p>
              </div>
              <Badge tone={CONNECTION_TONE[c.status]}>{CONNECTION_LABEL[c.status]}</Badge>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel eyebrow="Group 4" title="Interface">
        <div className="setting">
          <div className="setting__text">
            <h3>Theme</h3>
            <p>Dark is the default operating surface. Light mode carries the same semantics, not an inversion.</p>
          </div>
          <div className="setting__control">
            <div className="radioset" role="radiogroup" aria-label="Theme">
              {(['dark', 'light'] as const).map((t) => (
                <button
                  key={t}
                  role="radio"
                  aria-checked={theme === t}
                  className={cx('radioset__opt', theme === t && 'is-active')}
                  onClick={() => setTheme(t)}
                >
                  {t === 'dark' ? 'Dark' : 'Light'}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="setting">
          <div className="setting__text">
            <h3>Density</h3>
            <p>Compact tightens table rows and panel padding. Type size does not change.</p>
          </div>
          <div className="setting__control">
            <div className="radioset" role="radiogroup" aria-label="Density">
              {(['comfortable', 'compact'] as const).map((d) => (
                <button
                  key={d}
                  role="radio"
                  aria-checked={density === d}
                  className={cx('radioset__opt', density === d && 'is-active')}
                  onClick={() => setDensity(d)}
                >
                  {d === 'comfortable' ? 'Comfortable' : 'Compact'}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="setting">
          <div className="setting__text">
            <h3>Motion</h3>
            <p>
              The interface follows your operating-system reduced-motion preference. There is no separate switch,
              because a setting that can disagree with the system one is a setting that gets forgotten.
            </p>
          </div>
          <div className="setting__control">
            <span className="u-dim">Follows system</span>
          </div>
        </div>

        <div className="setting">
          <div className="setting__text">
            <h3>Timezone</h3>
            <p>All timestamps are shown in UTC so a stored event and a displayed one always read the same.</p>
          </div>
          <div className="setting__control">
            <span className="u-mono">UTC</span>
          </div>
        </div>
      </Panel>

      <Panel eyebrow="Group 5" title="Safety & system">
        <div className="diaggrid">
          <Diag label="Environment" value="Paper only" tone="paper" note="Live trading fails closed and has no configuration path." />
          <Diag
            label="Database"
            value={health.data ? CONNECTION_LABEL[health.data.database] : '…'}
            tone={health.data ? CONNECTION_TONE[health.data.database] : 'neutral'}
          />
          <Diag
            label="Audit storage"
            value={health.data ? CONNECTION_LABEL[health.data.auditStorage] : '…'}
            tone={health.data ? CONNECTION_TONE[health.data.auditStorage] : 'neutral'}
          />
          <Diag
            label="Broker connectivity"
            value={health.data ? CONNECTION_LABEL[health.data.broker] : '…'}
            tone={health.data ? CONNECTION_TONE[health.data.broker] : 'neutral'}
          />
          <Diag label="Backend version" value={health.data?.backendVersion ?? '…'} tone="neutral" />
          <Diag
            label="Market session"
            value={
              health.data?.marketOpen === null || health.data?.marketOpen === undefined
                ? 'Unavailable'
                : health.data.marketOpen
                  ? 'Open'
                  : 'Closed'
            }
            tone={health.data?.marketOpen ? 'ok' : 'neutral'}
            note={health.data?.nextClose ? `Next close ${stampOf(health.data.nextClose)} UTC` : undefined}
          />
          <Diag
            label="Frontend data source"
            value={API_MODE === 'mock' ? 'Demo dataset' : 'HTTP backend'}
            tone={API_MODE === 'mock' ? 'warn' : 'ok'}
            note={API_MODE === 'mock' ? 'Set VITE_API_MODE=http to read from the backend.' : `Base URL ${API_BASE_URL}`}
          />
        </div>
        <p className="panel__note">
          Credentials, tokens and authorization headers are not part of any response this frontend consumes, and there
          is no view in the product that can display one.
        </p>
      </Panel>
    </div>
  );
}

function SettingSwitch({
  title,
  body,
  checked,
  onChange,
  tone = 'accent',
}: {
  title: string;
  body: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  tone?: 'accent' | 'danger';
}) {
  return (
    <div className="setting">
      <div className="setting__text">
        <h3>{title}</h3>
        <p>{body}</p>
      </div>
      <div className="setting__control">
        <button
          className={cx('toggle', checked && 'is-on', tone === 'danger' && 'toggle--danger')}
          role="switch"
          aria-checked={checked}
          aria-label={title}
          onClick={() => onChange(!checked)}
        >
          <span className="toggle__track" aria-hidden="true">
            <span className="toggle__thumb" />
          </span>
          <span className="toggle__state">{checked ? 'On' : 'Off'}</span>
        </button>
      </div>
    </div>
  );
}

function Diag({ label, value, tone, note }: { label: string; value: string; tone: Tone; note?: string }) {
  return (
    <div className="diag">
      <span className="u-label">{label}</span>
      <Badge tone={tone}>{value}</Badge>
      {note && <p className="diag__note">{note}</p>}
    </div>
  );
}
