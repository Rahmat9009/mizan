import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';
import { AuditTimeline } from '@/components/domain/AuditTimeline';
import { Datum } from '@/components/domain/Datum';
import { VerdictBadge, OrderStateBadge, QuantityLedger } from '@/components/domain/Decision';
import { MetricStrip } from '@/components/domain/MetricStrip';
import type { MetricCell } from '@/components/domain/MetricStrip';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { QuietState } from '@/components/domain/QuietState';
import { Badge } from '@/components/ui/Badge';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError } from '@/components/ui/State';
import { SESSION_ANCHOR } from '@/data/clock';
import { RESPONSE_LEVELS } from '@/data/governance';
import { STATE_LABEL } from '@/data/pipeline';
import { cx, moneyCompact, percent, relative, signedMoney, signedPercent } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api, API_MODE } from '@/services/api';
import { AUTONOMY_LABEL, useApp } from '@/state/app';
import type { Proposal } from '@/types/domain';

/**
 * The Command Center, composed across the governance datum.
 *
 * The page reads as one sentence in two registers. Above the datum: what the
 * desk is being asked to do, and the book it is being asked to do it against.
 * Below it: what policy did about that. The brass rule is not a divider between
 * two arbitrary halves — the panels genuinely change authorship there, and
 * every operational surface in the product uses the same split, so an operator
 * always knows which side of the line they are reading.
 *
 * The day's governed totals stay at the very top. Ninety-nine percent of the
 * time nothing is wrong, and the quiet day is the proof of value; a home screen
 * built around incidents looks dead on a normal morning.
 */
