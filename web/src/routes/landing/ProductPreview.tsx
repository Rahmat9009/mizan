import { AgentPipeline } from '@/components/domain/AgentPipeline';
import { VerdictBadge, OrderStateBadge, QuantityLedger } from '@/components/domain/Decision';
import { Badge } from '@/components/ui/Badge';
import { PROPOSALS } from '@/data/proposals';
import { POSITIONS, PORTFOLIO_SUMMARY } from '@/data/portfolio';
import { ORDERS } from '@/data/orders';
import { moneyCompact, percent, signedMoney } from '@/lib/format';

/**
 * A miniature of the real Command Center, built from the real components.
 *
 * Not a screenshot and not a mock-up: the same pipeline, badges and ledger the
 * application renders, at a smaller scale. A marketing page that shows
 * something the product cannot do is the first thing an operator stops
 * trusting.
 */
export function ProductPreview({ compact = false }: { compact?: boolean }) {
  const featured = PROPOSALS.find((p) => p.proposalId === 'sel-20260902-nvda-0114')!;
  const s = PORTFOLIO_SUMMARY;

  return (
    <div className={`preview ${compact ? 'preview--compact' : ''}`} aria-hidden="true">
      <div className="preview__chrome">
        <span className="preview__dot" />
        <span className="preview__crumb">Command Center</span>
        <Badge tone="brass" shape="diamond">
          Paper only
        </Badge>
      </div>

      <div className="preview__metrics">
        <div>
          <span>Equity</span>
          <strong>{moneyCompact(s.equity.value)}</strong>
        </div>
        <div>
          <span>Daily P&L</span>
          <strong className="u-neg">{signedMoney(s.dailyPnl.value)}</strong>
        </div>
        <div>
          <span>Buying power</span>
          <strong>{moneyCompact(s.buyingPower.value)}</strong>
        </div>
        <div>
          <span>Capital at risk</span>
          <strong>{percent((s.capitalAtRisk.value ?? 0) / (s.equity.value ?? 1), 0)}</strong>
        </div>
      </div>

      <div className="preview__pipeline">
        <AgentPipeline stages={featured.stages} variant="rail" />
      </div>

      {!compact && (
        <div className="preview__lower">
          <div className="preview__col">
            <p className="u-eyebrow">Active proposals</p>
            <ul>
              {PROPOSALS.slice(0, 3).map((p) => (
                <li key={p.proposalId}>
                  <span className="u-mono">
                    {p.instrument.type === 'equity' ? p.instrument.symbol : p.instrument.underlying}
                  </span>
                  <QuantityLedger
                    proposed={p.governor?.originalQuantity ?? p.instrument.quantity}
                    approved={p.governor?.approvedQuantity ?? p.instrument.quantity}
                    unit=""
                    size="sm"
                  />
                  {p.governor ? <VerdictBadge decision={p.governor.decision} /> : <Badge tone="accent">Review</Badge>}
                </li>
              ))}
            </ul>
          </div>
          <div className="preview__col">
            <p className="u-eyebrow">Positions</p>
            <ul>
              {POSITIONS.slice(0, 3).map((p) => (
                <li key={p.symbol}>
                  <span className="u-mono">{p.symbol}</span>
                  <span>{percent(p.weight)}</span>
                  <span className={p.unrealizedPl !== null && p.unrealizedPl >= 0 ? 'u-pos' : 'u-neg'}>{signedMoney(p.unrealizedPl)}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="preview__col">
            <p className="u-eyebrow">Orders</p>
            <ul>
              {ORDERS.slice(0, 3).map((o) => (
                <li key={o.clientOrderId}>
                  <span className="u-mono">{o.symbol}</span>
                  <span>
                    {o.filledQuantity}/{o.approvedQuantity}
                  </span>
                  <OrderStateBadge state={o.lifecycle} />
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
