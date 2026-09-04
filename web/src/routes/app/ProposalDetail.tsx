import { Link, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { AuditTimeline } from '@/components/domain/AuditTimeline';
import { AuthorizationPanel } from '@/components/domain/AuthorizationPanel';
import { ConfidenceReadout } from '@/components/domain/Confidence';
import {
  AuthorizationLedger,
  AuthorizationSeal,
  PreventionBar,
  ReasonCodes,
} from '@/components/domain/DecisionCard';
import { Datum } from '@/components/domain/Datum';
import {
  ExecutionBadge,
  VERDICT_GLYPH,
  VERDICT_LABEL,
  VerdictBadge,
} from '@/components/domain/Decision';
import { ProvenanceBadge } from '@/components/domain/ProvenanceBadge';
import { GateList, PayloadTable, RiskCheckList } from '@/components/domain/RiskChecks';
import { WhyPanel } from '@/components/domain/WhyPanel';
import { Badge } from '@/components/ui/Badge';
import { Panel } from '@/components/ui/Panel';
import { Empty, Loading, LoadError, Unavailable } from '@/components/ui/State';
import { Tabs } from '@/components/ui/Tabs';
import type { TabItem } from '@/components/ui/Tabs';
import { SESSION_ANCHOR } from '@/data/clock';
import { decimalMoney } from '@/lib/decimal';
import { cx, decimal, integer, money, percent, relative, stampOf, timeOf } from '@/lib/format';
import { useAsync } from '@/lib/hooks';
import { api } from '@/services/api';
import type { AuditEvent, Proposal } from '@/types/domain';

/**
 * The decision record, composed across the governance datum.
 *
 * Reading order is the disclosure ladder, and the ladder now starts where the
 * product does:
 *
 *   L1  the verdict, in the masthead, before anything that needs explaining
 *   L1  Requested -> Authorized -> Executed, the gap policy created, at size,
 *       supported by the prevention bar and the authorization seal
 *   --  the claim: instrument, thesis, and the caller-supplied market inputs
 *   ==  the datum
 *   L2  why, drawn from the deterministic check arithmetic
 *   L3  every check, every threshold, every actual
 *   L4  the authorization, its bindings and the full audit trail
 *   L5  the immutable record and its hash chain
 */
export function ProposalDetail() {
  const { proposalId = '' } = useParams();
  const proposal = useAsync(() => api.getProposal(proposalId), [proposalId]);
  const audit = useAsync(() => api.getProposalAudit(proposalId), [proposalId]);

  if (proposal.loading) return <Loading label="Loading decision record" />;
  if (proposal.error) return <LoadError error={proposal.error} />;
  if (!proposal.data) {
    return (
      <div className="page">
        <Empty title="No such proposal" hint={<Link to="/app/proposals">Back to proposals</Link>} />
      </div>
    );
  }

  const p = proposal.data;
  const inst = p.instrument;
  const isOption = inst.type === 'option';
  const decision = p.governor?.decision ?? null;

  const tabs: TabItem[] = [
    { id: 'detail', label: 'Detail', content: <DetailLevel proposal={p} /> },
    {
      id: 'evidence',
      label: 'Evidence',
      count: audit.data?.length,
      content: <EvidenceLevel proposal={p} events={audit.data ?? []} />,
    },
    { id: 'record', label: 'Record', content: <RecordLevel proposal={p} events={audit.data ?? []} /> },
  ];

  return (
    <div className="page page--case">
      <div className="case__top">
        <Link className="backlink" to="/app/proposals">
          <ArrowLeft size={13} aria-hidden="true" /> Proposals
        </Link>
      </div>

      {/* ------------------------------------------------------------- L1 */}
      <Masthead proposal={p} />

      {/* ---------------------------------------------- the outcome, at the top */}
      <section className="outcome" aria-label="Requested, authorized, executed">
        <Panel eyebrow="Authorization" title="Requested, authorized, executed" className="panel--ledger">
          <AuthorizationLedger outcome={p.outcome} size="lg" />
          <PreventionBar outcome={p.outcome} decision={decision} />
          <ReasonCodes codes={p.reasonCodes} />
          <AuthorizationSeal authorization={p.authorization} reasonCodes={p.reasonCodes} />
        </Panel>
      </section>

      {/* ------------------------------------------- the claim, above the line */}
      <section className="claim" aria-label="What was requested">
        <div className="claim__grid">
          <Panel title="Instrument" eyebrow="Requested trade">
            <dl className="kv">
              {inst.type === 'option' ? (
                <>
                  <Row k="Underlying" v={<span className="u-mono">{inst.underlying}</span>} />
                  <Row k="Strategy" v={inst.strategy.replace(/_/g, ' ').toLowerCase()} />
                  <Row k="Contracts requested" v={String(inst.quantity)} />
                  <Row k="Expiry" v={`${inst.expiry} · ${inst.daysToExpiry} DTE`} />
                  <Row k="Net premium" v={inst.netPremiumPerUnit === null ? <Unavailable reason="The decision record does not declare a net premium." /> : `${money(inst.netPremiumPerUnit)} per share`} />
                  <Row k="Maximum defined loss" v={inst.maxDefinedLoss === null ? <Unavailable reason="No defined-loss amount is exposed by this record." /> : <strong>{money(inst.maxDefinedLoss)}</strong>} />
                  <Row
                    k="Maximum profit"
                    v={inst.maxProfitKnown === false ? <Unavailable reason="No maximum-profit amount is exposed by this record." /> : inst.maxProfit === null ? <Badge tone="warn">Unbounded</Badge> : money(inst.maxProfit)}
                  />
                </>
              ) : (
                <>
                  <Row k="Symbol" v={<span className="u-mono">{inst.symbol}</span>} />
                  <Row k="Side" v={inst.side} />
                  <Row k="Quantity requested" v={`${inst.quantity} shares`} />
                  <Row k="Estimated price" v={money(inst.estimatedPrice)} />
                  <Row k="Requested notional" v={decimalMoney(p.outcome.requested.notional) ?? '—'} />
                </>
              )}
              <Row
                k="Created"
                v={
                  <time dateTime={p.createdAt} title={p.createdAt}>
                    {stampOf(p.createdAt)} UTC
                  </time>
                }
              />
            </dl>

            <div className="panel__note">
              <ConfidenceReadout
                reported={p.strategyConfidence}
                calibration={p.aiRisk?.calibration ?? null}
                label="Agent confidence"
              />
            </div>

            {inst.type === 'option' && (
              <div className="legs">
                <p className="u-label">Legs</p>
                <ul>
                  {inst.legs.map((leg) => (
                    <li key={leg.optionSymbol} className={cx('legs__row', leg.side === 'SELL' && 'legs__row--short')}>
                      <span className={cx('side', leg.side === 'SELL' && 'side--sell')}>{leg.side}</span>
                      <span className="u-mono">{leg.optionSymbol}</span>
                      <span>
                        {leg.optionType} {money(leg.strike)}
                      </span>
                      <span className="u-dim">×{leg.ratio}</span>
                    </li>
                  ))}
                </ul>
                <p className="legs__note">
                  Every short leg is covered by a long leg. Naked short structures are not accepted by the risk engine.
                </p>
              </div>
            )}
          </Panel>

          <Panel title="Thesis" eyebrow="Why this trade was requested">
            <p className="prose">{p.thesis}</p>
            <div className="callout callout--invalidation">
              <p className="u-label">Invalidation condition</p>
              <p>{p.invalidationCondition}</p>
            </div>
            <p className="u-label claim__subhead">Upstream evidence</p>
            <p className="prose">{p.researchSummary}</p>
            <div className="chips">
              {p.sourceAgents.map((agent) => (
                <span key={agent} className="chip">
                  {agent}
                </span>
              ))}
            </div>
          </Panel>

          <Panel title="Market risk inputs" eyebrow="Caller supplied">
            {p.marketRisk ? (
              <>
                <dl className="kv">
                  <Row k="Annualised volatility" v={decimal(p.marketRisk.annualizedVolatility)} />
                  <Row k="30-day maximum drawdown" v={percent(p.marketRisk.maxDrawdown30d, 0)} />
                  <Row k="Liquidity score" v={decimal(p.marketRisk.liquidityScore)} />
                </dl>
                <p className="panel__note">
                  <ProvenanceBadge value={p.marketRisk.provenance} size="xs" /> These are assertions from the upstream
                  provider. This backend has no market-data feed and does not verify them.
                </p>
              </>
            ) : (
              <Unavailable reason="No market-risk snapshot accompanied this proposal." />
            )}
          </Panel>
        </div>
      </section>

      {/* --------------------------------------------------------- the datum */}
      <Datum
        stages={p.stages}
        selectable
        showDetail
        caption={
          <>
            The ruling is stated above. Everything between it and this line is the case an agent made for the trade;
            everything below the line is the measurement that produced the ruling, and the record policy wrote.{' '}
            {isOption ? 'Option structure' : 'Equity'}.
          </>
        }
        readout={
          decision ? (
            <>
              <VerdictBadge decision={decision} />
              <span className="datum__readout-qty">
                {p.outcome.authorized ? (
                  <>
                    {integer(p.outcome.authorized.quantity)} {p.outcome.unit} authorized
                  </>
                ) : (
                  'No quantity authorized'
                )}
              </span>
            </>
          ) : (
            <Badge tone="accent">In review</Badge>
          )
        }
      />

      {/* ------------------------------------------------------------ L2 why */}
      <section className="why" aria-label="What policy measured">
        <Panel
          eyebrow="Evidence"
          title="What policy measured"
          description="Drawn from the deterministic check output alone — threshold, current, requested, authorized — so it is provably the arithmetic the engine enforced rather than a paraphrase of it."
        >
          {p.hardRisk ? (
            <WhyPanel
              decision={decision}
              outcome={p.outcome}
              checks={p.hardRisk.checks}
              bindingRule={p.governor?.bindingConstraint ?? null}
            />
          ) : (
            <Unavailable reason="The deterministic engine has not run for this proposal yet." />
          )}
        </Panel>
      </section>

      {/* ---------------------------------------------------------- L3–L5 */}
      <section className="case__depth" aria-label="Forensic detail">
        <Tabs items={tabs} />
      </section>
    </div>
  );
}

/* --------------------------------------------------------------- L1 masthead */

/**
 * Level 1 in one band.
 *
 * The verdict has to land before the reader meets a single argument, so it sits
 * above the claim register rather than inside it. The requested figure is never
 * struck through or replaced — an operator needs to see what the agent asked
 * for as well as what it got.
 */
function Masthead({ proposal: p }: { proposal: Proposal }) {
  const decision = p.governor?.decision ?? null;
  const changed =
    decision === 'REDUCE' &&
    p.outcome.authorized !== null &&
    p.outcome.authorized.quantity < p.outcome.requested.quantity;

  return (
    <header className={cx('masthead', decision && `masthead--${decision.toLowerCase()}`)}>
      <div className="masthead__lead">
        <h1 className="masthead__title">{headline(p)}</h1>
        <div className="masthead__meta">
          <time className="u-mono" dateTime={p.governor?.decidedAt ?? p.createdAt}>
            {timeOf(p.governor?.decidedAt ?? p.createdAt)} UTC
          </time>
          {p.chain && (
            <code className="u-mono" title={`Record ${p.chain.position} · ${p.chain.recordHash}`}>
              #{p.chain.recordHash.slice(0, 6)}
            </code>
          )}
          <span className="u-mono u-dim">{p.proposalId}</span>
        </div>
      </div>

      <div className="masthead__verdict">
        {decision ? <VerdictBadge decision={decision} size="md" /> : <Badge tone="accent" size="md">IN REVIEW</Badge>}
        {changed && p.outcome.authorized && (
          <p className="masthead__delta">
            <span aria-hidden="true">{VERDICT_GLYPH.REDUCE}</span> {VERDICT_LABEL.REDUCE}{' '}
            <span className="u-mono">
              {integer(p.outcome.requested.quantity)} → {integer(p.outcome.authorized.quantity)}
            </span>
            {p.outcome.preventedNotional && (
              <span className="masthead__prevented">
                {decimalMoney(p.outcome.preventedNotional)} of exposure prevented
              </span>
            )}
          </p>
        )}
        {decision === 'REJECT' && (
          <p className="masthead__delta">
            <span aria-hidden="true">{VERDICT_GLYPH.REJECT}</span> No quantity authorized
            {p.outcome.preventedNotional && (
              <span className="masthead__prevented">
                {decimalMoney(p.outcome.preventedNotional)} of exposure prevented
              </span>
            )}
          </p>
        )}
        <p className="masthead__identity">
          {p.sourceAgents[p.sourceAgents.length - 1] ?? 'Trader Agent'} · Policy v18 ·{' '}
          {p.aiRisk?.modelLabel ?? 'AI Risk Model'}
        </p>
      </div>
    </header>
  );
}

/* ---------------------------------------------------------------- L3 detail */

/**
 * Level 3: the whole check surface.
 *
 * Both columns are governance now. The claim lives above the datum, so this
 * level no longer has to run a half-empty intelligence column beside two
 * thousand pixels of policy output.
 */
function DetailLevel({ proposal: p }: { proposal: Proposal }) {
  return (
    <div className="case__split">
      <div className="case__col">
        <Panel title="Deterministic checks" eyebrow="Hard policy">
          {p.hardRisk ? (
            <>
              <div className="scoreline">
                <div>
                  <span className="u-label">Risk score</span>
                  <strong className="scoreline__score">{p.hardRisk.riskScore ?? 'Unavailable'}</strong>
                  {p.hardRisk.riskScore !== null && <span className="u-dim"> / 100</span>}
                </div>
                <div>
                  <span className="u-label">Policy maximum</span>
                  <strong className="scoreline__score">{p.hardRisk.recommendedQuantity}</strong>
                  <span className="u-dim"> of {p.hardRisk.originalQuantity} requested</span>
                </div>
                <ProvenanceBadge value={p.hardRisk.provenance} size="xs" />
              </div>
              <RiskCheckList checks={p.hardRisk.checks} />
            </>
          ) : (
            <Unavailable reason="The deterministic engine has not run for this proposal yet." />
          )}
        </Panel>
      </div>

      <div className="case__col">
        <Panel title="Verdict" eyebrow="Final size" className="panel--verdict">
          {p.governor ? (
            <>
              <div className="verdict">
                <VerdictBadge decision={p.governor.decision} size="md" />
              </div>
              <p className="prose">{p.governor.reason}</p>
              <dl className="kv">
                <Row
                  k="Binding constraint"
                  v={
                    p.governor.bindingConstraint ? (
                      <code className="u-mono">{p.governor.bindingConstraint}</code>
                    ) : (
                      'None — authorized as requested'
                    )
                  }
                />
                <Row k="Risk score" v={p.governor.riskScore === null ? <Unavailable reason="The backend does not emit a composite risk score." /> : `${p.governor.riskScore} / 100`} />
                <Row
                  k="Decided"
                  v={
                    <time dateTime={p.governor.decidedAt} title={p.governor.decidedAt}>
                      {stampOf(p.governor.decidedAt)} UTC · {relative(p.governor.decidedAt, SESSION_ANCHOR)}
                    </time>
                  }
                />
              </dl>
            </>
          ) : (
            <Unavailable reason="No verdict has been reached for this proposal yet." />
          )}
        </Panel>

        <Panel title="Contextual review" eyebrow="Advisory only">
          {p.aiRisk ? (
            <>
              <div className="scoreline">
                <div>
                  <span className="u-label">Recommendation</span>
                  <VerdictBadge decision={p.aiRisk.recommendation} />
                </div>
                <div>
                  <span className="u-label">Suggested</span>
                  <strong className="scoreline__score">{p.aiRisk.recommendedQuantity}</strong>
                </div>
                <ProvenanceBadge value={p.aiRisk.provenance} size="xs" />
              </div>
              <ConfidenceReadout
                reported={p.aiRisk.confidence}
                calibration={p.aiRisk.calibration}
                label="Model confidence"
              />
              <p className="prose">{p.aiRisk.riskThesis}</p>
              <p className="u-label">Context raised</p>
              <ul className="reasons">
                {p.aiRisk.hiddenRisks.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              <p className="panel__note">
                This review has no authority. It may argue for a smaller size than policy permits; it has no mechanism
                to raise a ceiling, and it is never the explanation shown above.
              </p>
            </>
          ) : (
            <Unavailable
              reason={
                p.hardRisk?.blocked
                  ? 'Not consulted: a deterministic block is final, so there was nothing to review.'
                  : 'The contextual review has not returned yet.'
              }
            />
          )}
        </Panel>

        <Panel
          title="Execution gate"
          eyebrow="Safety"
          actions={p.execution ? <ExecutionBadge state={p.execution.state} /> : undefined}
        >
          {p.execution ? (
            <>
              <p className="prose">{p.execution.message}</p>
              {p.execution.clientOrderId && (
                <p className="panel__note">
                  Client order ID <code className="u-mono">{p.execution.clientOrderId}</code> ·{' '}
                  <Link to="/app/orders">open the order lifecycle</Link>
                </p>
              )}
              <GateList gates={p.execution.gates} />
            </>
          ) : (
            <Unavailable reason="The execution gate has not been entered for this proposal." />
          )}
        </Panel>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- L4 evidence */

function EvidenceLevel({ proposal: p, events }: { proposal: Proposal; events: AuditEvent[] }) {
  return (
    <div className="case__evidence">
      <Panel eyebrow="Permission" title="Authorization">
        <AuthorizationPanel authorization={p.authorization} outcome={p.outcome} subject={authorizationSubject(p)} />
      </Panel>

      <Panel eyebrow="Record" title="Audit trail" description={`${events.length} durable events`} flush>
        {events.length > 0 ? (
          <AuditTimeline events={events} linkProposals={false} />
        ) : (
          <p className="panel__inner u-dim">No audit events recorded for this proposal.</p>
        )}
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------ L5 raw */

function RecordLevel({ proposal: p, events }: { proposal: Proposal; events: AuditEvent[] }) {
  return (
    <div className="case__record">
      <Panel eyebrow="Immutable" title="Hash chain">
        {p.chain ? (
          <dl className="kv">
            <Row k="Chain position" v={<span className="u-mono">{integer(p.chain.position)}</span>} />
            <Row k="Record hash" v={<code className="u-mono u-break">{p.chain.recordHash}</code>} />
            <Row k="Previous hash" v={<code className="u-mono u-break">{p.chain.previousHash}</code>} />
            <Row
              k="Verified"
              v={
                <>
                  {p.chain.verified ? '✓ Stored record hash verified' : '✕ Verification failed'}
                  {p.chain.verifyMs !== null && <span className="u-dim"> · {p.chain.verifyMs}ms</span>}
                </>
              }
            />
            <Row
              k="Verified at"
              v={
                <time dateTime={p.chain.verifiedAt} title={p.chain.verifiedAt}>
                  {stampOf(p.chain.verifiedAt)} UTC
                </time>
              }
            />
          </dl>
        ) : (
          <Unavailable reason="This proposal has not been sealed into the chain yet." />
        )}
      </Panel>

      <Panel eyebrow="Payloads" title="Sanitised event payloads" description="Credentials never appear in a record.">
        <ul className="recordlist">
          {events.map((e) => (
            <li key={e.eventId} className="recordlist__item">
              <p className="recordlist__head">
                <code className="u-mono">{e.action}</code>
                <time dateTime={e.at} title={e.at} className="u-dim u-mono">
                  {stampOf(e.at)} UTC
                </time>
              </p>
              <PayloadTable payload={e.payload} />
            </li>
          ))}
          {events.length === 0 && <li className="u-dim">No payloads recorded for this proposal.</li>}
        </ul>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------ helpers */

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv__row">
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
}

function headline(p: Proposal) {
  const inst = p.instrument;
  return inst.type === 'option'
    ? `${inst.underlying} · ${inst.strategy.replace(/_/g, ' ').toLowerCase()}`
    : `${inst.symbol} · ${inst.side.toLowerCase()}`;
}

function authorizationSubject(p: Proposal) {
  const inst = p.instrument;
  const qty = p.outcome.authorized?.quantity ?? p.outcome.requested.quantity;
  const symbol = inst.type === 'equity' ? inst.symbol : inst.underlying;
  return `${integer(qty)} ${p.outcome.unit} ${symbol} · policy v18 · Alpaca Paper`;
}

export type { Proposal };
