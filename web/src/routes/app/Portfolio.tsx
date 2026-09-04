import { useMemo } from 'react';
import { MetricStrip } from '@/components/domain/MetricStrip';
import type { MetricCell } from '@/components/domain/MetricStrip';
import { Datum } from '@/components/domain/Datum';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import { DataTable } from '@/components/ui/DataTable';
import type { Column } from '@/components/ui/DataTable';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError, Unavailable } from '@/components/ui/State';
import { exposureByAssetType } from '@/data/portfolio';
import { cx, money, moneyCompact, percent, signedMoney, signedPercent } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api, API_MODE } from '@/services/api';
import type { Position } from '@/types/domain';

export function Portfolio() {
  const summary = useAsync(() => api.getPortfolioSummary(), []);
  const positions = useAsync(() => api.getPositions(), []);

  const cells: MetricCell[] = useMemo(() => {
    const s = summary.data;
    if (!s) return [];
    return [
      { label: 'Equity', value: moneyCompact(s.equity.value), provenance: s.equity.provenance },
      { label: 'Cash', value: moneyCompact(s.cash.value), provenance: s.cash.provenance },
      { label: 'Buying power', value: moneyCompact(s.buyingPower.value), provenance: s.buyingPower.provenance },
      {
        label: 'Daily P&L',
        value: signedMoney(s.dailyPnl.value),
        note: signedPercent(s.dailyPnlPct.value),
        tone: (s.dailyPnl.value ?? 0) >= 0 ? 'ok' : 'danger',
        provenance: s.dailyPnl.provenance,
      },
      {
        label: 'Realised P&L',
        value: signedMoney(s.realizedPnl.value),
        note: 'No positions closed today',
        provenance: s.realizedPnl.provenance,
      },
      {
        label: 'Unrealised P&L',
        value: signedMoney(s.unrealizedPnl.value),
        tone: (s.unrealizedPnl.value ?? 0) >= 0 ? 'ok' : 'danger',
        provenance: s.unrealizedPnl.provenance,
      },
      {
        label: 'Capital at risk',
        value: moneyCompact(s.capitalAtRisk.value),
        note: 'Options counted at defined maximum loss',
        provenance: s.capitalAtRisk.provenance,
        span: 2,
      },
    ];
  }, [summary.data]);

  const columns: Column<Position>[] = [
    {
      key: 'symbol',
      header: 'Instrument',
      card: 'title',
      sortValue: (p) => p.symbol,
      render: (p) => (
        <div className="cell-instrument">
          <span className="cell-instrument__sym u-mono">{p.symbol}</span>
          <span className="cell-instrument__type">
            {p.assetClass === 'us_option' ? 'Option · defined risk' : 'Equity'}
          </span>
        </div>
      ),
    },
    { key: 'side', header: 'Side', card: 'meta', sortValue: (p) => p.side, render: (p) => p.side },
    { key: 'qty', header: 'Qty', numeric: true, sortValue: (p) => p.quantity, render: (p) => p.quantity },
    { key: 'price', header: 'Price', numeric: true, sortValue: (p) => p.currentPrice ?? -1, render: (p) => money(p.currentPrice) },
    { key: 'cost', header: 'Cost', numeric: true, sortValue: (p) => p.costBasis ?? -1, render: (p) => money(p.costBasis) },
    { key: 'mv', header: 'Market value', numeric: true, sortValue: (p) => p.marketValue, render: (p) => money(p.marketValue) },
    {
      key: 'pnl',
      header: 'Unrealised P&L',
      numeric: true,
      sortValue: (p) => p.unrealizedPl ?? -Infinity,
      render: (p) => (
        <span className={cx(p.unrealizedPl !== null && (p.unrealizedPl >= 0 ? 'u-pos' : 'u-neg'))}>
          {signedMoney(p.unrealizedPl)} <span className="u-dim">{signedPercent(p.unrealizedPlPct)}</span>
        </span>
      ),
    },
    {
      key: 'weight',
      header: 'Weight',
      numeric: true,
      sortValue: (p) => p.weight ?? -1,
      render: (p) => (
        <span className={cx(p.weight !== null && p.weight > 0.2 && 'u-neg')}>
          {percent(p.weight)}
          {p.weight !== null && p.weight > 0.2 && <span className="u-sr-only"> — above the 20% position ceiling</span>}
        </span>
      ),
    },
    {
      key: 'risk',
      header: 'Risk contribution',
      numeric: true,
      sortValue: (p) => p.riskContribution ?? -1,
      render: (p) =>
        p.riskContribution === null ? (
          <Unavailable reason="No risk basis is available for this instrument." />
        ) : (
          <span title={p.riskBasis === 'DEFINED_MAX_LOSS' ? 'Defined maximum loss' : 'Market value'}>
            {money(p.riskContribution)}
          </span>
        ),
    },
    {
      key: 'strategy',
      header: 'Source strategy',
      sortValue: (p) => p.sourceStrategy,
      render: (p) => <span className="u-dim">{p.sourceStrategy}</span>,
    },
  ];

  const exposure = exposureByAssetType(positions.data ?? []);
  const totalMv = exposure.reduce((t, e) => t + e.value, 0);
  const equity = summary.data?.equity.value ?? 0;

  return (
    <div className="page">
      <PageHeader
        eyebrow="Operations"
        title="Portfolio"
        description="Account state, open positions, and how portfolio risk is allocated against policy."
      />

      {summary.loading && <Loading label="Loading portfolio" />}
      {summary.error && <LoadError error={summary.error} />}
      {summary.data && <MetricStrip cells={cells} />}

      <Panel eyebrow="Holdings" title="Positions" flush>
        {positions.loading && <Loading />}
        {positions.error && <LoadError error={positions.error} />}
        {positions.data && (
          <DataTable
            caption="Open positions"
            columns={columns}
            rows={positions.data}
            rowKey={(p) => p.symbol}
            initialSort={{ key: 'mv', direction: 'desc' }}
          />
        )}
      </Panel>

      <div className="grid grid--2">
        <Panel eyebrow="Exposure" title="By asset type" description="Options are risk-measured by defined maximum loss, never by stock-equivalent notional.">
          <ul className="bars">
            {exposure.map((e) => (
              <li key={e.label} className="bars__row">
                <div className="bars__head">
                  <span>{e.label}</span>
                  <span className="u-mono">{money(e.value)}</span>
                </div>
                <div className="bars__track">
                  <div className="bars__fill" style={{ width: `${totalMv ? (e.value / totalMv) * 100 : 0}%` }} />
                </div>
                <div className="bars__foot">
                  <span className="u-dim">
                    {e.count} {e.count === 1 ? 'position' : 'positions'} · risk basis: {e.riskBasis.toLowerCase()}
                  </span>
                  <span className="u-dim">{percent(totalMv ? e.value / totalMv : 0, 0)}</span>
                </div>
              </li>
            ))}
          </ul>
          <p className="panel__note">
            Sector, theme and correlation exposure are <Unavailable reason="No sector or correlation feed is connected." />{' '}
            — no upstream source supplies them, and the product will not invent them.
          </p>
        </Panel>

        <Panel eyebrow="Concentration" title="Symbol exposure" description="Position weight against the 20% single-position ceiling.">
          <ul className="bars">
            {(positions.data ?? []).map((p) => {
              const breach = p.weight !== null && p.weight > 0.2;
              return (
                <li key={p.symbol} className="bars__row">
                  <div className="bars__head">
                    <span className="u-mono">{p.symbol}</span>
                    <span className={cx('u-mono', breach && 'u-neg')}>{percent(p.weight)}</span>
                  </div>
                  <div className="bars__track">
                    <div
                      className={cx('bars__fill', breach && 'bars__fill--breach')}
                      style={{ width: `${p.weight === null ? 0 : Math.min(p.weight / 0.2, 1) * 100}%` }}
                    />
                  </div>
                  {breach && (
                    <div className="bars__foot">
                      <Badge tone="danger">Above ceiling</Badge>
                      <span className="u-dim">20.0% policy limit</span>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </Panel>
      </div>

      <Datum
        label="Governance boundary"
        registers={['The book', 'What policy measures it against']}
        caption="Above the line, what the account holds. Below it, the same book expressed as the quantity policy actually governs — capital genuinely at risk, with options counted at maximum defined loss rather than at stock-equivalent notional."
      />

      <Panel eyebrow="Risk allocation" title="Capital at risk by position" description="How much of the account each position genuinely puts at risk.">
        <ul className="bars">
          {(positions.data ?? []).map((p) => (
            <li key={p.symbol} className="bars__row">
              <div className="bars__head">
                <span className="u-mono">{p.symbol}</span>
                <span className="u-mono">
                  {p.riskContribution === null ? <Unavailable /> : money(p.riskContribution)}
                </span>
              </div>
              <div className="bars__track">
                <div
                  className={cx('bars__fill', p.riskBasis === 'DEFINED_MAX_LOSS' && 'bars__fill--defined')}
                  style={{ width: `${equity ? ((p.riskContribution ?? 0) / equity) * 100 : 0}%` }}
                />
              </div>
              <div className="bars__foot">
                <span className="u-dim">
                  {p.riskBasis === 'DEFINED_MAX_LOSS' ? 'Defined maximum loss' : 'Market value'}
                </span>
                <span className="u-dim">{percent(equity ? (p.riskContribution ?? 0) / equity : 0)} of equity</span>
              </div>
            </li>
          ))}
        </ul>
      </Panel>

      <p className="page__footnote">
        <ProvenanceBadge value={API_MODE === 'http' ? 'MIZAN_LEDGER' : 'DEMO'} size="xs" /> Portfolio figures are read
        from {API_MODE === 'http' ? 'the latest recorded backend snapshot' : 'demonstration data shaped like an Alpaca Paper snapshot'}.
      </p>
    </div>
  );
}
