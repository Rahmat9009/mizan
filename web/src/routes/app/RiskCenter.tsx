import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { AutonomyControl } from '@/components/domain/Autonomy';
import { Datum } from '@/components/domain/Datum';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import type { Tone } from '@/components/ui/Badge';
import { DataTable } from '@/components/ui/DataTable';
import type { Column } from '@/components/ui/DataTable';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError } from '@/components/ui/State';
import { Tabs } from '@/components/ui/Tabs';
import { SESSION_ANCHOR } from '@/data/clock';
import { cx, percent, relative, stampOf } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { comparePolicyBreachFirst, derivePolicyTriage } from '@/lib/policy';
import { api } from '@/services/api';
import { useApp } from '@/state/app';
import type { Intervention, PolicyLimit } from '@/types/domain';

const STATUS_TONE: Record<PolicyLimit['status'], Tone> = {
  OK: 'ok',
  WATCH: 'warn',
  BREACH: 'danger',
  UNAVAILABLE: 'neutral',
};

const INTERVENTION_TONE: Record<Intervention['kind'], Tone> = {
  REDUCE: 'warn',
  REJECT: 'danger',
  BLOCK: 'danger',
  REAUTHORIZE: 'warn',
  MARKET_CLOSED: 'neutral',
  STALE_AUTHORIZATION: 'warn',
};

/**
 * The Risk Center.
 *
 * Equity risk and options risk are kept separate throughout: a share position
 * risks its market value, a defined-risk spread risks its maximum loss, and
 * mixing the two produces a number that means nothing.
 */
export function RiskCenter() {
  const limits = useAsync(() => api.getPolicyLimits(), []);
  const interventions = useAsync(() => api.getInterventions(), []);
  const { killSwitch, setKillSwitch, executionEnabled, setExecutionEnabled, dryRun, setDryRun } = useApp();

  const allLimits = limits.data ?? [];
  const equityLimits = allLimits.filter((l) => l.group === 'equity');
  const optionLimits = allLimits.filter((l) => l.group === 'options');
  const portfolioLimits = allLimits.filter((l) => l.group === 'portfolio');

  const interventionColumns: Column<Intervention>[] = [
    {
      key: 'time',
      header: 'Time',
      card: 'meta',
      sortValue: (i) => i.at,
      render: (i) => (
        <time className="u-mono" dateTime={i.at} title={`${stampOf(i.at)} UTC`}>
          {relative(i.at, SESSION_ANCHOR)}
        </time>
      ),
    },
    {
      key: 'symbol',
      header: 'Symbol',
      card: 'title',
      sortValue: (i) => i.symbol,
      render: (i) => <span className="u-mono">{i.symbol}</span>,
    },
    {
      key: 'kind',
      header: 'Intervention',
      sortValue: (i) => i.kind,
      render: (i) => <Badge tone={INTERVENTION_TONE[i.kind]}>{i.kind.replace(/_/g, ' ')}</Badge>,
    },
    { key: 'rule', header: 'Rule', sortValue: (i) => i.rule, render: (i) => <code className="u-mono">{i.rule}</code> },
    {
      /* One ledger column rather than split Before/After. This is the surface
         an operator scans for the magnitude of an intervention, and it was the
         one place the product's own signature device had been discarded. */
      key: 'change',
      header: 'Requested → authorized',
      render: (i) => (
        <span className="intervention__ledger">
          <span className="intervention__from">{i.before}</span>
          <ArrowRight size={12} aria-hidden="true" className="intervention__arrow" />
          <strong className="intervention__to">{i.after}</strong>
        </span>
      ),
    },
    { key: 'actor', header: 'Decided by', render: (i) => i.actor },
    {
      key: 'proposal',
      header: 'Proposal',
      align: 'right',
      render: (i) => (
        <Link className="u-mono" to={`/app/proposals/${i.proposalId}`}>
          {i.proposalId}
        </Link>
      ),
    },
  ];

  return (
    <div className="page">
      <PageHeader
        eyebrow="Governance"
        title="Risk Center"
        description="The policy the engine actually enforces, how much of it is in use, and every time it changed a trade."
      />

      {allLimits.length > 0 && <PolicyPosture limits={allLimits} />}

      <Panel eyebrow="Controls" title="Safety controls" className="panel--controls">
        <div className="controls">
          <div className="controls__cell controls__cell--paper">
            <span className="u-label">Environment</span>
            {/* The environment is paper gold everywhere it appears, including
                the top bar. Brass is the governance boundary and nothing else. */}
            <Badge tone="paper" shape="diamond" size="md">
              Paper only
            </Badge>
            <p className="controls__note">Live trading is not supported and has no configuration path.</p>
          </div>

          <div className="controls__cell">
            <span className="u-label">Autonomy</span>
            <AutonomyControl variant="panel" />
          </div>

          <div className="controls__cell">
            <span className="u-label">Execution</span>
            <div className="switches">
              <Toggle label="Execution enabled" checked={executionEnabled} onChange={setExecutionEnabled} />
              <Toggle label="Dry run" checked={dryRun} onChange={setDryRun} />
            </div>
            <KillSwitch armed={killSwitch} onChange={setKillSwitch} />
            {/* Three controls governing capital deployment with no stated
                precedence is an error-prevention failure, not a density win.
                The order they resolve in is now written down. */}
            <p className="controls__note">
              The kill switch overrides both. With it armed, every submission is refused at the gate whatever the other
              two say. With it off, dry run overrides execution enabled: nothing is sent, but everything that would
              have been is recorded.
            </p>
          </div>
        </div>
      </Panel>

      {limits.loading && <Loading label="Loading policy" />}
      {limits.error && <LoadError error={limits.error} />}

      <Tabs
        items={[
          {
            id: 'equity',
            label: 'Equity risk',
            count: equityLimits.length,
            content: <LimitRegister limits={equityLimits} />,
          },
          {
            id: 'options',
            label: 'Options risk',
            count: optionLimits.length,
            content: (
              <>
                <p className="tabs__lede">
                  Options are governed by maximum defined loss. The engine recomputes the loss from the legs rather than
                  trusting a declared figure, and refuses any structure with a naked short leg.
                </p>
                <LimitRegister limits={optionLimits} />
              </>
            ),
          },
          {
            id: 'portfolio',
            label: 'Portfolio & execution',
            count: portfolioLimits.length,
            content: <LimitRegister limits={portfolioLimits} />,
          },
        ]}
      />

      <Datum
        label="Governance boundary"
        registers={['Policy as written', 'Policy as enforced']}
        caption="Above the line, the policy as written — what the engine will enforce on anything an agent proposes. Below it, the policy as enforced: every trade it actually changed or stopped."
      />

      <Panel eyebrow="Record" title="Recent interventions" description="Every time governance changed or stopped a trade." flush>
        {interventions.loading && <Loading />}
        {interventions.data && (
          <DataTable
            caption="Governor and execution interventions"
            columns={interventionColumns}
            rows={interventions.data}
            rowKey={(i) => i.id}
            initialSort={{ key: 'time', direction: 'desc' }}
          />
        )}
      </Panel>
    </div>
  );
}

