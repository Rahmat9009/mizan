import { useState } from 'react';
import { cx, integer, latency } from '@/lib/format';
import { STATE_LABEL, STATE_TONE } from '@/data/pipeline';
import type { PipelineStage, PipelineStageId } from '@/types/domain';
import { ProvenanceBadge } from './ProvenanceBadge';

interface AgentPipelineProps {
  stages: PipelineStage[];
  /**
   * `full` is the dashboard instrument panel with selectable stages.
   * `compact` is the strip used above a proposal case file.
   * `rail` is the smallest form, for a table cell or landing-page diagram.
   */
  variant?: 'full' | 'compact' | 'rail';
  selectable?: boolean;
  /** Stage currently in focus, when the parent wants to control it. */
  activeStage?: PipelineStageId | null;
  onSelect?: (id: PipelineStageId) => void;
  className?: string;
}

/**
 * The end-to-end pipeline: four market-intelligence stages, then four
 * governance stages, separated by the boundary where a suggestion becomes a
 * TradeProposal subject to policy.
 *
 * State is carried by a shape, a word and a colour together, so the component
 * survives both greyscale printing and colour-blind readers. The only motion
 * is a slow pulse on a stage that is genuinely running, and it stops entirely
 * under `prefers-reduced-motion`.
 */
export function AgentPipeline({
  stages,
  variant = 'full',
  selectable = false,
  activeStage,
  onSelect,
  className,
}: AgentPipelineProps) {
  const [internalActive, setInternalActive] = useState<PipelineStageId | null>(null);
  const active = activeStage !== undefined ? activeStage : internalActive;
  const selected = stages.find((s) => s.id === active) ?? null;

  function select(id: PipelineStageId) {
    if (!selectable) return;
    if (onSelect) onSelect(id);
    else setInternalActive((current) => (current === id ? null : id));
  }

  return (
    <div className={cx('pipeline', `pipeline--${variant}`, className)}>
      <ol className="pipeline__rail" aria-label="Trade decision pipeline">
        {stages.map((stage, index) => {
          const tone = STATE_TONE[stage.state];
          const isBoundary = stage.layer === 'governance' && stages[index - 1]?.layer === 'intelligence';
          const Cell = selectable ? 'button' : 'div';

          return (
            <li
              key={stage.id}
              className={cx('pipeline__item', isBoundary && 'pipeline__item--boundary')}
              data-layer={stage.layer}
            >
              {isBoundary && (
                <span className="pipeline__boundary" aria-hidden="true">
                  <span className="pipeline__boundary-line" />
                  <span className="pipeline__boundary-label">TradeProposal</span>
                </span>
              )}
              <Cell
                className={cx('pipeline__stage', `is-${tone}`, `is-state-${stage.state.toLowerCase()}`, active === stage.id && 'is-active')}
                onClick={selectable ? () => select(stage.id) : undefined}
                aria-pressed={selectable ? active === stage.id : undefined}
                aria-label={`${stage.label}: ${STATE_LABEL[stage.state]}`}
                type={selectable ? 'button' : undefined}
              >
                <span className="pipeline__marker" aria-hidden="true">
                  <span className="pipeline__marker-shape" />
                </span>
                <span className="pipeline__labels">
                  <span className="pipeline__label">{stage.label}</span>
                  <span className="pipeline__state">{STATE_LABEL[stage.state]}</span>
                </span>
                {variant === 'full' && stage.quantityOut !== undefined && (
                  <span className="pipeline__qty">{integer(stage.quantityOut)}</span>
                )}
              </Cell>
              {index < stages.length - 1 && <span className="pipeline__link" aria-hidden="true" />}
            </li>
          );
        })}
      </ol>

      {variant === 'full' && (
        <div className="pipeline__detail" aria-live="polite">
          {selected ? (
            <>
              <div className="pipeline__detail-head">
                <span className="u-label">{selected.actor}</span>
                <span className={cx('pipeline__detail-state', `is-${STATE_TONE[selected.state]}`)}>
                  {STATE_LABEL[selected.state]}
                </span>
              </div>
              <p className="pipeline__detail-text">{selected.detail ?? 'No detail recorded for this stage.'}</p>
              {/* Existing stage fields only, each one labelled: the quantity
                  leaving the stage, its measured latency, which side of the
                  boundary it sits on, and where the reading came from. */}
              <dl className="pipeline__detail-meta">
                {selected.quantityOut !== undefined && (
                  <div>
                    <dt>Quantity out</dt>
                    <dd>{integer(selected.quantityOut)}</dd>
                  </div>
                )}
                {selected.latencyMs !== undefined && (
                  <div>
                    <dt>Latency</dt>
                    <dd>{latency(selected.latencyMs)}</dd>
                  </div>
                )}
                <div>
                  <dt>Authority</dt>
                  <dd className="pipeline__detail-word">
                    {selected.layer === 'governance' ? 'Portfolio governance' : 'Market intelligence'}
                  </dd>
                </div>
                {selected.provenance && (
                  <div>
                    <dt>Source</dt>
                    <dd>
                      <ProvenanceBadge value={selected.provenance} size="xs" />
                    </dd>
                  </div>
                )}
              </dl>
            </>
          ) : (
            <p className="pipeline__detail-hint">
              {selectable
                ? 'Select a stage to see what it did and what it changed.'
                : 'Intelligence proposes. Governance decides the size. Execution checks again before the broker.'}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
