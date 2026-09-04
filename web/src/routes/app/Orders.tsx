import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { OrderStateBadge, QuantityLedger, orderStateLabel } from '@/components/domain/Decision';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import { DataTable } from '@/components/ui/DataTable';
import type { Column } from '@/components/ui/DataTable';
import { Drawer } from '@/components/ui/Drawer';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError, Unavailable } from '@/components/ui/State';
import { SESSION_ANCHOR } from '@/data/clock';
import { cx, money, percent, relative, stampOf } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { Order, OrderLifecycleState } from '@/types/domain';

/**
 * The lifecycle, split at the broker boundary.
 *
 * Everything in the first group happened inside Mizan and never reached the broker;
 * everything in the second is the broker account of what was reported back.
 */
const LIFECYCLE_GROUPS: { label: string; states: OrderLifecycleState[] }[] = [
  {
    label: 'Before the broker',
    states: ['DRY_RUN', 'WOULD_SUBMIT'],
  },
  {
    label: 'At Alpaca Paper',
    states: ['SUBMITTED', 'NEW', 'PARTIALLY_FILLED', 'FILLED', 'REJECTED', 'CANCELED', 'EXPIRED'],
  },
];

/**
 * Orders, lifecycle-first.
 *
 * The filter row is the lifecycle vocabulary itself, so the states an operator
 * needs to reason about are visible before any row is read.
 */
