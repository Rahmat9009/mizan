import { REASON_TEXT } from '@/data/checks';
import { decimalCompare, decimalToRatio } from '@/lib/decimal';
import { cx, integer } from '@/lib/format';
import type { CheckMeasurement, Decision, DecisionOutcome, RiskCheck } from '@/types/domain';
import { Scale, format } from './Scale';
import type { Reading } from './Scale';

/**
 * Level 2: why the verdict was what it was.
 *
 * Rendered from the deterministic check output and nothing else — `threshold`,
 * `actual_current`, `actual_if_requested`, `actual_if_authorized`, `reason_code`.
 * The panel is therefore provably the same arithmetic the engine enforced,
 * rather than a paraphrase of it. An LLM's account of a decision may appear at
 * L4 marked advisory; it never appears here.
 *
 * The picture is now a single calibrated instrument per check rather than a
 * stack of independent bars: one limit rule, one hatched zone beyond it, and
 * the three readings standing at their measured positions. A reader who has
 * never heard the word "concentration" sees the requested reading inside the
 * hatching and the authorized reading outside it, which is the entire product
 * in one image.
 */

interface WhyPanelProps {
  /** `null` while the proposal is still in review. */
  decision: Decision | null;
  outcome: DecisionOutcome;
  checks: RiskCheck[];
  /** The rule the Governor recorded as binding, when it recorded one. */
  bindingRule?: string | null;
}

export function WhyPanel({ decision, outcome, checks, bindingRule }: WhyPanelProps) {
  const failed = checks.filter((c) => !c.passed && c.measurement);
  const plotted =
    failed.length > 0
      ? failed
      : [tightestPassing(checks)].filter(Boolean as unknown as (c: RiskCheck | null) => c is RiskCheck);

  if (plotted.length === 0) {
    return (
      <p className="why__empty">
        No deterministic check produced a measurable quantity for this proposal, so there is no arithmetic to draw.
      </p>
    );
  }

  const sizeInvariantBlock = failed.some((c) => c.measurement?.sizeInvariant);

  return (
    <div className="why">
      <p className="why__lede">{lede(decision, outcome)}</p>

      {plotted.map((check) => (
        <CheckInstrument
          key={check.rule}
          check={check}
          outcome={outcome}
          binding={bindingRule === check.rule}
        />
      ))}

      <dl className="why__result">
        <div className="why__resultrow">
          <dt>Authorized</dt>
          <dd>
            {outcome.authorized === null
              ? 'Not yet decided'
              : `${integer(outcome.authorized.quantity)} ${outcome.unit}`}
          </dd>
        </div>
        <div className="why__resultrow">
          <dt>Reason code</dt>
          <dd>
            {failed.length === 0 ? (
              <span className="u-dim">None — every check passed</span>
            ) : (
              <code className="u-mono">
                {failed
                  .map((c) => c.reasonCode)
                  .filter(Boolean)
                  .join(' · ')}
              </code>
            )}
          </dd>
        </div>
      </dl>

      {sizeInvariantBlock && (
        <p className="why__note why__note--hard">
          At least one failing limit does not move with order size. No quantity satisfies it, which is why the verdict
          is a rejection rather than a reduction. A new proposal is required.
        </p>
      )}
      {decision !== null && <p className="why__note">No human intervention required.</p>}
    </div>
  );
}

/* ------------------------------------------------------------ one instrument */

function CheckInstrument({
  check,
  outcome,
  binding,
}: {
  check: RiskCheck;
  outcome: DecisionOutcome;
  binding: boolean;
}) {
  const m = check.measurement;
  if (!m) return null;

  const readings: Reading[] = [];
  if (m.actualCurrent !== null) {
    readings.push({ kind: 'current', label: 'Current', value: m.actualCurrent });
  }
  readings.push({
    kind: 'requested',
    label: `If requested ${integer(outcome.requested.quantity)}`,
    value: m.actualIfRequested,
  });
  if (m.actualIfAuthorized !== null && outcome.authorized) {
    readings.push({
      kind: 'authorized',
      label: `If authorized ${integer(outcome.authorized.quantity)}`,
      value: m.actualIfAuthorized,
    });
  }

  return (
    <section className={cx('why__check', binding && 'is-binding', !check.passed && 'is-failed')}>
      <header className="why__checkhead">
        <h3 className="why__checkname">{check.label}</h3>
        <span className="why__limit u-mono">
          {m.bound === 'ceiling' ? 'limit' : 'minimum'} {format(m.threshold, m.unit)}
        </span>
        {binding && <span className="why__binding">binding</span>}
      </header>

      <Scale unit={m.unit} bound={m.bound} threshold={m.threshold} readings={readings} />

      {check.reasonCode && !check.passed && (
        <p className="why__code">
          <code className="u-mono">{check.reasonCode}</code>
          <span className="u-dim"> — {REASON_TEXT[check.reasonCode] ?? check.reasonCode.replaceAll('_', ' ').toLowerCase()}</span>
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------- helpers */

/**
 * The check closest to its own limit.
 *
 * Shown when nothing failed, so an approval is explained by the same picture as
 * a reduction: here is the tightest constraint, and here is how much room the
 * trade left under it.
 *
 * Size-varying checks are preferred. A confidence floor can sit close to its
 * threshold without constraining anything an operator can act on; the useful
 * answer to "what nearly stopped this" is a limit that moves with the order.
 */
function tightestPassing(checks: RiskCheck[]): RiskCheck | null {
  const sizeVarying = checks.filter((c) => c.measurement && !c.measurement.sizeInvariant);
  const pool = sizeVarying.length > 0 ? sizeVarying : checks;

  let best: RiskCheck | null = null;
  let bestScore = -Infinity;
  for (const c of pool) {
    const m = c.measurement;
    if (!m) continue;
    const threshold = decimalToRatio(m.threshold);
    const actual = decimalToRatio(m.actualIfAuthorized ?? m.actualIfRequested);
    if (threshold === null || actual === null || threshold <= 0 || actual < 0) continue;
    // Ordering heuristic only; the values rendered come from the strings.
    const score = m.bound === 'ceiling' ? actual / threshold : threshold / Math.max(actual, 1e-9);
    if (score > bestScore) {
      bestScore = score;
      best = c;
    }
  }
  return best;
}

/** Retained for callers that need the satisfaction test without a scale. */
export function satisfies(value: CheckMeasurement['threshold'], m: CheckMeasurement): boolean {
  const cmp = decimalCompare(value, m.threshold);
  return m.bound === 'ceiling' ? cmp <= 0 : cmp >= 0;
}

/** A deterministic sentence built from the record. Never model-generated. */
function lede(decision: Decision | null, outcome: DecisionOutcome): string {
  const unit = outcome.unit;
  const requested = integer(outcome.requested.quantity);
  if (decision === null) {
    return `No verdict yet for the ${requested} ${unit} requested. The tightest constraint measured so far is shown below.`;
  }
  if (decision === 'REJECT') {
    return `Policy authorized no quantity of the ${requested} ${unit} requested.`;
  }
  if (decision === 'REDUCE' && outcome.authorized) {
    return `Policy reduced this order from ${requested} to ${integer(outcome.authorized.quantity)} ${unit}.`;
  }
  return `Policy authorized the ${requested} ${unit} requested in full. The tightest constraint is shown below.`;
}
