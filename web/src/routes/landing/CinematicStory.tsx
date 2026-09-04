import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { QuantityLedger } from '@/components/domain/Decision';
import { Scale } from '@/components/domain/Scale';
import type { Reading } from '@/components/domain/Scale';
import { Badge } from '@/components/ui/Badge';
import { decimalMoney } from '@/lib/decimal';
import { usePrefersReducedMotion } from '@/lib/hooks';
import { PROPOSALS } from '@/data/proposals';

/**
 * The landing page's four-movement product story.
 *
 * Movement 1 is the hero (see `HeroRecord`). Movements 2-4 run here: one
 * record, held on a sticky stage, read against three scrolled chapters —
 * governance, controlled execution, proof.
 *
 * Two rules shape the implementation.
 *
 * First, **every figure is already in the demo dataset**. The stage reads the
 * flagship NVDA proposal straight out of `PROPOSALS`; nothing here is authored
 * for the landing page, so the page cannot drift from the product.
 *
 * Second, **the story is complete before any script runs**. Scroll position
 * only moves emphasis between rows that are all rendered and legible from
 * first paint. There is no entrance that starts at `opacity: 0` and no row
 * whose content depends on the scroll listener ever running — emphasis is the
 * only thing scroll position controls. Under reduced motion the listener is
 * never attached and the stage renders flat, which is also exactly what a
 * reader without scripting sees.
 */

const NVDA = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-nvda-0114')!;

/** The binding check, by reason code rather than by position in the array. */
const BINDING =
  NVDA.hardRisk?.checks.find((c) => c.rule === NVDA.governor?.bindingConstraint) ?? null;

/** The instrument's own side; the landing page never restates it by hand. */
const SIDE = NVDA.instrument.type === 'equity' ? NVDA.instrument.side : NVDA.instrument.strategy;
const SYMBOL = NVDA.instrument.type === 'equity' ? NVDA.instrument.symbol : NVDA.instrument.underlying;

/** Notional strings are decimals, not floats, and are formatted as such. */
const cash = (value: string | null | undefined) => decimalMoney(value) ?? 'Unavailable';

const CHAPTERS = [
  {
    id: 'cine-governance',
    eyebrow: 'Movement two · Governance',
    title: 'Policy measures the request before anyone acts on it.',
    body: 'The deterministic engine recomputes the portfolio as it would stand if the proposal were filled at the requested size, and compares that figure to a fixed ceiling. Forty shares of NVDA would put correlated datacenter exposure at 18.3% against a 12.5% limit. The engine returns the largest size that clears it.',
    note: 'Reduce changes the quantity and nothing else. The price, the structure and the thesis the agent wrote are left exactly as proposed.',
  },
  {
    id: 'cine-execution',
    eyebrow: 'Movement three · Controlled execution',
    title: 'Only the authorized quantity is allowed to reach the broker.',
    body: 'An approval is not a submission. Seven gates re-run at submission time — authorization present, still fresh, kill switch not armed, market open, asset tradable, portfolio re-fetched, no duplicate client order ID — against a portfolio that has moved since the decision was made.',
    note: 'Alpaca Paper only. Live trading is not supported and has no configuration path.',
  },
  {
    id: 'cine-proof',
    eyebrow: 'Movement four · Proof',
    title: 'The decision persists as evidence, not as an explanation.',
    body: 'The authorization is bound to the portfolio and market state it was issued against, expires on a timer, and is marked used the moment it is spent. Every stage wrote a durable event, so the decision can be replayed months later from the record rather than reconstructed from prose.',
    note: null,
  },
];