export function Dashboard() {
  const navigate = useNavigate();
  const { autonomy, responseLevel, executionEnabled, dryRun } = useApp();

  const summary = useAsync(() => api.getPortfolioSummary(), []);
  const day = useAsync(() => api.getGovernanceDay(), []);
  const proposals = useAsync(() => api.listProposals(), []);
  const alerts = useAsync(() => api.getRiskAlerts(), []);
  const positions = useAsync(() => api.getPositions(), []);
  const orders = useAsync(() => api.listOrders(), []);
  const audit = useAsync(() => api.listAuditEvents(), []);

  const [focusedId, setFocusedId] = useState<string | null>(null);
  const focused: Proposal | null = useMemo(() => {
    const list = proposals.data ?? [];
    if (focusedId) return list.find((p) => p.proposalId === focusedId) ?? null;
    return list.find((p) => p.stages.some((s) => s.state === 'RUNNING')) ?? list[0] ?? null;
  }, [proposals.data, focusedId]);

  /* What the page is about to show as needing a person. The quiet state is
     computed from this rather than from a stored figure, so the home screen
     cannot announce calm above a panel of open blocks. */
  const openItems = useMemo(
    () =>
      (alerts.data ?? [])
        .filter((a) => a.severity === 'BLOCK' || a.severity === 'HIGH')
        .map((a) => ({ id: a.id, label: `${a.kind} · ${a.symbol}` })),
    [alerts.data],
  );

  const cells: MetricCell[] = useMemo(() => {
    const s = summary.data;
    if (!s) return [];
    return [
      { label: 'Equity', value: moneyCompact(s.equity.value), note: 'Paper account', provenance: s.equity.provenance },
      {
        label: 'Daily P&L',
        value: signedMoney(s.dailyPnl.value),
        note: signedPercent(s.dailyPnlPct.value),
        tone: s.dailyPnl.value === null ? 'neutral' : s.dailyPnl.value >= 0 ? 'ok' : 'danger',
        provenance: s.dailyPnl.provenance,
      },
      { label: 'Cash', value: moneyCompact(s.cash.value), note: 'Settled', provenance: s.cash.provenance },
      { label: 'Buying power', value: moneyCompact(s.buyingPower.value), note: 'Paper margin', provenance: s.buyingPower.provenance },
      {
        label: 'Capital at risk',
        value: moneyCompact(s.capitalAtRisk.value),
        note:
          s.capitalAtRisk.value === null || s.equity.value === null
            ? 'Unavailable · no aggregate risk figure was supplied'
            : `${percent(s.capitalAtRisk.value / s.equity.value)} of equity · options counted at defined loss`,
        provenance: s.capitalAtRisk.provenance,
        span: 2,
      },
      {
        label: 'Autonomy',
        value: AUTONOMY_LABEL[autonomy],
        note: !executionEnabled ? 'Execution disabled' : dryRun ? 'Dry run' : 'Execution enabled',
        tone: autonomy === 'AUTONOMOUS_PAPER' ? 'warn' : 'neutral',
      },
      {
        label: 'Response level',
        value: `L${responseLevel}`,
        note: RESPONSE_LEVELS[responseLevel].name,
        tone: RESPONSE_LEVELS[responseLevel].tone,
      },
    ];
  }, [summary.data, autonomy, responseLevel, executionEnabled, dryRun]);

  return (
    <div className="page page--dashboard">
      {/* The Command Center opens on the quiet state rather than on a page
          header, so it has no visible title to carry the route's h1. The view
          name is announced here instead of borrowed from the top bar, which is
          chrome shared by every route. */}
      <h1 className="u-sr-only">Command Center</h1>

      {day.data && <QuietState day={day.data} open={openItems} />}

      {summary.loading && <Loading label="Loading account state" />}
      {summary.error && <LoadError error={summary.error} />}
      {summary.data && <MetricStrip cells={cells} />}

      {/* ------------------------------------------ the claim, above the line */}
      <section className="register register--claim" aria-label="What the agents are asking for">
        <Panel
          eyebrow="Needs attention"
          title="Active proposals"
          description="What the upstream agents have asked for, before policy has spoken."
          actions={<Link className="panel__more" to="/app/proposals">All proposals <ArrowUpRight size={12} aria-hidden="true" /></Link>}
          flush
        >
          {proposals.loading && <Loading />}
          {proposals.error && <LoadError error={proposals.error} />}
          <ul className="proplist">
            {(proposals.data ?? []).slice(0, 5).map((p) => (
              <li key={p.proposalId}>
                <button className="proplist__row" onClick={() => navigate(`/app/proposals/${p.proposalId}`)}>
                  <div className="proplist__lead">
                    <span className="proplist__sym">{symbolOf(p)}</span>
                    <span className={cx('proplist__side', sideOf(p) === 'SELL' && 'is-sell')}>{sideOf(p)}</span>
                  </div>
                  <div className="proplist__mid">
                    <QuantityLedger
                      proposed={p.governor?.originalQuantity ?? quantityOf(p)}
                      approved={p.governor?.approvedQuantity ?? quantityOf(p)}
                      unit={p.instrumentType === 'option' ? 'contracts' : 'shares'}
                      size="sm"
                    />
                    <span className="proplist__stage">
                      {stageLabel(p)}
                      {p.reasonCodes[0] && <code className="proplist__code">{p.reasonCodes[0]}</code>}
                    </span>
                  </div>
                  <div className="proplist__tail">
                    {p.governor ? <VerdictBadge decision={p.governor.decision} /> : <Badge tone="accent">In review</Badge>}
                    <span className="proplist__time">{relative(p.createdAt, SESSION_ANCHOR)}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </Panel>

        <Panel
          eyebrow="Portfolio"
          title="The book they are sizing against"
          actions={<Link className="panel__more" to="/app/portfolio">Detail <ArrowUpRight size={12} aria-hidden="true" /></Link>}
          flush
        >
          {positions.loading && <Loading />}
          <ul className="minilist">
            {(positions.data ?? []).map((p) => (
              <li key={p.symbol} className="minilist__row">
                <span className="minilist__key u-mono">{p.symbol}</span>
                <span className="minilist__mid">{percent(p.weight)}</span>
                <span className={cx('minilist__val', p.unrealizedPl !== null && (p.unrealizedPl >= 0 ? 'u-pos' : 'u-neg'))}>
                  {signedMoney(p.unrealizedPl)}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      </section>

      {/* --------------------------------------------------------- the datum */}
      <div className="dash__datumhead">
        <div>
          <p className="u-eyebrow">Live pipeline</p>
          <p className="dash__datumsubject">
            {focused ? headline(focused) : 'Pipeline'}
            {focused && (
              <span className="u-mono u-dim"> · {focused.proposalId} · created {relative(focused.createdAt, SESSION_ANCHOR)}</span>
            )}
          </p>
        </div>
        <label className="fieldinline">
          <span className="u-sr-only">Pipeline subject</span>
          <select className="select" value={focused?.proposalId ?? ''} onChange={(e) => setFocusedId(e.target.value)}>
            {(proposals.data ?? []).map((p) => (
              <option key={p.proposalId} value={p.proposalId}>
                {headline(p)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {proposals.loading && <Loading label="Loading pipeline" />}
      {focused && (
        <Datum
          stages={focused.stages}
          selectable
          showDetail
          caption="Above the line an agent is asking. Below it, policy has already answered."
        />
      )}

      {/* ----------------------------------------- the ruling, below the line */}
      <section className="register register--ruling" aria-label="What policy did">
        <Panel
          eyebrow="Governance"
          title="Risk alerts"
          description="Raised by policy, not by sentiment."
          flush
        >
          {alerts.loading && <Loading />}
          <ul className="alertlist">
            {(alerts.data ?? []).map((a) => (
              <li key={a.id} className={cx('alertlist__row', `is-${a.severity.toLowerCase()}`)}>
                <div className="alertlist__head">
                  <Badge tone={a.severity === 'WATCH' ? 'warn' : 'danger'}>{a.severity}</Badge>
                  <span className="alertlist__kind">{a.kind}</span>
                  <span className="alertlist__sym u-mono">{a.symbol}</span>
                  <time className="alertlist__time" dateTime={a.at}>
                    {relative(a.at, SESSION_ANCHOR)}
                  </time>
                </div>
                <p className="alertlist__reason">{a.reason}</p>
                <div className="alertlist__foot">
                  <code>{a.policy}</code>
                  {a.proposalId && (
                    <Link className="u-mono" to={`/app/proposals/${a.proposalId}`}>
                      {a.proposalId}
                    </Link>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Panel>

        <div className="register__stack">
          <Panel
            eyebrow="Execution"
            title="Under authorization"
            actions={<Link className="panel__more" to="/app/orders">Lifecycle <ArrowUpRight size={12} aria-hidden="true" /></Link>}
            flush
          >
            {orders.loading && <Loading />}
            <ul className="minilist">
              {(orders.data ?? []).slice(0, 6).map((o) => (
                <li key={o.clientOrderId} className="minilist__row">
                  <span className="minilist__key u-mono">{o.symbol}</span>
                  <span className="minilist__mid u-mono">
                    {o.filledQuantity}/{o.approvedQuantity}
                  </span>
                  <OrderStateBadge state={o.lifecycle} />
                </li>
              ))}
            </ul>
          </Panel>

          <Panel
            eyebrow="Record"
            title="Recent audit events"
            actions={<Link className="panel__more" to="/app/audit">Full timeline <ArrowUpRight size={12} aria-hidden="true" /></Link>}
            flush
          >
            {audit.loading && <Loading />}
            <AuditTimeline events={(audit.data ?? []).slice(0, 5)} dense />
          </Panel>
        </div>
      </section>

      <p className="page__footnote">
        <ProvenanceBadge value={API_MODE === 'http' ? 'MIZAN_LEDGER' : 'DEMO'} size="xs" />{' '}
        {API_MODE === 'http' ? 'This view is projected from authenticated Mizan API records.' : 'This view runs on the fixed demo dataset.'}{' '}
        Values the selected source cannot supply read <span className="u-unavailable">Unavailable</span> rather than zero.
      </p>
    </div>
  );
}

function symbolOf(p: Proposal) {
  return p.instrument.type === 'equity' ? p.instrument.symbol : p.instrument.underlying;
}
function sideOf(p: Proposal) {
  return p.instrument.type === 'equity' ? p.instrument.side : 'BUY';
}
function quantityOf(p: Proposal) {
  return p.instrument.quantity;
}
function headline(p: Proposal) {
  return p.instrument.type === 'equity'
    ? `${p.instrument.side} ${p.instrument.quantity} ${p.instrument.symbol}`
    : `${p.instrument.underlying} ${p.instrument.strategy.replace(/_/g, ' ').toLowerCase()}`;
}
function stageLabel(p: Proposal) {
  const s = p.stages.find((x) => x.id === p.stage);
  return s ? `${s.label} · ${STATE_LABEL[s.state]}` : 'Pipeline';
}
