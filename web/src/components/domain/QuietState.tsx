import { Link } from 'react-router-dom';
import { Link2 } from 'lucide-react';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { decimalMoney, decimalMoneyCompact, decimalToRatio } from '@/lib/decimal';
import { cx, integer, percent } from '@/lib/format';
import type { GovernanceDay } from '@/types/domain';
import { VERDICT_GLYPH } from './Decision';

/** One outstanding item the same page is about to render. */
export interface OpenItem {
  id: string;
  label: string;
}

/**
 * The quiet state.
 *
 * Ninety-nine percent of the time nothing is wrong. A governance product whose
 * home screen is designed around incidents looks dead on a normal day, and a
 * dashboard nobody opens governs nothing.
 *
 * So the quiet day is itself the proof of value, and the figure that carries it
 * is the exposure that never reached the broker. "Nothing happened" and "here
 * is what we stopped" are the same day described twice; only the second one
 * renews a contract.
 *
 * Includes the prevention visualization bar representing requested vs authorized.
 */
export function QuietState({ day, open = [] }: { day: GovernanceDay; open?: OpenItem[] }) {
  /* Derived, never asserted. The screen previously read "Nothing needs your
     attention" from a stored figure while a red BLOCK and a breach at 133% of
     limit sat four hundred pixels below it. A governance product cannot claim
     calm the same screen disproves — so the claim is computed from the same
     alerts the page is about to render, and it can no longer disagree with
     them. */
  const outstanding = open.length > 0 ? open.length : day.needsAttention;
  const calm = outstanding === 0;

  const hasNotional =
    day.requestedNotional !== null &&
    day.authorizedNotional !== null &&
    day.preventedNotional !== null;
  const requested = decimalToRatio(day.requestedNotional) ?? 0;
  const authorized = decimalToRatio(day.authorizedNotional) ?? 0;
  const prevented = decimalToRatio(day.preventedNotional) ?? 0;

  const authorizedShare = requested > 0 ? Math.min(authorized / requested, 1) : 0;
  const preventedShare = requested > 0 ? Math.min(prevented / requested, 1) : 0;

  return (
    <section className={cx('quiet', calm && 'is-calm')} aria-label="Today's governance summary">
      <div className="quiet__lead">
        <p className="quiet__eyebrow">
          Today <span className="u-mono">{day.date}</span> <ProvenanceBadge value={day.provenance} size="xs" />
        </p>
        <p className="quiet__headline">
          <strong className="u-mono">{integer(day.decisionsGoverned)}</strong> decisions governed
        </p>
        <ul className="quiet__breakdown">
          <li>
            <span className="quiet__glyph is-ok" aria-hidden="true">
              {VERDICT_GLYPH.APPROVE}
            </span>
            <span className="u-mono">{integer(day.approved)}</span> approved
          </li>
          <li>
            <span className="quiet__glyph is-warn" aria-hidden="true">
              {VERDICT_GLYPH.REDUCE}
            </span>
            <span className="u-mono">{integer(day.reduced)}</span> reduced
          </li>
          <li>
            <span className="quiet__glyph is-danger" aria-hidden="true">
              {VERDICT_GLYPH.REJECT}
            </span>
            <span className="u-mono">{integer(day.rejected)}</span> rejected
          </li>
        </ul>
      </div>

      <div className="quiet__flow">
        <div className="quiet__flowheads">
          <Flow label="Requested" value={decimalMoneyCompact(day.requestedNotional)} exact={day.requestedNotional} />
          <Flow label="Authorized" value={decimalMoneyCompact(day.authorizedNotional)} exact={day.authorizedNotional} />
          <Flow
            label="Prevented"
            value={decimalMoneyCompact(day.preventedNotional)}
            exact={day.preventedNotional}
            emphasis
          />
        </div>

        {hasNotional && (
          <div
            className="quiet__bar"
            role="img"
            aria-label={`Of the requested notional, ${percent(authorizedShare, 0)} was authorized and ${percent(
              preventedShare,
              0,
            )} did not receive authorization.`}
          >
            <span className="quiet__barfill" style={{ width: `${authorizedShare * 100}%` }} />
            {authorizedShare > 0 && authorizedShare < 1 && (
              <span className="quiet__barstop" style={{ left: `${authorizedShare * 100}%` }} aria-hidden="true" />
            )}
          </div>
        )}

        <p className="quiet__ratio">
          <strong>{hasNotional ? percent(preventedShare, 0) : 'Unavailable'}</strong>{' '}
          <span className="u-dim">({decimalMoneyCompact(day.preventedNotional)})</span> of requested exposure did not receive authorization
        </p>
      </div>

      <div className="quiet__foot">
        <p className={cx('quiet__verdict', calm && 'is-calm')}>
          {calm ? (
            <>
              {/* Proof, not reassurance. A quiet day is evidence that the
                  machinery ran, so the machinery is what gets stated. */}
              Nothing is waiting on you.{' '}
              <span className="quiet__proof">
                Every one of the <span className="u-mono">{integer(day.decisionsGoverned)}</span> was measured against
                policy before it could reach a broker.
              </span>
            </>
          ) : (
            <>
              <span className="quiet__glyph is-danger" aria-hidden="true">
                ◆
              </span>
              <span className="u-mono">{integer(outstanding)}</span>{' '}
              {outstanding === 1 ? 'item needs' : 'items need'} your attention
              {open.length > 0 && <span className="quiet__proof">{open.map((o) => o.label).join(' · ')}</span>}
            </>
          )}
        </p>
        <p className="quiet__chain">
          <Link2 size={12} aria-hidden="true" />
          {/* The same claim the case file and the replay make, in the same
              words: a stored record hash matched. The frontend performs no
              cryptographic verification of its own and must not imply one. */}
          Stored record hashes verified <span className="u-mono">{integer(day.chainVerified)}</span>
          <span className="u-dim"> / {integer(day.chainTotal)}</span>
          <Link className="quiet__chainlink" to="/app/audit">
            Audit
          </Link>
        </p>
      </div>
    </section>
  );
}

function Flow({
  label,
  value,
  exact,
  emphasis,
}: {
  label: string;
  value: string | null;
  exact: string | null;
  emphasis?: boolean;
}) {
  return (
    <div className={cx('quiet__flowcell', emphasis && 'is-emphasis')}>
      <p className="quiet__flowlabel">{label}</p>
      {/* The compact figure is a reading aid; the exact decimal is on hover and
          in the record, never lost to a rounding. */}
      <p className="quiet__flowvalue u-mono" title={decimalMoney(exact) ?? undefined}>
        {value}
      </p>
    </div>
  );
}