export function CinematicStory() {
  const reduced = usePrefersReducedMotion();
  /* `null` means "no chapter has claimed the stage yet", which is the state at
     first paint and the permanent state without scripting. It renders every
     row at full strength. */
  const [step, setStep] = useState<number | null>(null);
  const chapterRefs = useRef<(HTMLLIElement | null)[]>([]);

  useEffect(() => {
    if (reduced) return;
    if (typeof window === 'undefined') return;

    /* Read scroll position directly. The chapter whose box contains the middle
       of the viewport owns the stage. Three rect reads per scroll event, no
       writes between them, and React bails out when the step is unchanged — so
       this costs a comparison on most frames. The page scrolls natively and
       nothing here can block, hijack or delay it.

       Deliberately synchronous rather than deferred to an animation frame: a
       document that is not being painted never runs the frame callback, and
       emphasis that depends on a frame is emphasis that can silently never
       arrive. */
    const measure = () => {
      const nodes = chapterRefs.current;
      const middle = window.innerHeight / 2;
      let next: number | null = null;
      for (let i = 0; i < nodes.length; i += 1) {
        const node = nodes[i];
        if (!node) continue;
        const box = node.getBoundingClientRect();
        /* Past the final chapter the finished record stays on the stage. */
        if (box.bottom < middle || (box.top <= middle && box.bottom >= middle)) next = i;
      }
      if (next !== null) setStep(next);
    };

    window.addEventListener('scroll', measure, { passive: true });
    window.addEventListener('resize', measure);
    measure();
    return () => {
      window.removeEventListener('scroll', measure);
      window.removeEventListener('resize', measure);
    };
  }, [reduced]);

  return (
    <section className="cine" aria-labelledby="cine-heading">
      <div className="cine__intro">
        <p className="u-eyebrow">The sequence</p>
        <h2 className="cine__heading" id="cine-heading">
          One proposal, from assertion to evidence.
        </h2>
        <p className="cine__lede">
          A single record from the demonstration dataset, followed all the way through. Every number below is the one
          the engine actually produced.
        </p>
      </div>

      <div className="cine__track">
        <div className="cine__stagecol">
          <StoryRecord step={step} />
        </div>

        <ol className="cine__chapters">
          {CHAPTERS.map((chapter, i) => (
            <li
              key={chapter.id}
              id={chapter.id}
              className="cine__chapter"
              ref={(node) => {
                chapterRefs.current[i] = node;
              }}
            >
              <p className="cine__chaptereyebrow u-eyebrow">{chapter.eyebrow}</p>
              <h3 className="cine__chaptertitle">{chapter.title}</h3>
              <p className="cine__chapterbody">{chapter.body}</p>
              {chapter.note && <p className="cine__chapternote">{chapter.note}</p>}
            </li>
          ))}
        </ol>
      </div>

      <div className="cine__close">
        <p className="cine__closemark wordmark">MIZAN</p>
        <p className="cine__closeline">
          <span>Requested.</span> <span>Authorized.</span> <span>Proven.</span>
        </p>
        <Link className="cine__closecta" to="/app">
          Open Mizan <ArrowRight size={15} aria-hidden="true" />
        </Link>
      </div>
    </section>
  );
}

/* --------------------------------------------------------------- the stage */

/**
 * The record itself.
 *
 * Four rows, always present: what was requested, what policy measured and
 * authorized, what reached the broker, and what was kept. `step` shifts
 * emphasis between them and never removes one — a dimmed row is still read at
 * full size, and below 900px the dimming does not apply at all, because there
 * the rows are a vertical sequence rather than a stage.
 */