export function Orders() {
  const { data, loading, error } = useAsync(() => api.listOrders(), []);
  const [state, setState] = useState<OrderLifecycleState | 'all'>('all');
  const [open, setOpen] = useState<Order | null>(null);

  const counts = useMemo(() => {
    const list = data ?? [];
    const map = new Map<string, number>();
    list.forEach((o) => map.set(o.lifecycle, (map.get(o.lifecycle) ?? 0) + 1));
    return map;
  }, [data]);

  const rows = useMemo(() => {
    const list = data ?? [];
    return state === 'all' ? list : list.filter((o) => o.lifecycle === state);
  }, [data, state]);

  const columns: Column<Order>[] = [
    {
      key: 'symbol',
      header: 'Instrument',
      card: 'title',
      sortValue: (o) => o.symbol,
      render: (o) => (
        <div className="cell-instrument">
          <span className="cell-instrument__sym u-mono">{o.symbol}</span>
          <span className="cell-instrument__type">
            {o.orderClass === 'mleg' ? 'Multi-leg option' : 'Equity'} · {o.executionMode === 'ALPACA_PAPER_DRY_RUN' ? 'dry run' : 'paper'}
          </span>
          {/* The client order ID is a reference, not a reading. It had its own
              right-hand column, where it wrapped to three lines and tripled the
              row height on a density surface. */}
          <code className="cell-instrument__ref u-mono">{o.clientOrderId}</code>
        </div>
      ),
    },
    {
      key: 'side',
      header: 'Side',
      card: 'meta',
      sortValue: (o) => o.side,
      render: (o) => <span className={cx('side', o.side === 'SELL' && 'side--sell')}>{o.side}</span>,
    },
    {
      /* The signature device, restored. Split Requested/Authorized columns made
         a reduction something the reader had to reconstruct by comparing two
         non-adjacent figures, with colour doing the work alone. */
      key: 'authorization',
      header: 'Requested → authorized',
      sortValue: (o) => o.approvedQuantity - o.proposedQuantity,
      render: (o) => (
        <QuantityLedger
          proposed={o.proposedQuantity}
          approved={o.approvedQuantity}
          unit={o.orderClass === 'mleg' ? 'contracts' : 'shares'}
          size="sm"
        />
      ),
    },
    {
      key: 'fill',
      header: 'Fill progress',
      boundary: true,
      numeric: true,
      sortValue: (o) => (o.approvedQuantity ? o.filledQuantity / o.approvedQuantity : 0),
      render: (o) => (
        <span className="fillcell">
          <span className="fillcell__text">
            {o.filledQuantity}/{o.approvedQuantity}
          </span>
          <span className="fillcell__track" aria-hidden="true">
            <span
              className="fillcell__fill"
              style={{ width: `${o.approvedQuantity ? (o.filledQuantity / o.approvedQuantity) * 100 : 0}%` }}
            />
          </span>
          <span className="u-sr-only">
            {percent(o.approvedQuantity ? o.filledQuantity / o.approvedQuantity : 0, 0)} filled
          </span>
        </span>
      ),
    },
    {
      key: 'avg',
      header: 'Avg fill',
      numeric: true,
      sortValue: (o) => o.filledAvgPrice ?? -1,
      render: (o) => (o.filledAvgPrice === null ? <Unavailable reason="Nothing has filled yet." /> : money(o.filledAvgPrice)),
    },
    {
      key: 'lifecycle',
      header: 'Lifecycle',
      sortValue: (o) => o.lifecycle,
      render: (o) => <OrderStateBadge state={o.lifecycle} />,
    },
    {
      key: 'broker',
      header: 'Broker status',
      sortValue: (o) => o.brokerStatus ?? '',
      render: (o) => (o.brokerStatus ? <code className="u-mono">{o.brokerStatus}</code> : <span className="u-dim">Never sent</span>),
    },
    {
      key: 'submitted',
      header: 'Submitted',
      align: 'right',
      sortValue: (o) => o.submittedAt ?? '',
      render: (o) =>
        o.submittedAt ? (
          <time className="u-dim" dateTime={o.submittedAt}>
            {relative(o.submittedAt, SESSION_ANCHOR)}
          </time>
        ) : (
          <span className="u-dim">—</span>
        ),
    },
  ];

  return (
    <div className="page">
      <PageHeader
        eyebrow="Execution"
        title="Orders"
        description="What was proposed, what was authorized, and what the broker actually did with it. The brass rule in the table marks the seam between what occurred before the broker and what was reported at Alpaca Paper."
        actions={
          <Badge
            tone="paper"
            shape="diamond"
            size="md"
            title="Isolated Alpaca Paper environment. Live broker submission is disabled and has no configuration path."
          >
            ALPACA PAPER
          </Badge>
        }
      />

      <Panel flush>
        <div className="toolbar">
          <div className="lifefilter" role="group" aria-label="Filter by lifecycle state">
            <button
              className={cx('segmented__btn', 'lifefilter__all', state === 'all' && 'is-active')}
              onClick={() => setState('all')}
              aria-pressed={state === 'all'}
            >
              All
              <span className="segmented__count">{data?.length ?? 0}</span>
            </button>

            {LIFECYCLE_GROUPS.map((group) => {
              const activeStates = group.states.filter((s) => counts.get(s) || state === s);
              if (activeStates.length === 0) return null;
              return (
                <div className="lifefilter__group" key={group.label}>
                  <span className="lifefilter__label">{group.label}</span>
                  <div className="segmented segmented--scroll">
                    {activeStates.map((s) => (
                      <button
                        key={s}
                        className={cx('segmented__btn', state === s && 'is-active', !counts.get(s) && 'is-empty')}
                        onClick={() => setState(s)}
                        aria-pressed={state === s}
                      >
                        {orderStateLabel(s)}
                        <span className="segmented__count">{counts.get(s) ?? 0}</span>
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {loading && <Loading label="Loading orders" />}
        {error && <LoadError error={error} />}
        {data && (
          <DataTable
            caption="Paper orders and their lifecycle"
            columns={columns}
            rows={rows}
            rowKey={(o) => o.clientOrderId}
            onRowActivate={(o) => setOpen(o)}
            emptyMessage="No orders in this state."
            initialSort={{ key: 'submitted', direction: 'desc' }}
          />
        )}
      </Panel>

      <Drawer
        open={open !== null}
        onClose={() => setOpen(null)}
        title={open ? `${open.symbol} · ${orderStateLabel(open.lifecycle)}` : 'Order'}
        subtitle={open ? <code className="u-mono">{open.clientOrderId}</code> : undefined}
        size="lg"
      >
        {open && (
          <>
            <dl className="kv">
              <div className="kv__row">
                <dt>Proposal</dt>
                <dd>
                  <Link className="u-mono" to={`/app/proposals/${open.proposalId}`}>
                    {open.proposalId}
                  </Link>
                </dd>
              </div>
              <div className="kv__row">
                <dt>Broker order ID</dt>
                <dd>{open.brokerOrderId ? <code className="u-mono">{open.brokerOrderId}</code> : <Unavailable reason="Never reached the broker." />}</dd>
              </div>
              <div className="kv__row">
                <dt>Execution mode</dt>
                <dd>
                  <Badge tone={open.executionMode === 'ALPACA_PAPER_DRY_RUN' ? 'neutral' : 'accent'}>
                    {open.executionMode === 'ALPACA_PAPER_DRY_RUN' ? 'Paper dry run' : 'Alpaca Paper'}
                  </Badge>
                </dd>
              </div>
              <div className="kv__row">
                <dt>Quantities</dt>
                <dd>
                  {open.proposedQuantity} proposed · {open.approvedQuantity} approved · {open.filledQuantity} filled
                </dd>
              </div>
              <div className="kv__row">
                <dt>Average fill</dt>
                <dd>{open.filledAvgPrice === null ? <Unavailable reason="Nothing has filled yet." /> : money(open.filledAvgPrice)}</dd>
              </div>
              <div className="kv__row">
                <dt>Last update</dt>
                <dd>
                  <time dateTime={open.updatedAt}>{stampOf(open.updatedAt)} UTC</time>
                </dd>
              </div>
              <div className="kv__row">
                <dt>Source</dt>
                <dd>
                  <ProvenanceBadge value={open.provenance} size="xs" />
                </dd>
              </div>
            </dl>

            <h3 className="drawer__section">Lifecycle</h3>
            <ol className="lifecycle">
              {open.timeline.map((step) => (
                <li key={`${step.at}-${step.label}`} className={cx('lifecycle__step', `is-${step.state}`)}>
                  <span className="lifecycle__marker" aria-hidden="true" />
                  <div className="lifecycle__body">
                    <div className="lifecycle__head">
                      <span className="lifecycle__label">{step.label}</span>
                      <span className="lifecycle__actor">{step.actor}</span>
                      <time className="u-mono lifecycle__time" dateTime={step.at}>
                        {stampOf(step.at)}
                      </time>
                    </div>
                    <p className="lifecycle__detail">{step.detail}</p>
                    <span className="u-sr-only">
                      {step.state === 'done'
                        ? 'Completed'
                        : step.state === 'current'
                          ? 'In progress'
                          : step.state === 'failed'
                            ? 'Failed'
                            : 'Pending'}
                    </span>
                  </div>
                </li>
              ))}
            </ol>
          </>
        )}
      </Drawer>
    </div>
  );
}