/**
 * Policy posture triage summary.
 * Answers the operator's first question: "What needs attention right now?"
 * Derived from the exact same PolicyLimit rows using derivePolicyTriage().
 */
function PolicyPosture({ limits }: { limits: PolicyLimit[] }) {
  const triage = derivePolicyTriage(limits);
  const isBreached = triage.breachCount > 0;
  const isWatchOnly = !isBreached && triage.watchCount > 0;

  return (
    <section
      className={cx('posture', isBreached && 'is-breach', isWatchOnly && 'is-watch-only')}
      aria-label="Current policy posture and triage summary"
    >
      <p className="posture__lede">
        <strong>{triage.breachCount}</strong> of <strong>{triage.total}</strong> armed policies{' '}
        {triage.breachCount === 1 ? 'is' : 'are'} over the line.
      </p>
      <ul className="posture__counts">
        <PostureCount n={triage.breachCount} label="breach" tone="danger" glyph="■" />
        <PostureCount n={triage.watchCount} label="watch" tone="warn" glyph="◆" />
        <PostureCount n={triage.withinLimitCount} label="within limit" tone="ok" glyph="●" />
        <PostureCount n={triage.unavailableCount} label="not measurable" tone="neutral" glyph="─" />
      </ul>
      {triage.tightest && triage.tightest.utilisation !== null && (
        <p className="posture__tightest">
          Tightest: <strong>{triage.tightest.label}</strong> at{' '}
          <span className="u-mono">{percent(triage.tightest.utilisation, 0)}</span> of its limit
          <span className="u-dim"> · {triage.tightest.currentDisplay}</span>
        </p>
      )}
    </section>
  );
}

function PostureCount({
  n,
  label,
  tone,
  glyph,
}: {
  n: number;
  label: string;
  tone: 'danger' | 'warn' | 'ok' | 'neutral';
  glyph: string;
}) {
  return (
    <li className={cx('posture__count', `is-${tone}`, n === 0 && 'is-empty')}>
      <span className="posture__glyph" aria-hidden="true">
        {glyph}
      </span>
      <span className="posture__n">{n}</span>
      <span className="posture__label">{label}</span>
    </li>
  );
}

