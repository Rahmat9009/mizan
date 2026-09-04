import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { Badge } from '@/components/ui/Badge';
import type { Tone } from '@/components/ui/Badge';
import { PageHeader } from '@/components/ui/PageHeader';
import { Panel } from '@/components/ui/Panel';
import { Loading, LoadError, Unavailable } from '@/components/ui/State';
import { SESSION_ANCHOR } from '@/data/clock';
import { STAGE_META, STAGE_ORDER } from '@/data/pipeline';
import { cx, latency, relative } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { AgentCardModel, AgentStatus, PipelineLayer } from '@/types/domain';

const STATUS_TONE: Record<AgentStatus, Tone> = {
  ACTIVE: 'ok',
  IDLE: 'neutral',
  ERROR: 'danger',
  NOT_CONFIGURED: 'warn',
};

const STATUS_LABEL: Record<AgentStatus, string> = {
  ACTIVE: 'Active',
  IDLE: 'Idle',
  ERROR: 'Error',
  NOT_CONFIGURED: 'Not configured',
};

/** The stage's position in the eight-stage lifecycle, printed as `01`…`08`. */
function ordinalOf(id: AgentCardModel['id']): string {
  return String(STAGE_ORDER.indexOf(id) + 1).padStart(2, '0');
}

/**
 * The agent operations surface.
 *
 * This is observability, not a roster: two aligned telemetry lanes reading the
 * same six columns, in lifecycle order, split by the one boundary that matters.
 * Every column is a field the adapter actually returns — state, the work in
 * hand, the proposal being consumed, measured latency, and the last completed
 * action. What a stage *is*, its running count and its recent history sit one
 * disclosure down, so the lane itself stays a scan of current operations.
 */
