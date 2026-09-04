import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { Link } from 'react-router-dom';
import { cx, stampOf } from '@/lib/format';
import type { AuditEvent } from '@/types/domain';
import { ProvenanceBadge } from './ProvenanceBadge';
import { PayloadTable } from './RiskChecks';

const ACTOR_LABEL: Record<AuditEvent['actor'], string> = {
  research_agent: 'Market Research Agent',
  selection_agent: 'Stock Selection Agent',
  probability_agent: 'Probability Agent',
  trader_agent: 'Trader Agent',
  risk_engine: 'Deterministic Risk Engine',
  ai_risk_agent: 'AI Risk Model',
  governor: 'Portfolio Governor',
  execution: 'Execution Agent',
  broker: 'Alpaca Paper',
};

const OUTCOME_TONE: Record<AuditEvent['outcome'], string> = {
  ok: 'ok',
  watch: 'warn',
  blocked: 'danger',
  info: 'neutral',
};

/**
 * The forensic timeline.
 *
 * Every row is one durable event. Expanding a row shows the sanitised payload
 * the backend stored, which is what makes a decision reconstructable months
 * later rather than merely summarised.
 */
export function AuditTimeline({
  events,
  linkProposals = true,
  dense = false,
}: {
  events: AuditEvent[];
  linkProposals?: boolean;
  dense?: boolean;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (events.length === 0) {
    return <p className="statebox statebox--empty">No audit events match these filters.</p>;
  }

  return (
    <ol className={cx('timeline', dense && 'timeline--dense')}>
      {events.map((event) => {
        const open = expanded === event.eventId;
        return (
          <li key={event.eventId} className={cx('timeline__item', `is-${OUTCOME_TONE[event.outcome]}`)}>
            <span className="timeline__spine" aria-hidden="true">
              <span className="timeline__node" />
            </span>
            <div className="timeline__content">
              <div className="timeline__head">
                <time className="timeline__time u-mono" dateTime={event.at}>
                  {stampOf(event.at)}
                </time>
                <span className="timeline__actor">{ACTOR_LABEL[event.actor]}</span>
                <code className="timeline__action">{event.action}</code>
                <ProvenanceBadge value={event.provenance} size="xs" />
              </div>
              <p className="timeline__summary">{event.summary}</p>
              <div className="timeline__foot">
                <span className="timeline__symbol u-mono">{event.symbol}</span>
                {linkProposals ? (
                  <Link className="timeline__link u-mono" to={`/app/proposals/${event.proposalId}`}>
                    {event.proposalId}
                  </Link>
                ) : (
                  <span className="u-mono u-dim">{event.proposalId}</span>
                )}
                {event.orderId && <span className="u-mono u-dim">{event.orderId}</span>}
                <button
                  className="timeline__toggle"
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : event.eventId)}
                >
                  <ChevronRight size={12} aria-hidden="true" className={cx('timeline__chev', open && 'is-open')} />
                  {open ? 'Hide payload' : 'Show payload'}
                </button>
              </div>
              {open && (
                <div className="timeline__payload">
                  <PayloadTable payload={event.payload} />
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export { ACTOR_LABEL };
