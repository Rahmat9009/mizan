import type { ReactNode } from 'react';
import { cx } from '@/lib/format';

/**
 * A page division that is not the governance boundary.
 *
 * The datum is the product's scarcest device: a rule that changes material
 * where authority begins, carrying the words GOVERNANCE BOUNDARY. It only means
 * something because it is rare. A page that declares it twice has spent it.
 *
 * Some pages still need to name a division — Audit separates the decision
 * *reconstructed* from the durable *record* — but that is a division of the
 * document, not the point where intelligence ends and policy begins. This is
 * the device for that: the same two-register shape, drawn as an ordinary
 * hairline in ordinary ink.
 *
 * It is never brass, and it never carries the boundary's label.
 */
export function SectionRule({
  registers,
  caption,
  className,
}: {
  /** What the division separates, e.g. ['Reconstruction', 'Record']. */
  registers: [string, string];
  /** One line stating what changes across the rule. */
  caption?: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx('sectionrule', className)} aria-label={`${registers[0]} / ${registers[1]}`}>
      <div className="sectionrule__grid">
        <p className="sectionrule__register">{registers[0]}</p>
        <p className="sectionrule__register sectionrule__register--second">{registers[1]}</p>
      </div>
      {caption && <p className="sectionrule__caption">{caption}</p>}
    </section>
  );
}
