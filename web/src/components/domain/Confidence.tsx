import { ArrowRight } from 'lucide-react';
import { ProvenanceBadge } from './ProvenanceBadge';
import { decimalFixed, decimalPercent } from '@/lib/decimal';
import { integer } from '@/lib/format';
import type { Calibration } from '@/types/domain';

/**
 * Agent-reported confidence, and what it has historically been worth.
 *
 * A self-reported confidence is an estimate with unknown error. Rendered large
 * and clean it teaches an operator to trust a number the risk engine itself
 * haircuts before use, so it is never the headline: the raw figure is small,
 * labelled as the agent's own claim, and shown beside the calibrated value
 * wherever a calibration record exists.
 *
 * Where no such record exists the absence is stated. It is not filled in.
 */
export function ConfidenceReadout({
  reported,
  calibration,
  label = 'Agent confidence',
  compact = false,
}: {
  reported: number | null;
  calibration: Calibration | null;
  label?: string;
  /** Table-cell form: the pair of figures, with the account on hover. */
  compact?: boolean;
}) {
  const raw = reported === null ? 'Unavailable' : decimalPercent(reported.toFixed(4), 0);

  if (compact) {
    return (
      <span
        className="confidence confidence--compact"
        title={
          calibration
            ? `Agent claim ${raw}. Calibrated ${decimalPercent(calibration.calibratedConfidence, 0)} — historically overconfident by ${decimalFixed(calibration.meanErrorPoints, 0)} points across ${integer(calibration.sampleSize)} decisions.`
            : `Agent claim ${raw}. No calibration record exists for this agent, so the figure has not been checked against outcomes.`
        }
      >
        <span className="confidence__raw u-mono">{raw}</span>
        {calibration ? (
          <>
            <ArrowRight size={11} aria-hidden="true" className="confidence__arrow" />
            <span className="confidence__cal u-mono">{decimalPercent(calibration.calibratedConfidence, 0)}</span>
          </>
        ) : (
          <span className="confidence__nocal">no calibration</span>
        )}
      </span>
    );
  }

  return (
    <div className="confidence">
      <div className="confidence__row">
        <span className="confidence__label">{label}</span>
        <span className="confidence__raw u-mono">{raw}</span>
        {calibration && (
          <>
            <ArrowRight size={12} aria-hidden="true" className="confidence__arrow" />
            <span className="confidence__label">calibrated</span>
            <span className="confidence__cal u-mono">{decimalPercent(calibration.calibratedConfidence, 0)}</span>
          </>
        )}
      </div>

      {calibration ? (
        <p className="confidence__note">
          This agent has historically been overconfident by {decimalFixed(calibration.meanErrorPoints, 0)} points
          across {integer(calibration.sampleSize)} decisions.{' '}
          <ProvenanceBadge value={calibration.provenance} size="xs" />
        </p>
      ) : (
        <p className="confidence__note confidence__note--absent">
          No calibration record exists for this agent, so the reported figure has not been checked against outcomes.
          Read it as a claim, not a probability.
        </p>
      )}
    </div>
  );
}
