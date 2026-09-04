import type { ReactNode } from 'react';
import { useState } from 'react';
import { cx, integer, latency } from '@/lib/format';
import { STATE_LABEL, STATE_TONE } from '@/data/pipeline';
import type { PipelineStage, PipelineStageId } from '@/types/domain';
import { ProvenanceBadge } from './ProvenanceBadge';

/**
 * The governance datum.
 *
 * This is the governance boundary, promoted from an ornament inside a widget to
 * the structural spine of the page. Every operational surface is composed
 * *across* it: everything above the datum is a claim — what an agent requested,
 * scored, or asserted — and everything below it is a ruling, measured and
 * recorded by policy.
 *
 * The rule itself changes material at the boundary point. Its intelligence half
 * is a plain hairline; its governance half is brass. A reader does not have to
 * decode a legend to see where authority begins — the line stops being ordinary
 * exactly there. Brass stays scarce: one datum per page, and nothing else on
 * the page is brass.
 *
 * The eight pipeline stages are not a separate device. They are the datum's
 * calibration: four ticks on the hairline half, four on the brass half, with
 * the boundary marker sitting between `trader` and `hard_risk` where a
 * suggestion becomes a TradeProposal subject to policy. One object, one
 * meaning, instead of a boundary widget and a pipeline widget that happen to
 * describe the same fact.
 */

interface DatumProps {
  /** The eight stages. Omitted on pages where the datum carries no trace. */
  stages?: PipelineStage[];
  /** What the line marks. Always rendered; the device never appears unlabelled. */
  label?: string;
  /**
   * What the two registers are on this page. The device always separates a
   * claim from an authority, but the pages name that differently: intelligence
   * and authority on the dashboard and the case file, policy-as-written and
   * policy-as-enforced in the Risk Center, reconstruction and record in Audit.
   * The brass half is always the authority side.
   */
  registers?: [string, string];
  /** The register beginning below the line, e.g. "Policy decides from here". */
  caption?: ReactNode;
  /** Right-hand readout: a verdict, a record reference, a count. */
  readout?: ReactNode;
  selectable?: boolean;
  activeStage?: PipelineStageId | null;
  onSelect?: (id: PipelineStageId) => void;
  /** Renders the selected stage's detail beneath the line. */
  showDetail?: boolean;
  size?: 'md' | 'sm';
  className?: string;
}

export function Datum({
  stages,
  label = 'Governance boundary',
  registers = ['Intelligence', 'Authority'],
  caption,
  readout,
  selectable = false,
  activeStage,
  onSelect,
  showDetail = false,
  size = 'md',
  className,
}: DatumProps) {
  const [internalActive, setInternalActive] = useState<PipelineStageId | null>(null);
  const active = activeStage !== undefined ? activeStage : internalActive;
  const selected = stages?.find((s) => s.id === active) ?? null;

  function select(id: PipelineStageId) {
    if (!selectable) return;
    if (onSelect) onSelect(id);
    else setInternalActive((current) => (current === id ? null : id));
  }

  const intelligence = stages?.filter((s) => s.layer === 'intelligence') ?? [];
  const governance = stages?.filter((s) => s.layer === 'governance') ?? [];

  return (
    <section
      className={cx('datum', `datum--${size}`, !stages && 'datum--bare', className)}
      aria-label={label}
    >
      <div className="datum__grid">
        <Half
          side="intelligence"
          name={registers[0]}
          stages={intelligence}
          active={active}
          selectable={selectable}
          onSelect={select}
        />

        <div className="datum__mark">
          <span className="datum__tick" aria-hidden="true" />
          <p className="datum__label">{label}</p>
        </div>

        <Half
          side="governance"
          name={registers[1]}
          stages={governance}
          active={active}
          selectable={selectable}
          onSelect={select}
        />
      </div>

      {(caption || readout) && (
        <div className="datum__foot">
          {caption && <p className="datum__caption">{caption}</p>}
          {readout && <div className="datum__readout">{readout}</div>}
        </div>
      )}

      {showDetail && (
        <div className="datum__detail" aria-live="polite">
          {selected ? (
            <>
              <div className="datum__detail-head">
                <span className="u-label">{selected.actor}</span>
                <span className={cx('datum__detail-state', `is-${STATE_TONE[selected.state]}`)}>
                  {STATE_LABEL[selected.state]}
                </span>
                {selected.provenance && <ProvenanceBadge value={selected.provenance} size="xs" />}
                {selected.latencyMs !== undefined && (
                  <span className="datum__detail-latency u-mono">{latency(selected.latencyMs)}</span>
                )}
                {selected.quantityOut !== undefined && (
                  <span className="datum__detail-qty u-mono">
                    {integer(selected.quantityOut)} out
                  </span>
                )}
              </div>
              <p className="datum__detail-text">
                {selected.detail ?? 'No detail recorded for this stage.'}
              </p>
            </>
          ) : (
            <p className="datum__detail-hint">
              {selectable
                ? 'Select a stage to see what it did and what it changed.'
                : 'Intelligence proposes. Policy decides the size. Execution checks again before the broker.'}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function Half({
  side,
  name,
  stages,
  active,
  selectable,
  onSelect,
}: {
  side: 'intelligence' | 'governance';
  name: string;
  stages: PipelineStage[];
  active: PipelineStageId | null;
  selectable: boolean;
  onSelect: (id: PipelineStageId) => void;
}) {
  return (
    <div className={cx('datum__half', `datum__half--${side}`)} data-layer={side}>
      <p className="datum__register">{name}</p>
      <ol className="datum__stages">
        {stages.map((stage) => {
          const Cell = selectable ? 'button' : 'div';
          return (
            <li key={stage.id} className="datum__stageitem">
              <Cell
                className={cx(
                  'datum__stage',
                  `is-${STATE_TONE[stage.state]}`,
                  `is-state-${stage.state.toLowerCase()}`,
                  active === stage.id && 'is-active',
                )}
                onClick={selectable ? () => onSelect(stage.id) : undefined}
                aria-pressed={selectable ? active === stage.id : undefined}
                aria-label={`${stage.label}: ${STATE_LABEL[stage.state]}`}
                type={selectable ? 'button' : undefined}
              >
                <span className="datum__stagelabel">{stage.label}</span>
                <span className="datum__stagestate">{STATE_LABEL[stage.state]}</span>
                <span className="datum__marker" aria-hidden="true">
                  <span className="datum__marker-shape" />
                </span>
                <span className="datum__stageqty u-mono">
                  {stage.quantityOut !== undefined ? integer(stage.quantityOut) : ''}
                </span>
              </Cell>
            </li>
          );
        })}
        {stages.length === 0 && <li className="datum__stageitem datum__stageitem--empty" aria-hidden="true" />}
      </ol>
    </div>
  );
}
