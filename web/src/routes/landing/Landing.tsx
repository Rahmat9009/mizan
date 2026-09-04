import { useEffect } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Check, Minus, MoonStar, Sun, X } from 'lucide-react';
import { AgentPipeline } from '@/components/domain/AgentPipeline';
import { QuantityLedger } from '@/components/domain/Decision';
import { Badge } from '@/components/ui/Badge';
import { ButtonLink } from '@/components/ui/Button';
import { Mark } from '@/components/shell/Mark';
import { STAGE_META, stage } from '@/data/pipeline';
import { PROPOSALS } from '@/data/proposals';
import { money } from '@/lib/format';
import { AUTONOMY_BLURB, AUTONOMY_LABEL, useApp } from '@/state/app';
import { CinematicStory, HeroRecord } from './CinematicStory';
import { ProductPreview } from './ProductPreview';
import { Reveal } from './Reveal';

const HERO_STAGES = [
  stage('research', 'COMPLETE'),
  stage('selection', 'COMPLETE'),
  stage('probability', 'COMPLETE'),
  stage('trader', 'COMPLETE', { quantityOut: 40 }),
  stage('hard_risk', 'REDUCED', { quantityOut: 24 }),
  stage('ai_risk', 'WATCH', { quantityOut: 20 }),
  stage('governor', 'REDUCED', { quantityOut: 20 }),
  stage('execution', 'COMPLETE', { quantityOut: 20 }),
];

const INTELLIGENCE = ['research', 'selection', 'probability', 'trader'] as const;
const GOVERNANCE = ['hard_risk', 'ai_risk', 'governor'] as const;

const GATES = [
  { label: 'Authorization', detail: 'A Governor decision exists with an approved size above zero.' },
  { label: 'Freshness', detail: 'The decision is younger than the configured maximum age.' },
  { label: 'Kill switch', detail: 'The operator has not armed the refusal switch.' },
  { label: 'Market hours', detail: 'The session is open for this asset class.' },
  { label: 'Asset', detail: 'The instrument is active and tradable on the paper account.' },
  { label: 'Fresh portfolio', detail: 'The portfolio is re-fetched and still supports the size.' },
  { label: 'Idempotency', detail: 'No prior order exists for this client order ID.' },
];