/**
 * The policy, as one instrument.
 *
 * This was nine bordered, rounded cards in a three-column grid — a panel inside
 * a panel, a ragged empty slot in the last row, and the single most reproduced
 * composition in B2B software on the view that most needed to look like this
 * product. The policies are read together and compared against each other, so
 * they are one hairline-separated register with a shared limit rule: every
 * row's utilisation is drawn against the same ceiling mark, and a row that has
 * no budget to draw says so instead of rendering an empty bar.
 */
/**
 * The ceiling stands at 72% of the track rather than at its right edge, so a
 * policy at 133% of its limit is drawn *past* the mark instead of clipped flush
 * with it. Against a full-width ceiling, a breach and a policy merely at its
 * limit render identically — which is the one comparison this column exists to
 * make.
 */
const CEILING_X = 72;

function LimitRegister({ limits }: { limits: PolicyLimit[] }) {
  // Sort breach-first by default: BREACH > WATCH > OK > UNAVAILABLE
  const sortedLimits = [...limits].sort(comparePolicyBreachFirst);

  return (
    <div className="policyreg">
      <div className="policyreg__head" aria-hidden="true">
        <span>Policy</span>
        <span>What it limits</span>
        <span className="policyreg__headuse">
          <span>Utilisation against limit</span>
          <span className="policyreg__headceiling">Ceiling 100%</span>
        </span>
        <span className="policyreg__headnum">Current</span>
        <span className="policyreg__headnum">Limit</span>
        <span>State</span>
      </div>
      <ul className="policyreg__rows">
        {sortedLimits.map((limit) => (
          <li key={limit.id} className={cx('policyreg__row', `is-${limit.status.toLowerCase()}`)}>
            <div className="policyreg__name">
              <h3 className="policyreg__title">{limit.label}</h3>
              <code className="policyreg__id u-mono">{limit.id}</code>
            </div>
            <p className="policyreg__desc">{limit.description}</p>
            <div className="policyreg__use">
              {limit.utilisation === null ? (
                <p className="policyreg__nobudget">
                  Threshold, not a budget — there is no utilisation to draw.
                </p>
              ) : (
                <span className="policyreg__track" title={`Utilisation: ${percent(limit.utilisation, 0)} of limit`}>
                  <span
                    className={cx(
                      'policyreg__fill',
                      limit.status === 'BREACH' ? 'is-over' : limit.status === 'WATCH' ? 'is-warn' : 'is-ok',
                    )}
                    style={{ width: `${Math.min(limit.utilisation * CEILING_X, 100)}%` }}
                  />
                  <span className="policyreg__ceiling" aria-hidden="true" />
                </span>
              )}
            </div>
            <span className="policyreg__num u-mono">{limit.currentDisplay}</span>
            <span className="policyreg__num policyreg__num--limit u-mono">{limit.limitDisplay}</span>
            <div className="policyreg__state">
              <Badge tone={STATUS_TONE[limit.status]}>{limit.status}</Badge>
              <ProvenanceBadge value={limit.provenance} size="xs" />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The kill switch is the same state as the sidebar's Full stop
 * (`killSwitch === responseLevel 5`), so it gets the same gravity: a danger
 * control with a stated consequence, not a toggle that looks like "Dry run".
 */
function KillSwitch({ armed, onChange }: { armed: boolean; onChange: (next: boolean) => void }) {
  const [confirming, setConfirming] = useState(false);

  if (armed) {
    return (
      <div className="killswitch killswitch--armed" role="status">
        <p className="killswitch__title">
          <span aria-hidden="true">✕</span> Kill switch armed
        </p>
        <p className="killswitch__body">
          Every submission is refused at the execution gate and every outstanding authorization is void.
        </p>
        <Button variant="secondary" size="sm" onClick={() => onChange(false)}>
          Stand down
        </Button>
      </div>
    );
  }

  if (confirming) {
    return (
      <div className="killswitch killswitch--confirm" role="alertdialog" aria-label="Confirm kill switch">
        <p className="killswitch__body">
          Arming the kill switch refuses every submission and voids every outstanding authorization. This is the same
          state as Full stop. Agents will need fresh authorization to trade again.
        </p>
        <div className="killswitch__row">
          <Button
            variant="danger"
            size="sm"
            onClick={() => {
              onChange(true);
              setConfirming(false);
            }}
          >
            Arm kill switch
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <Button variant="danger" size="sm" onClick={() => setConfirming(true)}>
      Arm kill switch
    </Button>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  tone = 'accent',
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  tone?: 'accent' | 'danger';
}) {
  return (
    <button
      className={cx('toggle', checked && 'is-on', tone === 'danger' && 'toggle--danger')}
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
    >
      <span className="toggle__track" aria-hidden="true">
        <span className="toggle__thumb" />
      </span>
      <span className="toggle__label">{label}</span>
      <span className="toggle__state">{checked ? 'On' : 'Off'}</span>
    </button>
  );
}
