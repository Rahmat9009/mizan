import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { VerdictBadge, QuantityLedger } from '@/components/domain/Decision';
import { ConfidenceReadout } from '@/components/domain/Confidence';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import { DataTable } from '@/components/ui/DataTable';
import type { Column } from '@/components/ui/DataTable';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError } from '@/components/ui/State';
import { SESSION_ANCHOR } from '@/data/clock';
import { STATE_LABEL, STATE_TONE } from '@/data/pipeline';
import { cx, relative } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { Decision, Proposal } from '@/types/domain';

type Filter = 'all' | Decision | 'in_review';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'in_review', label: 'In review' },
  { id: 'APPROVE', label: 'Approved' },
  { id: 'REDUCE', label: 'Reduced' },
  { id: 'REJECT', label: 'Rejected' },
];

export function Proposals() {
  const navigate = useNavigate();
  const { data, loading, error } = useAsync(() => api.listProposals(), []);
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');

  const rows = useMemo(() => {
    let list = data ?? [];
    if (filter === 'in_review') list = list.filter((p) => !p.governor);
    else if (filter !== 'all') list = list.filter((p) => p.governor?.decision === filter);
    if (query.trim()) {
      const needle = query.toLowerCase();
      list = list.filter((p) => `${symbolOf(p)} ${p.proposalId} ${p.thesis}`.toLowerCase().includes(needle));
    }
    return list;
  }, [data, filter, query]);

  const counts = useMemo(() => {
    const list = data ?? [];
    return {
      all: list.length,
      in_review: list.filter((p) => !p.governor).length,
      APPROVE: list.filter((p) => p.governor?.decision === 'APPROVE').length,
      REDUCE: list.filter((p) => p.governor?.decision === 'REDUCE').length,
      REJECT: list.filter((p) => p.governor?.decision === 'REJECT').length,
    } as Record<Filter, number>;
  }, [data]);

  const columns: Column<Proposal>[] = [
    {
      key: 'instrument',
      header: 'Instrument',
      card: 'title',
      sortValue: (p) => symbolOf(p),
      render: (p) => (
        <div className="cell-instrument">
          <span className="cell-instrument__sym u-mono">{symbolOf(p)}</span>
          <span className="cell-instrument__type">
            {p.instrument.type === 'equity' ? 'Equity' : p.instrument.strategy.replace(/_/g, ' ').toLowerCase()}
          </span>
        </div>
      ),
    },
    {
      key: 'side',
      header: 'Side',
      card: 'meta',
      sortValue: (p) => sideOf(p),
      render: (p) => <span className={cx('side', sideOf(p) === 'SELL' && 'side--sell')}>{sideOf(p)}</span>,
    },
    {
      key: 'quantity',
      header: 'Quantity',
      numeric: true,
      sortValue: (p) => p.governor?.approvedQuantity ?? p.instrument.quantity,
      render: (p) => (
        <QuantityLedger
          proposed={p.governor?.originalQuantity ?? p.instrument.quantity}
          approved={p.governor?.approvedQuantity ?? p.instrument.quantity}
          unit={p.instrumentType === 'option' ? 'ct' : 'sh'}
          size="sm"
        />
      ),
    },
    {
      /* Never a bare headline number. A self-reported confidence is an estimate
         with unknown error, and a clean figure in a column teaches a reader to
         trust a number the engine itself haircuts. The claim is labelled as the
         agent's, and where a calibration record exists it is shown beside it;
         where none exists, the absence is stated rather than filled in. */
      key: 'confidence',
      header: 'Agent claim',
      sortValue: (p) => p.strategyConfidence ?? -1,
      render: (p) => (
        <ConfidenceReadout
          reported={p.strategyConfidence}
          calibration={p.aiRisk?.calibration ?? null}
          label="Agent confidence"
          compact
        />
      ),
    },
    {
      key: 'stage',
      header: 'Stage',
      sortValue: (p) => p.stage,
      render: (p) => {
        const s = p.stages.find((x) => x.id === p.stage);
        if (!s) return '—';
        return (
          <span className="cell-stage">
            <i className={cx('dot', `dot--${STATE_TONE[s.state]}`)} aria-hidden="true" />
            {s.label}
            <span className="u-dim"> · {STATE_LABEL[s.state]}</span>
          </span>
        );
      },
    },
    {
      key: 'governor',
      header: 'Governor',
      sortValue: (p) => p.governor?.decision ?? 'PENDING',
      render: (p) => (p.governor ? <VerdictBadge decision={p.governor.decision} /> : <Badge tone="accent">In review</Badge>),
    },
    {
      key: 'time',
      header: 'Created',
      align: 'right',
      sortValue: (p) => p.createdAt,
      render: (p) => (
        <time dateTime={p.createdAt} className="u-dim">
          {relative(p.createdAt, SESSION_ANCHOR)}
        </time>
      ),
    },
    {
      key: 'provenance',
      header: 'Source',
      align: 'right',
      render: (p) => <ProvenanceBadge value={p.provenance} size="xs" />,
    },
  ];

  return (
    <div className="page">
      <PageHeader
        eyebrow="Governance"
        title="Proposals"
        description="Every TradeProposal that reached the boundary, with what governance did to it."
      />

      <Panel flush>
        <div className="toolbar">
          <div className="segmented" role="group" aria-label="Filter by Governor result">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                className={cx('segmented__btn', filter === f.id && 'is-active')}
                onClick={() => setFilter(f.id)}
                aria-pressed={filter === f.id}
              >
                {f.label}
                <span className="segmented__count">{counts[f.id] ?? 0}</span>
              </button>
            ))}
          </div>
          <label className="searchfield">
            <span className="u-sr-only">Search proposals</span>
            <input
              className="input"
              placeholder="Filter by symbol, ID or thesis"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        </div>

        {loading && <Loading label="Loading proposals" />}
        {error && <LoadError error={error} />}
        {data && (
          <DataTable
            caption="Evaluated trade proposals"
            columns={columns}
            rows={rows}
            rowKey={(p) => p.proposalId}
            onRowActivate={(p) => navigate(`/app/proposals/${p.proposalId}`)}
            emptyMessage="No proposals match this filter."
            initialSort={{ key: 'time', direction: 'desc' }}
          />
        )}
      </Panel>
    </div>
  );
}

function symbolOf(p: Proposal) {
  return p.instrument.type === 'equity' ? p.instrument.symbol : p.instrument.underlying;
}
function sideOf(p: Proposal) {
  return p.instrument.type === 'equity' ? p.instrument.side : 'BUY';
}