function StoryRecord({ step }: { step: number | null }) {
  const outcome = NVDA.outcome;
  const governor = NVDA.governor!;
  const measurement = BINDING?.measurement ?? null;

  const readings: Reading[] = [];
  if (measurement?.actualCurrent) {
    readings.push({ kind: 'current', label: 'Current', value: measurement.actualCurrent });
  }
  if (measurement) {
    readings.push({
      kind: 'requested',
      label: `If requested ${outcome.requested.quantity}`,
      value: measurement.actualIfRequested,
    });
    if (measurement.actualIfAuthorized) {
      readings.push({
        kind: 'authorized',
        label: `If authorized ${outcome.authorized?.quantity ?? governor.approvedQuantity}`,
        value: measurement.actualIfAuthorized,
      });
    }
  }

  return (
    <article className="cine__record" data-step={step === null ? undefined : step}>
      <header className="cine__recordhead">
        <span className="cine__sym">
          {SYMBOL} · {SIDE}
        </span>
        <span className="cine__id u-mono">{NVDA.proposalId}</span>
      </header>

      {/* Movement 1 carries into the stage: the assertion, unchanged. */}
      <Row index={0} step={step} label="Requested" role="Trader Agent · assertion">
        <p className="cine__figure">
          <span className="cine__qty">{outcome.requested.quantity}</span>
          <span className="cine__unit">{outcome.unit}</span>
        </p>
        <p className="cine__notional u-mono">{cash(outcome.requested.notional)}</p>
      </Row>

      <div className="cine__boundary" role="separator" aria-label="Governance boundary">
        <span className="cine__boundarylabel">Intelligence → Authority</span>
      </div>

      {/* Movement 2 — the arithmetic, then the quantity it binds. */}
      <Row index={0} step={step} label="Authorized" role={`${governor.decision} · ${governor.bindingConstraint}`}>
        {measurement && (
          <div className="cine__scale">
            <p className="cine__scalehead">
              <span>{BINDING?.label ?? 'Correlated exposure'}</span>
              <span className="u-mono">limit 12.5%</span>
            </p>
            <Scale
              unit={measurement.unit}
              bound={measurement.bound}
              threshold={measurement.threshold}
              readings={readings}
              size="sm"
            />
          </div>
        )}
        <div className="cine__reduce">
          <QuantityLedger
            proposed={governor.originalQuantity}
            approved={governor.approvedQuantity}
            unit={outcome.unit}
            size="lg"
          />
          <p className="cine__notional u-mono">{cash(outcome.authorized?.notional)}</p>
        </div>
        <p className="cine__code">
          <code className="u-mono">{BINDING?.reasonCode ?? NVDA.reasonCodes[0]}</code>
          <span className="u-dim"> — quantity only. Price, structure and thesis unchanged.</span>
        </p>
      </Row>

      {/* Movement 3 — the seam the broker sits behind. */}
      <Row index={1} step={step} label="Executed" role="Execution gate · 7 checks re-run">
        <p className="cine__venue">
          <Badge tone="paper" shape="diamond">
            Paper only
          </Badge>
          <span className="cine__venuename">Alpaca Paper</span>
        </p>
        <p className="cine__figure">
          <span className="cine__qty">{outcome.executed?.quantity ?? 0}</span>
          <span className="cine__unit">{outcome.unit} filled</span>
        </p>
        <p className="cine__notional u-mono">{cash(outcome.executed?.notional)}</p>
        <p className="cine__prevented">
          <span className="u-label">Prevented</span>
          {cash(outcome.preventedNotional)}  of requested exposure never reached the broker.
        </p>
      </Row>

      {/* Movement 4 — what is left afterwards. */}
      <Row index={2} step={step} label="Proven" role="Persisted evidence">
        <dl className="cine__proof">
          <div>
            <dt>Authorization</dt>
            <dd>
              <span className="cine__proofmark" aria-hidden="true">●</span> {NVDA.authorization?.status ?? 'NOT ISSUED'} ·{' '}
              <span className="u-mono">{NVDA.authorization?.id}</span>
            </dd>
          </div>
          <div>
            <dt>Record</dt>
            <dd>
              {NVDA.chain?.verified ? 'Stored record hash verified' : 'Verification unavailable'}
            </dd>
          </div>
          <div>
            <dt>Hash</dt>
            <dd className="u-mono cine__hash">{NVDA.chain?.recordHash ?? 'Unavailable'}</dd>
          </div>
        </dl>
      </Row>
    </article>
  );
}

/**
 * One row of the record.
 *
 * `index` is the chapter that owns it. Rows are rendered identically whatever
 * the step; the attribute is read by the stylesheet, which only ever changes
 * emphasis, and only above 900px.
 */
function Row({
  index,
  step,
  label,
  role,
  children,
}: {
  index: number;
  step: number | null;
  label: string;
  role: string;
  children: React.ReactNode;
}) {
  const state = step === null ? 'flat' : step === index ? 'active' : step > index ? 'past' : 'ahead';
  return (
    <section className="cine__row" data-state={state}>
      <header className="cine__rowhead">
        <span className="cine__rowlabel">{label}</span>
        <span className="cine__rowrole u-mono">{role}</span>
      </header>
      {children}
    </section>
  );
}

/* ---------------------------------------------------------------- movement 1 */

/**
 * The hero's first visual object: the assertion, before anything has judged it.
 *
 * A real record from the dataset rather than a chart — the number is the
 * subject, and it is the same 40 the stage below carries through to 20.
 */
export function HeroRecord() {
  const outcome = NVDA.outcome;
  return (
    <article className="herorec">
      <header className="herorec__head">
        <span className="herorec__sym">{SYMBOL}</span>
        <span className="herorec__side u-mono">{SIDE}</span>
        <span className="herorec__stage u-mono">TradeProposal</span>
      </header>
      <p className="herorec__label u-label">Requested</p>
      <p className="herorec__figure">
        <span className="herorec__qty">{outcome.requested.quantity}</span>
        <span className="herorec__unit">{outcome.unit}</span>
      </p>
      <p className="herorec__notional u-mono">{cash(outcome.requested.notional)}</p>
      <p className="herorec__foot">
        An assertion by an agent. Nothing has authorized it yet.
      </p>
    </article>
  );
}
