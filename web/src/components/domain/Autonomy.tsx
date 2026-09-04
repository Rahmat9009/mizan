import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { cx } from '@/lib/format';
import { useDismissable } from '@/lib/hooks';
import { AUTONOMY_LABEL, useApp } from '@/state/app';
import type { AutonomyMode } from '@/types/domain';

/**
 * Changing who is in the loop is an authorization, so it is made like one.
 *
 * This control previously committed on a single keystroke: a bare `<select>` in
 * the top chrome, styled like a timezone picker, wired straight to the setter.
 * One arrow key moved the desk from *Manual approval* — a person confirms every
 * execution — to *Autonomous paper*, in which the machine submits on its own.
 * Meanwhile the Full stop button, the control that makes the system *safer*,
 * asked for confirmation.
 *
 * The product's whole claim is that authority is deliberate and recorded. So
 * the posture is a readout first, and any change to it states its consequence
 * and takes an explicit second act. The safe direction is not privileged over
 * the dangerous one; both go through the same gate.
 */

const ORDER: AutonomyMode[] = ['OBSERVE', 'MANUAL', 'AUTONOMOUS_PAPER'];

/** What actually changes. Written from the system's behaviour, not softened. */
const CONSEQUENCE: Record<AutonomyMode, string> = {
  OBSERVE:
    'Observe analyses and records, and never executes. Proposals still run every check and are still written to the audit trail, but nothing reaches the broker.',
  MANUAL:
    'Manual approval lets the Governor decide the size. A person confirms each execution before it is submitted.',
  AUTONOMOUS_PAPER:
    'Autonomous paper lets Governor-approved trades reach Alpaca Paper without a person confirming them. Every deterministic gate and the execution safety gate still apply, and the environment is still paper only.',
};

export function AutonomyControl({ variant = 'chip' }: { variant?: 'chip' | 'panel' }) {
  const { autonomy, setAutonomy } = useApp();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<AutonomyMode | null>(null);
  const ref = useDismissable(open, () => {
    setOpen(false);
    setPending(null);
  });

  function choose(mode: AutonomyMode) {
    if (mode === autonomy) {
      setOpen(false);
      return;
    }
    setPending(mode);
  }

  function commit() {
    if (pending) setAutonomy(pending);
    setPending(null);
    setOpen(false);
  }

  if (variant === 'panel') {
    return (
      <div className="autonomyctl autonomyctl--panel">
        <div className="autonomyctl__options" role="group" aria-label="Autonomy mode">
          {ORDER.map((mode) => (
            <button
              key={mode}
              type="button"
              className={cx('autonomyctl__option', mode === autonomy && 'is-current')}
              aria-pressed={mode === autonomy}
              onClick={() => setPending(mode === autonomy ? null : mode)}
            >
              {AUTONOMY_LABEL[mode]}
            </button>
          ))}
        </div>
        <p className="autonomyctl__now">{CONSEQUENCE[autonomy]}</p>
        {pending && <Confirm mode={pending} onCommit={commit} onCancel={() => setPending(null)} />}
      </div>
    );
  }

  return (
    <div className="autonomyctl" ref={ref}>
      <button
        type="button"
        className={cx('autonomyctl__trigger', open && 'is-open')}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="autonomyctl__key">Autonomy</span>
        <span className="autonomyctl__value">{AUTONOMY_LABEL[autonomy]}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>

      {open && (
        <div className="autonomyctl__pop" role="dialog" aria-label="Change autonomy mode">
          <p className="autonomyctl__pophead u-label">Autonomy mode</p>
          <ul className="autonomyctl__list">
            {ORDER.map((mode) => (
              <li key={mode}>
                <button
                  type="button"
                  className={cx('autonomyctl__item', mode === autonomy && 'is-current')}
                  aria-current={mode === autonomy ? 'true' : undefined}
                  onClick={() => choose(mode)}
                >
                  <span className="autonomyctl__itemname">{AUTONOMY_LABEL[mode]}</span>
                  {mode === autonomy && <span className="autonomyctl__itemnow">Current</span>}
                </button>
              </li>
            ))}
          </ul>
          {pending ? (
            <Confirm mode={pending} onCommit={commit} onCancel={() => setPending(null)} />
          ) : (
            <p className="autonomyctl__note">{CONSEQUENCE[autonomy]}</p>
          )}
        </div>
      )}
    </div>
  );
}

function Confirm({
  mode,
  onCommit,
  onCancel,
}: {
  mode: AutonomyMode;
  onCommit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="autonomyctl__confirm" role="alertdialog" aria-label={`Confirm ${AUTONOMY_LABEL[mode]}`}>
      <p>{CONSEQUENCE[mode]}</p>
      <div className="autonomyctl__confirmrow">
        <Button
          variant={mode === 'AUTONOMOUS_PAPER' ? 'danger' : 'primary'}
          size="sm"
          onClick={onCommit}
        >
          Set {AUTONOMY_LABEL[mode]}
        </Button>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