export function Landing() {
  const { theme, toggleTheme } = useApp();
  const nvda = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-nvda-0114')!;
  const tsla = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-tsla-0121')!;
  const aapl = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-aapl-0092')!;
  const msft = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-msft-0118')!;

  useEffect(() => {
    document.documentElement.classList.add('is-landing');
    return () => document.documentElement.classList.remove('is-landing');
  }, []);

  return (
    <div className="landing">
      <header className="lnav">
        <Link className="lnav__brand" to="/">
          <Mark size={24} />
          <span className="wordmark">MIZAN</span>
        </Link>
        <nav className="lnav__links" aria-label="Sections">
          <a href="#intelligence">Intelligence</a>
          <a href="#governance">Governance</a>
          <a href="#execution">Execution</a>
          <a href="#audit">Audit</a>
        </nav>
        <div className="lnav__actions">
          <button className="iconbtn" onClick={toggleTheme} aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}>
            {theme === 'dark' ? <Sun size={16} aria-hidden="true" /> : <MoonStar size={16} aria-hidden="true" />}
          </button>
          <ButtonLink to="/app" variant="primary" size="sm" iconRight={<ArrowRight size={14} aria-hidden="true" />}>
            Launch Command Center
          </ButtonLink>
        </div>
      </header>

      {/* 1 — Hero ---------------------------------------------------------- */}
      <section className="hero">
        {/* Nothing above the fold is wrapped in an entrance. The hero is the
            one place a failed or slow observer would cost the reader the whole
            proposition, so it is painted, not revealed. */}
        <div className="hero__text">
          <p className="hero__eyebrow">
            <Badge tone="paper" shape="diamond">
              Paper only
            </Badge>
            <span>Alpaca Paper · live trading not supported</span>
          </p>
          <h1 className="hero__title">
            AI can propose.{' '}
            <br />
            Mizan decides what is authorized.
          </h1>
          <p className="hero__lede">
            Market-intelligence agents research, select, score and propose trades. Mizan decides what size — if any —
            is authorized to reach the broker: a deterministic risk engine, a contextual AI review, and the Portfolio
            Governor. Every step is recorded.
          </p>
          <div className="hero__cta">
            <ButtonLink to="/app" variant="primary" size="lg" iconRight={<ArrowRight size={16} aria-hidden="true" />}>
              Open Mizan
            </ButtonLink>
            <a className="hero__link" href="#cine-governance">
              See how it works
            </a>
          </div>
        </div>

        {/* Movement one. The first object on the page is a real record, not a
            chart: an agent's assertion, with the number it asked for as the
            subject. The rail beneath names the eight stages it must travel. */}
        <div className="hero__visual">
          <HeroRecord />
        </div>

        {/* The eight stages the record must travel. It spans the hero rather
            than sharing the record's column, because a rail squeezed into half
            a column abbreviates its own stage names. */}
        <div className="hero__pipeline">
          <AgentPipeline stages={HERO_STAGES} variant="rail" />
        </div>
      </section>

      {/* 1b — The sequence (movements two to four) ------------------------- */}
      <CinematicStory />

      {/* 2 — Problem ------------------------------------------------------- */}
      <section className="lsection lsection--problem">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">The problem</p>
          <h2 className="lsection__title">
            Agents keep getting better at deciding what to trade. That is not the same as being allowed to trade it.
          </h2>
          <div className="problem">
            <div className="problem__col">
              <h3>What agents are good at</h3>
              <p>
                Reading filings faster than a desk can. Ranking a universe on a factor. Attaching a calibrated
                confidence to a thesis and writing the invalidation condition down.
              </p>
            </div>
            <div className="problem__col problem__col--risk">
              <h3>What direct broker access costs</h3>
              <p>
                A model with an API key can size a position at forty percent of the account, add a fourth correlated
                bet on one catalyst, or resubmit after a rejection. None of that is a reasoning failure. It is a missing
                control layer.
              </p>
            </div>
          </div>
        </Reveal>
      </section>

      {/* 3 — Market intelligence ------------------------------------------- */}
      <section className="lsection" id="intelligence">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Market intelligence</p>
          <h2 className="lsection__title">Four specialised agents, one structured output.</h2>
          <p className="lsection__lede">
            Each stage narrows the problem, and each one leaves a record of what it did. The last of them produces a
            TradeProposal: a typed object with a size, a price, a confidence, a thesis and the condition that would
            prove it wrong.
          </p>
        </Reveal>
        <ol className="stagecards">
          {INTELLIGENCE.map((id, i) => (
            <Reveal as="li" key={id} delay={i * 0.06}>
              <article className="stagecard">
                <span className="stagecard__code u-mono">{STAGE_META[id].short}</span>
                <h3>{STAGE_META[id].actor}</h3>
                <p>{STAGE_META[id].blurb}</p>
              </article>
            </Reveal>
          ))}
        </ol>
      </section>

      {/* 4 — Handoff -------------------------------------------------------- */}
      <section className="lsection lsection--handoff">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">The handoff</p>
          <h2 className="lsection__title">One object crosses the boundary.</h2>
          <div className="handoff">
            <div className="handoff__object">
              <p className="u-label">TradeProposal</p>
              <dl>
                <div>
                  <dt>symbol</dt>
                  <dd className="u-mono">NVDA</dd>
                </div>
                <div>
                  <dt>side</dt>
                  <dd className="u-mono">BUY</dd>
                </div>
                <div>
                  <dt>quantity</dt>
                  <dd className="u-mono">40</dd>
                </div>
                <div>
                  <dt>estimated_price</dt>
                  <dd className="u-mono">182.40</dd>
                </div>
                <div>
                  <dt>strategy_confidence</dt>
                  <dd className="u-mono">0.78</dd>
                </div>
                <div>
                  <dt>thesis</dt>
                  <dd>Datacenter guidance raised for a third quarter…</dd>
                </div>
                <div>
                  <dt>invalidation_condition</dt>
                  <dd>Close below 168.00 on above-average volume…</dd>
                </div>
              </dl>
            </div>
            <div className="boundary-rule boundary-rule--vertical" role="separator" aria-label="Governance boundary">
              <span className="boundary-rule__label">Governance boundary</span>
            </div>
            <div className="handoff__note">
              <h3>What upstream agents cannot do</h3>
              <ul className="ticklist ticklist--no">
                <li>
                  <X size={13} aria-hidden="true" /> Call a broker mutation
                </li>
                <li>
                  <X size={13} aria-hidden="true" /> Override deterministic policy
                </li>
                <li>
                  <X size={13} aria-hidden="true" /> Supply their own risk report or Governor decision
                </li>
                <li>
                  <X size={13} aria-hidden="true" /> Resubmit a replacement after a rejection
                </li>
              </ul>
              <p>
                The proposal is an assertion. Everything after this line treats it as one.
              </p>
            </div>
          </div>
        </Reveal>
      </section>

      {/* 5 — Governance ----------------------------------------------------- */}
      <section className="lsection" id="governance">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Portfolio governance</p>
          <h2 className="lsection__title">Deterministic first. Contextual second. Neither can raise a ceiling.</h2>
        </Reveal>
        <ol className="stagecards stagecards--3">
          {GOVERNANCE.map((id, i) => (
            <Reveal as="li" key={id} delay={i * 0.06}>
              <article className="stagecard stagecard--gov">
                <span className="stagecard__code u-mono">{STAGE_META[id].short}</span>
                <h3>{STAGE_META[id].actor}</h3>
                <p>{STAGE_META[id].blurb}</p>
              </article>
            </Reveal>
          ))}
        </ol>
        <Reveal>
          <p className="lsection__pull">
            The AI risk review is advisory in one direction only. It can argue a position down and it can raise a
            concern nobody encoded, but it has no mechanism to lift a policy limit — so a model that is wrong, jailbroken
            or simply confident cannot make the portfolio less safe than policy allows.
          </p>
        </Reveal>
      </section>

      {/* 6 — Decision demo --------------------------------------------------- */}
      <section className="lsection lsection--demo">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Three outcomes</p>
          <h2 className="lsection__title">Approve, reduce, reject — and the reason for each.</h2>
        </Reveal>
        <div className="demo">
          {[aapl, nvda, tsla].map((p, i) => {
            const tone = p.governor!.decision === 'APPROVE' ? 'ok' : p.governor!.decision === 'REDUCE' ? 'warn' : 'danger';
            return (
              <Reveal key={p.proposalId} delay={i * 0.07}>
                <article className={`democard is-${tone}`}>
                  <header className="democard__head">
                    <span className="democard__sym u-mono">
                      {p.instrument.type === 'equity' ? p.instrument.symbol : p.instrument.underlying}
                    </span>
                    <Badge tone={tone}>{p.governor!.decision}</Badge>
                  </header>
                  <QuantityLedger
                    proposed={p.governor!.originalQuantity}
                    approved={p.governor!.approvedQuantity}
                    unit="shares"
                    size="lg"
                  />
                  <p className="democard__reason">{p.governor!.reason}</p>
                  <p className="democard__binding">
                    <span className="u-label">Binding constraint</span>
                    {p.governor!.bindingConstraint ?? 'None — approved as proposed'}
                  </p>
                  <Link className="democard__link" to={`/app/proposals/${p.proposalId}`}>
                    Open the case file <ArrowRight size={12} aria-hidden="true" />
                  </Link>
                </article>
              </Reveal>
            );
          })}
        </div>
      </section>

      {/* 7 — Controlled execution -------------------------------------------- */}
      <section className="lsection" id="execution">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Controlled execution</p>
          <h2 className="lsection__title">An approval is not a submission.</h2>
          <p className="lsection__lede">
            Between the Governor and the broker sit seven checks that all run again at submission time, because a
            decision made ninety seconds ago was made against a portfolio that has since moved.
          </p>
        </Reveal>
        <ol className="gatelist">
          {GATES.map((gate, i) => (
            <Reveal as="li" key={gate.label} delay={i * 0.04}>
              <span className="gatelist__num u-mono">{String(i + 1).padStart(2, '0')}</span>
              <div>
                <h3>{gate.label}</h3>
                <p>{gate.detail}</p>
              </div>
            </Reveal>
          ))}
        </ol>
        <Reveal>
          <p className="lsection__pull">
            Paper only, with no configuration path to live trading. The kill switch refuses every submission the moment
            it is armed, whatever the Governor decided a second earlier.
          </p>
        </Reveal>
      </section>

      {/* 8 — Command Center --------------------------------------------------- */}
      <section className="lsection lsection--preview">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Command Center</p>
          <h2 className="lsection__title">One screen for account, agents, risk and execution.</h2>
        </Reveal>
        <Reveal delay={0.08}>
          <div className="previewframe">
            <ProductPreview />
          </div>
        </Reveal>
      </section>

      {/* 9 — Options ---------------------------------------------------------- */}
      <section className="lsection lsection--options">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Options</p>
          <h2 className="lsection__title">Defined risk, measured as maximum loss.</h2>
          <div className="options">
            <div className="options__body">
              <p>
                For a spread, the honest risk number is the most it can lose — not its stock-equivalent notional and
                not its mark. The engine recomputes maximum loss from the legs rather than trusting the figure the
                proposal declared, and refuses any structure with an uncovered short leg.
              </p>
              <ul className="ticklist">
                <li>
                  <Check size={13} aria-hidden="true" /> Long call, long put, verticals, iron condor
                </li>
                <li>
                  <Check size={13} aria-hidden="true" /> Maximum defined loss capped as a share of equity and buying power
                </li>
                <li>
                  <Check size={13} aria-hidden="true" /> Contract ceiling and a minimum days-to-expiry floor
                </li>
                <li>
                  <X size={13} aria-hidden="true" /> No naked short structures, at any size
                </li>
                <li>
                  <Minus size={13} aria-hidden="true" /> No implied volatility, Greeks or probability of profit — nothing supplies them
                </li>
              </ul>
            </div>
            <aside className="options__card">
              <p className="u-label">Worked example</p>
              <h3 className="u-mono">MSFT 520/530 C · 16 Oct 26</h3>
              <dl>
                <div>
                  <dt>Requested</dt>
                  <dd>8 contracts</dd>
                </div>
                <div>
                  <dt>Maximum defined loss</dt>
                  <dd>{money(msft.instrument.type === 'option' ? msft.instrument.maxDefinedLoss : 0)}</dd>
                </div>
                <div>
                  <dt>Policy</dt>
                  <dd>5.0% of equity</dd>
                </div>
                <div>
                  <dt>Authorized</dt>
                  <dd>
                    <QuantityLedger proposed={8} approved={5} unit="contracts" size="sm" />
                  </dd>
                </div>
              </dl>
              <p className="options__note">
                Only the contract count changed. The structure the trader agent designed was left intact.
              </p>
            </aside>
          </div>
        </Reveal>
      </section>

      {/* 10 — Audit ------------------------------------------------------------ */}
      <section className="lsection" id="audit">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Auditability</p>
          <h2 className="lsection__title">Reconstructable months later, not just explainable today.</h2>
          <p className="lsection__lede">
            Every stage writes a durable, sanitised event: what it was given, what rule it applied, what it returned and
            what the quantity became. Decision Replay steps a stored proposal from the first research event to the
            broker outcome, one step at a time, with the payload visible at each one.
          </p>
        </Reveal>
        <Reveal delay={0.08}>
          <ol className="auditstrip">
            {['Research', 'Selection', 'Probability', 'Trader', 'Risk', 'AI Risk', 'Governor', 'Execution', 'Broker'].map(
              (label, i) => (
                <li key={label}>
                  <span className="auditstrip__num u-mono">{i + 1}</span>
                  <span>{label}</span>
                </li>
              ),
            )}
          </ol>
        </Reveal>
      </section>

      {/* 11 — Autonomy --------------------------------------------------------- */}
      <section className="lsection lsection--autonomy">
        <Reveal>
          <p className="lsection__eyebrow u-eyebrow">Autonomy</p>
          <h2 className="lsection__title">Three modes. The boundary is the same in all of them.</h2>
        </Reveal>
        <div className="modes">
          {(['OBSERVE', 'MANUAL', 'AUTONOMOUS_PAPER'] as const).map((mode, i) => (
            <Reveal key={mode} delay={i * 0.07}>
              <article className="modecard">
                <h3>{AUTONOMY_LABEL[mode]}</h3>
                <p>{AUTONOMY_BLURB[mode]}</p>
              </article>
            </Reveal>
          ))}
        </div>
      </section>

      {/* 12 — CTA -------------------------------------------------------------- */}
      <section className="lsection lsection--cta">
        <Reveal>
          <h2 className="cta__title">AI agents can generate ideas. They cannot bypass governance.</h2>
          <ButtonLink to="/app" variant="primary" size="lg" iconRight={<ArrowRight size={16} aria-hidden="true" />}>
            Launch Command Center
          </ButtonLink>
          <p className="cta__note">
            Runs on Alpaca Paper. Live trading is not supported and cannot be enabled.
          </p>
        </Reveal>
      </section>

      <footer className="lfooter">
        <div className="lfooter__brand">
          <Mark size={20} />
          <span className="wordmark">MIZAN</span>
        </div>
        <p>
          Portfolio intelligence and governance. Demonstration data throughout; nothing on this page is investment
          advice.
        </p>
      </footer>
    </div>
  );
}
