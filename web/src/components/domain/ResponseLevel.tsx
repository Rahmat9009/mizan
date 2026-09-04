import { useState } from 'react';
import { Power } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { RESPONSE_LEVELS } from '@/data/governance';
import { cx, stampOf } from '@/lib/format';
import { useApp } from '@/state/app';
import type { ResponseLevel } from '@/types/domain';

/**
 * The graduated response ladder.
 *
 * Two things the interface previously could not say: which of six postures the
 * system is in, and — when it is stopped — who stopped it and when. A binary
 * switch renders an action; a desk needs to read a state.
 */

const LEVELS: ResponseLevel[] = [0, 1, 2, 3, 4, 5];

/**
 * A page-level banner for the two levels that stop work.
 *
 * The chip states the posture; this states the consequence, above the content,
 * so nobody reads a screen full of proposals without knowing that none of them
 * can currently reach a broker.
 */
export function ResponseBanner() {
  const { responseLevel, responseSince, responseEngagedBy } = useApp();
  if (responseLevel < 4) return null;

  const meta = RESPONSE_LEVELS[responseLevel];
  return (
    <div className={cx('rlbanner', responseLevel === 5 && 'is-stop')} role="status">
      <span className="rlbanner__glyph" aria-hidden="true">
        {meta.glyph}
      </span>
      <strong className="rlbanner__title">
        Level {responseLevel} · {meta.name}
      </strong>
      <span className="rlbanner__effect">{meta.effect}</span>
      <span className="rlbanner__who u-mono">
        engaged {stampOf(responseSince)} UTC{responseEngagedBy ? ` by ${responseEngagedBy}` : ''}
      </span>
    </div>
  );
}

/** The compact indicator that rides in the top chrome on every screen. */
export function ResponseLevelChip() {
  const { responseLevel } = useApp();
  const meta = RESPONSE_LEVELS[responseLevel];

  return (
    <span
      className={cx('rlchip', `rlchip--${meta.tone}`)}
      title={`Response level ${responseLevel} — ${meta.name}. ${meta.effect}`}
    >
      <span className="rlchip__glyph" aria-hidden="true">
        {meta.glyph}
      </span>
      <span className="rlchip__level">L{responseLevel}</span>
      <span className="rlchip__name">{meta.name}</span>
    </span>
  );
}

/**
 * The rail control: the current level, an escalation step, and the full stop.
 *
 * The full stop is guarded by a confirm because it is not undoable in the sense
 * that matters — every outstanding authorization is void once it fires, and
 * they have to be re-earned rather than resumed.
 */
export function ResponseLadder() {
  const { responseLevel, setResponseLevel, responseSince, responseEngagedBy } = useApp();
  const [confirming, setConfirming] = useState(false);
  const meta = RESPONSE_LEVELS[responseLevel];
  const stopped = responseLevel === 5;

  return (
    <div className={cx('ladder', stopped && 'is-stopped')}>
      <div className="ladder__head">
        <span className="rail__key">Response level</span>
        <span className={cx('ladder__now', `is-${meta.tone}`)}>
          <span aria-hidden="true">{meta.glyph}</span> L{responseLevel} · {meta.name}
        </span>
      </div>

      <ol className="ladder__steps" aria-label="Graduated response ladder">
        {LEVELS.map((level) => (
          <li key={level}>
            <button
              type="button"
              className={cx(
                'ladder__step',
                `is-${RESPONSE_LEVELS[level].tone}`,
                level <= responseLevel && 'is-on',
                level === responseLevel && 'is-current',
              )}
              aria-pressed={level === responseLevel}
              aria-label={`Level ${level}, ${RESPONSE_LEVELS[level].name}. ${RESPONSE_LEVELS[level].effect}`}
              title={`L${level} · ${RESPONSE_LEVELS[level].name} — ${RESPONSE_LEVELS[level].effect}`}
              onClick={() => (level === 5 ? setConfirming(true) : setResponseLevel(level))}
            >
              <span className="u-sr-only">Level {level}</span>
            </button>
          </li>
        ))}
      </ol>

      <p className="ladder__effect">{meta.effect}</p>

      {stopped ? (
        <div className="ladder__state" role="status">
          <p className="ladder__statetitle">
            <span aria-hidden="true">✕</span> FULL STOP ACTIVE
          </p>
          <p className="ladder__statebody">
            Engaged {stampOf(responseSince)} UTC{responseEngagedBy ? ` by ${responseEngagedBy}` : ''}. All agents
            halted, all outstanding authorizations void.
          </p>
          <Button variant="secondary" size="sm" onClick={() => setResponseLevel(0)}>
            Stand down to L0
          </Button>
        </div>
      ) : confirming ? (
        <div className="ladder__confirm" role="alertdialog" aria-label="Confirm full stop">
          <p>
            Full stop refuses every submission and voids every outstanding authorization. Agents will need fresh
            authorization to trade again.
          </p>
          <div className="ladder__confirmrow">
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                setResponseLevel(5);
                setConfirming(false);
              }}
            >
              Engage full stop
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <button type="button" className="fullstop" onClick={() => setConfirming(true)}>
          <Power size={14} aria-hidden="true" />
          <span className="fullstop__label">Full stop</span>
          <span className="fullstop__state">L5</span>
        </button>
      )}
    </div>
  );
}