export function Agents() {
  const { data, loading, error } = useAsync(() => api.listAgents(), []);

  /* Read in lifecycle order rather than adapter order: the ordinal printed
     beside each stage is its position in the pipeline, so the two must agree. */
  const ordered = STAGE_ORDER.map((id) => (data ?? []).find((a) => a.id === id)).filter(
    (a): a is AgentCardModel => Boolean(a),
  );
  const lane = (layer: PipelineLayer) => ordered.filter((a) => a.layer === layer);

  return (
    <div className="page page--agents">
      <PageHeader
        eyebrow="System"
        title="Agents"
        description="Eight stages, four on each side of the boundary. Current state, work in hand, latency and last action for each."
      />

      {loading && <Loading label="Loading agents" />}
      {error && <LoadError error={error} />}

      {ordered.length > 0 && (
        <>
          <TelemetryLane
            eyebrow="Intelligence"
            title="Market intelligence"
            description="Upstream stages. They produce TradeProposals and never reach the broker."
            rows={lane('intelligence')}
          />

          <div className="boundary-rule" role="separator" aria-label="Governance boundary">
            <span className="boundary-rule__label">TradeProposal · governance boundary</span>
          </div>

          <TelemetryLane
            eyebrow="Authority"
            title="Portfolio governance"
            description={"Downstream stages. They decide size, authorization and whether anything reaches Alpaca Paper at all."}
            rows={lane('governance')}
          />
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- lane */

function TelemetryLane({
  eyebrow,
  title,
  description,
  rows,
}: {
  eyebrow: string;
  title: string;
  description: string;
  rows: AgentCardModel[];
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const toggle = (id: string) => setExpanded((current) => (current === id ? null : id));

  return (
    <Panel eyebrow={eyebrow} title={title} description={description} flush className="tel">
      {/* Wide: the full operational table. */}
      <table className="tel__table">
        <caption className="u-sr-only">{`${title} — stage telemetry`}</caption>
        <thead>
          <tr>
            <th scope="col" className="tel__col-stage">
              Stage
            </th>
            <th scope="col" className="tel__col-state">
              State
            </th>
            <th scope="col" className="tel__col-doing">
              Currently doing
            </th>
            <th scope="col" className="tel__col-consumes">
              Consumes
            </th>
            <th scope="col" className="tel__col-latency is-right">
              Latency
            </th>
            <th scope="col" className="tel__col-last">
              Last action
            </th>
          </tr>
        </thead>
        {rows.map((agent) => {
          const open = expanded === agent.id;
          return (
            <tbody key={agent.id} className={cx('tel__group', open && 'is-open')}>
              <tr className={cx('tel__row', `is-${agent.status.toLowerCase()}`)}>
                <th scope="row" className="tel__stagecell">
                  <button
                    type="button"
                    className="tel__disclose"
                    aria-expanded={open}
                    aria-controls={`tel-detail-${agent.id}`}
                    onClick={() => toggle(agent.id)}
                  >
                    <ChevronRight size={13} aria-hidden="true" className="tel__chevron" />
                    <span className="tel__ord u-mono">{ordinalOf(agent.id)}</span>
                    <span className="tel__stage">{STAGE_META[agent.id].label}</span>
                    <span className="u-sr-only">
                      {open ? '— hide stage detail' : '— show stage detail'}
                    </span>
                  </button>
                  <span className="tel__actor">{agent.name}</span>
                </th>
                <td className="tel__statecell">
                  <Badge tone={STATUS_TONE[agent.status]}>{STATUS_LABEL[agent.status]}</Badge>
                </td>
                <td className="tel__doing">
                  {agent.currentAction ?? <span className="u-dim">Waiting for work</span>}
                </td>
                <td className="tel__consumes">
                  {agent.currentProposalId ? (
                    <Link className="u-mono" to={`/app/proposals/${agent.currentProposalId}`}>
                      {agent.currentProposalId}
                    </Link>
                  ) : (
                    <span className="u-dim">Nothing in hand</span>
                  )}
                </td>
                <td className="tel__latency is-right is-num">
                  {agent.latencyMs === null ? (
                    <Unavailable reason="No completed run to measure." />
                  ) : (
                    latency(agent.latencyMs)
                  )}
                </td>
                <td className="tel__last">
                  <span className="tel__lasttext">{agent.lastAction}</span>
                  <time className="tel__lastat u-mono" dateTime={agent.lastActionAt}>
                    {relative(agent.lastActionAt, SESSION_ANCHOR)}
                  </time>
                </td>
              </tr>
              <tr className="tel__detailrow" id={`tel-detail-${agent.id}`} hidden={!open}>
                <td colSpan={6}>
                  <StageDetail agent={agent} />
                </td>
              </tr>
            </tbody>
          );
        })}
      </table>

      {/* Narrow: the same readings, stacked in priority order. Nothing is
          dropped and nothing is truncated — stage, state and current action
          lead, then latency, what it consumes, and the last action. */}
      <ul className="tel__stack">
        {rows.map((agent) => {
          const open = expanded === agent.id;
          return (
            <li key={agent.id} className={cx('tel__card', `is-${agent.status.toLowerCase()}`)}>
              <div className="tel__cardhead">
                <button
                  type="button"
                  className="tel__disclose"
                  aria-expanded={open}
                  aria-controls={`tel-detail-sm-${agent.id}`}
                  onClick={() => toggle(agent.id)}
                >
                  <ChevronRight size={13} aria-hidden="true" className="tel__chevron" />
                  <span className="tel__ord u-mono">{ordinalOf(agent.id)}</span>
                  <span className="tel__stage">{STAGE_META[agent.id].label}</span>
                  <span className="u-sr-only">
                    {open ? '— hide stage detail' : '— show stage detail'}
                  </span>
                </button>
                <Badge tone={STATUS_TONE[agent.status]}>{STATUS_LABEL[agent.status]}</Badge>
              </div>
              <p className="tel__actor">{agent.name}</p>

              <dl className="tel__fields">
                <div>
                  <dt>Current action</dt>
                  <dd>{agent.currentAction ?? <span className="u-dim">Waiting for work</span>}</dd>
                </div>
                <div>
                  <dt>Latency</dt>
                  <dd className="is-num">
                    {agent.latencyMs === null ? (
                      <Unavailable reason="No completed run to measure." />
                    ) : (
                      latency(agent.latencyMs)
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Consumes</dt>
                  <dd>
                    {agent.currentProposalId ? (
                      <Link className="u-mono" to={`/app/proposals/${agent.currentProposalId}`}>
                        {agent.currentProposalId}
                      </Link>
                    ) : (
                      <span className="u-dim">Nothing in hand</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Last action</dt>
                  <dd>
                    {agent.lastAction}{' '}
                    <time className="u-dim u-mono" dateTime={agent.lastActionAt}>
                      {relative(agent.lastActionAt, SESSION_ANCHOR)}
                    </time>
                  </dd>
                </div>
              </dl>

              <div id={`tel-detail-sm-${agent.id}`} hidden={!open}>
                <StageDetail agent={agent} />
              </div>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

/* ----------------------------------------------------------------- detail */

/**
 * What the stage is, what it has counted today, and what it last did — the
 * reference material the lane deliberately keeps out of the scan.
 */
function StageDetail({ agent }: { agent: AgentCardModel }) {
  return (
    <div className="tel__detail">
      <p className="tel__role">{agent.role}</p>

      <div className="tel__detailgrid">
        {agent.metric && (
          <div className="tel__metric">
            <span className="tel__metriclabel">{agent.metric.label}</span>
            <span className="tel__metricvalue">{agent.metric.value}</span>
          </div>
        )}
        <div className="tel__metric">
          <span className="tel__metriclabel">Reading source</span>
          <span className="tel__metricvalue">
            <ProvenanceBadge value={agent.provenance} size="xs" />
          </span>
        </div>
      </div>

      <div className="tel__events">
        <p className="tel__eventshead">Recent events</p>
        <ul>
          {agent.recentEvents.map((e) => (
            <li key={`${e.at}-${e.text}`}>
              <time className="u-mono" dateTime={e.at}>
                {relative(e.at, SESSION_ANCHOR)}
              </time>
              <span>{e.text}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
