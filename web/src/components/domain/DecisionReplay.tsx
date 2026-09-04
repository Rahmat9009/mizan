import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Link2, Pause, Play, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { cx, integer, stampOf } from '@/lib/format';
import { usePrefersReducedMotion } from '@/lib/hooks';
import type { AuditEvent, Proposal } from '@/types/domain';
import { ACTOR_LABEL } from './AuditTimeline';
import { Datum } from './Datum';
import { PayloadTable } from './RiskChecks';

const OUTCOME_LABEL: Record<AuditEvent['outcome'], string> = {
  ok: 'Passed',
  watch: 'Watch',
  blocked: 'Blocked',
  info: 'Informational',
};

/**
 * Decision Replay, as evidence reconstruction.
 *
 * Reconstructs the proposal decision from persisted audit records rather than
 * an AI retelling:
 *
 *   ROUTE     the stage traversal across the governance boundary datum
 *   EVIDENCE  what happened: event timestamp, actor, action, outcome, payload
 *   PROOF     what persisted record supports it: event id, chain position, hashes, verification
 *
 * Step-first transport; autoplay respects prefers-reduced-motion.
 */
export function DecisionReplay({ proposal, events }: { proposal: Proposal; events: AuditEvent[] }) {
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const reducedMotion = usePrefersReducedMotion();

  const total = events.length;
  const current = events[Math.min(index, total - 1)];

  useEffect(() => {
    setIndex(0);
    setPlaying(false);
  }, [proposal.proposalId]);

  useEffect(() => {
    if (!playing) return;
    if (index >= total - 1) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setIndex((i) => Math.min(i + 1, total - 1)), 1400);
    return () => clearTimeout(timer);
  }, [playing, index, total]);

  if (total === 0) {
    return <p className="statebox statebox--empty">No stored events for this proposal.</p>;
  }

  // Stages up to and including the current event are "played"; the rest are
  // shown idle so the operator can see how far the reconstruction has reached.
  const playedStages = proposal.stages.map((s) => {
    const reached = events.slice(0, index + 1).some((e) => e.stage === s.id);
    return reached ? s : { ...s, state: 'IDLE' as const };
  });

  return (
    <div className="replay">
      <div className="replay__route">
        <p className="replay__lane">Route</p>
        <Datum stages={playedStages} size="sm" />
      </div>

      <div className="replay__transport">
        <div className="replay__buttons">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setIndex(0);
              setPlaying(false);
            }}
            aria-label="Restart replay"
            iconLeft={<RotateCcw size={13} aria-hidden="true" />}
          >
            Restart
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setIndex((i) => Math.max(0, i - 1))}
            disabled={index === 0}
            aria-label="Previous step"
            iconLeft={<ChevronLeft size={13} aria-hidden="true" />}
          >
            Back
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setIndex((i) => Math.min(total - 1, i + 1))}
            disabled={index >= total - 1}
            aria-label="Next step"
            iconRight={<ChevronRight size={13} aria-hidden="true" />}
          >
            Step
          </Button>
          {!reducedMotion && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setPlaying((p) => !p)}
              disabled={index >= total - 1}
              aria-label={playing ? 'Pause replay' : 'Play replay'}
              iconLeft={playing ? <Pause size={13} aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
            >
              {playing ? 'Pause' : 'Play'}
            </Button>
          )}
        </div>

        <ol className="replay__track" aria-label="Recorded events">
          {events.map((event, i) => (
            <li key={event.eventId}>
              <button
                className={cx(
                  'replay__tick',
                  `is-${event.outcome}`,
                  i === index && 'is-current',
                  i < index && 'is-played',
                )}
                onClick={() => {
                  setIndex(i);
                  setPlaying(false);
                }}
                aria-current={i === index ? 'step' : undefined}
                aria-label={`Step ${i + 1} of ${total}: ${event.action}, ${OUTCOME_LABEL[event.outcome]}`}
                title={`${stampOf(event.at)} · ${event.action} (${OUTCOME_LABEL[event.outcome]})`}
              />
            </li>
          ))}
        </ol>

        <p className="replay__progress" aria-live="polite">
          Step <span className="u-mono">{index + 1}</span> of <span className="u-mono">{total}</span>
        </p>
      </div>

      <div className="replay__split">
        <section className="replay__evidence" aria-live="polite">
          <p className="replay__lane">Evidence</p>
          <div className="replay__stage-head">
            <time className="u-mono" dateTime={current.at}>
              {stampOf(current.at)} UTC
            </time>
            <span className="replay__stage-actor">{ACTOR_LABEL[current.actor]}</span>
            <code className="replay__stage-action u-mono">{current.action}</code>
            <span className={cx('replay__outcome', `is-${current.outcome}`)}>{OUTCOME_LABEL[current.outcome]}</span>
          </div>
          <p className="replay__stage-summary">{current.summary}</p>
          <PayloadTable payload={current.payload} />
        </section>

        <section className="replay__proof">
          <p className="replay__lane">Proof</p>
          <dl className="replay__proofrows">
            <div>
              <dt>Event</dt>
              <dd>
                <code className="u-mono u-break">{current.eventId}</code>
              </dd>
            </div>
            <div>
              <dt>Chain position</dt>
              <dd>
                {proposal.chain ? (
                  <span className="u-mono">{integer(proposal.chain.position)}</span>
                ) : (
                  <span className="u-dim">Unavailable</span>
                )}
              </dd>
            </div>
            <div>
              <dt>Record hash</dt>
              <dd>
                {proposal.chain ? (
                  <code className="u-mono u-break">{proposal.chain.recordHash}</code>
                ) : (
                  <span className="u-dim">Unavailable</span>
                )}
              </dd>
            </div>
            <div>
              <dt>Previous hash</dt>
              <dd>
                {proposal.chain ? (
                  <code className="u-mono u-break">{proposal.chain.previousHash}</code>
                ) : (
                  <span className="u-dim">Unavailable</span>
                )}
              </dd>
            </div>
          </dl>
          <p className={cx('replay__verified', proposal.chain?.verified ? 'is-ok' : 'is-bad')}>
            <Link2 size={12} aria-hidden="true" />
            {proposal.chain
              ? proposal.chain.verified
                ? 'Stored record hash verified'
                : 'Verification failed'
              : 'Verification unavailable · Not sealed in chain'}
            {proposal.chain?.verifyMs !== null && proposal.chain?.verifyMs !== undefined && (
              <span className="u-dim u-mono"> · {proposal.chain.verifyMs}ms</span>
            )}
          </p>
          <p className="replay__proofnote">
            Reconstructed from persisted audit events. Nothing here is generated at view time.
          </p>
        </section>
      </div>
    </div>
  );
}
