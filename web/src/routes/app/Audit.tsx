import { useMemo, useState } from 'react';
import { AuditTimeline, ACTOR_LABEL } from '@/components/domain/AuditTimeline';
import { DecisionReplay } from '@/components/domain/DecisionReplay';
import { PageHeader } from '@/components/ui/PageHeader';
import { SectionRule } from '@/components/ui/SectionRule';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError } from '@/components/ui/State';
import { cx } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { AuditEvent } from '@/types/domain';

const OUTCOMES: { id: AuditEvent['outcome'] | 'all'; label: string }[] = [
  { id: 'all', label: 'All outcomes' },
  { id: 'ok', label: 'Passed' },
  { id: 'watch', label: 'Watch' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'info', label: 'Informational' },
];

/**
 * The audit view.
 *
 * Two ways into the same record: a filterable forensic timeline for
 * investigation, and Decision Replay for reconstructing one proposal step by
 * step from research signal to broker outcome.
 */
export function Audit() {
  const [outcome, setOutcome] = useState<AuditEvent['outcome'] | 'all'>('all');
  const [actor, setActor] = useState<string>('all');
  const [symbol, setSymbol] = useState<string>('all');
  const [query, setQuery] = useState('');

  const events = useAsync(() => api.listAuditEvents(), []);
  const proposals = useAsync(() => api.listProposals(), []);

  const [replayId, setReplayId] = useState<string>('sel-20260902-nvda-0114');
  const replayEvents = useAsync(() => api.getProposalAudit(replayId), [replayId]);
  const replayProposal = useAsync(() => api.getProposal(replayId), [replayId]);

  const symbols = useMemo(() => Array.from(new Set((events.data ?? []).map((e) => e.symbol))).sort(), [events.data]);
  const actors = useMemo(() => Array.from(new Set((events.data ?? []).map((e) => e.actor))), [events.data]);

  const filtered = useMemo(() => {
    let list = events.data ?? [];
    if (outcome !== 'all') list = list.filter((e) => e.outcome === outcome);
    if (actor !== 'all') list = list.filter((e) => e.actor === actor);
    if (symbol !== 'all') list = list.filter((e) => e.symbol === symbol);
    if (query.trim()) {
      const needle = query.toLowerCase();
      list = list.filter((e) => `${e.summary} ${e.action} ${e.proposalId}`.toLowerCase().includes(needle));
    }
    return list;
  }, [events.data, outcome, actor, symbol, query]);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Record"
        title="Audit"
        description="Every stage writes a durable, sanitised event. Credentials, raw broker objects and storage paths are never audit fields."
      />

      <Panel
        eyebrow="Reconstruction"
        title="Decision replay"
        description="Reconstruct one proposal from the first research event to the broker outcome."
        actions={
          <label className="fieldinline">
            <span className="u-sr-only">Proposal to replay</span>
            <select className="select" value={replayId} onChange={(e) => setReplayId(e.target.value)}>
              {(proposals.data ?? []).map((p) => (
                <option key={p.proposalId} value={p.proposalId}>
                  {p.instrument.type === 'equity'
                    ? `${p.instrument.side} ${p.instrument.symbol}`
                    : `${p.instrument.underlying} spread`}{' '}
                  · {p.proposalId}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {(replayEvents.loading || replayProposal.loading) && <Loading label="Loading replay" />}
        {replayProposal.data && replayEvents.data && (
          <DecisionReplay proposal={replayProposal.data} events={replayEvents.data} />
        )}
      </Panel>

      {/* Not the governance boundary. This divides the decision as reconstructed
          from the durable record — a division of the document, not the point
          where intelligence ends and authority begins. The datum inside the
          replay above already marks that point, and it marks it truthfully,
          across the eight stages. Declaring it a second time here turned the
          product's scarcest device into a page furniture. So this is an
          ordinary hairline in ordinary ink, and it says what it separates. */}
      <SectionRule
        registers={['Reconstruction', 'Record']}
        caption="Above the rule, the decision reconstructed — market state, prompts and checks as they stood at the moment of the verdict. Below it, the durable record itself, which is what a supervisor is entitled to read."
      />

      <Panel eyebrow="Forensics" title="Event timeline" flush>
        <div className="toolbar toolbar--wrap">
          <div className="segmented" role="group" aria-label="Filter by outcome">
            {OUTCOMES.map((o) => (
              <button
                key={o.id}
                className={cx('segmented__btn', outcome === o.id && 'is-active')}
                onClick={() => setOutcome(o.id)}
                aria-pressed={outcome === o.id}
              >
                {o.label}
              </button>
            ))}
          </div>

          <label className="fieldinline">
            <span className="u-sr-only">Filter by actor</span>
            <select className="select" value={actor} onChange={(e) => setActor(e.target.value)}>
              <option value="all">All actors</option>
              {actors.map((a) => (
                <option key={a} value={a}>
                  {ACTOR_LABEL[a]}
                </option>
              ))}
            </select>
          </label>

          <label className="fieldinline">
            <span className="u-sr-only">Filter by symbol</span>
            <select className="select" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              <option value="all">All symbols</option>
              {symbols.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>

          <label className="searchfield">
            <span className="u-sr-only">Search events</span>
            <input
              className="input"
              placeholder="Filter by action, summary or proposal ID"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        </div>

        <div className="panel__inner">
          {events.loading && <Loading label="Loading audit trail" />}
          {events.error && <LoadError error={events.error} />}
          {events.data && <AuditTimeline events={filtered} />}
        </div>
      </Panel>
    </div>
  );
}
