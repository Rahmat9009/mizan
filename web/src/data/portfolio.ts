import type { PortfolioSummary, Position } from '@/types/domain';
import { ago } from './clock';

/**
 * Paper-account portfolio state.
 *
 * Every figure here is DEMO data shaped like the `PortfolioSnapshot` the
 * backend returns from Alpaca Paper. Values that a real snapshot may not carry
 * are modelled as `Sourced<T>` so the UI can render "Unavailable" rather than a
 * misleading zero.
 */

export const EQUITY = 59_950.0;

export const PORTFOLIO_SUMMARY: PortfolioSummary = {
  source: 'ALPACA_PAPER',
  equity: { value: EQUITY, provenance: 'DEMO', asOf: ago(1) },
  cash: { value: 21_228.3, provenance: 'DEMO', asOf: ago(1) },
  buyingPower: { value: 79_420.55, provenance: 'DEMO', asOf: ago(1) },
  dailyPnl: { value: -252.3, provenance: 'DEMO', asOf: ago(1) },
  dailyPnlPct: { value: -0.0042, provenance: 'DEMO', asOf: ago(1) },
  realizedPnl: { value: 0, provenance: 'DEMO', asOf: ago(1) },
  unrealizedPnl: { value: 828.7, provenance: 'DEMO', asOf: ago(1) },
  capitalAtRisk: { value: 38_616.7, provenance: 'DEMO', asOf: ago(1) },
};

export const POSITIONS: Position[] = [
  {
    symbol: 'JPM',
    assetClass: 'us_equity',
    side: 'LONG',
    quantity: 55,
    marketValue: 15_900.5,
    costBasis: 15_290.0,
    currentPrice: 289.1,
    unrealizedPl: 610.5,
    unrealizedPlPct: 0.0399,
    weight: 0.2652,
    riskContribution: 15_900.5,
    riskBasis: 'MARKET_VALUE',
    sourceStrategy: 'Quality carry',
    provenance: 'DEMO',
  },
  {
    symbol: 'AAPL',
    assetClass: 'us_equity',
    side: 'LONG',
    quantity: 34,
    marketValue: 8_529.24,
    costBasis: 8_410.0,
    currentPrice: 250.86,
    unrealizedPl: 119.24,
    unrealizedPlPct: 0.0142,
    weight: 0.1423,
    riskContribution: 8_529.24,
    riskBasis: 'MARKET_VALUE',
    sourceStrategy: 'Installed-base upgrade',
    provenance: 'DEMO',
  },
  {
    symbol: 'GOOGL',
    assetClass: 'us_equity',
    side: 'LONG',
    quantity: 18,
    marketValue: 4_824.36,
    costBasis: 4_950.0,
    currentPrice: 268.02,
    unrealizedPl: -125.64,
    unrealizedPlPct: -0.0254,
    weight: 0.0805,
    riskContribution: 4_824.36,
    riskBasis: 'MARKET_VALUE',
    sourceStrategy: 'Search monetisation',
    provenance: 'DEMO',
  },
  {
    symbol: 'NVDA',
    assetClass: 'us_equity',
    side: 'LONG',
    quantity: 20,
    marketValue: 3_651.0,
    costBasis: 3_648.0,
    currentPrice: 182.55,
    unrealizedPl: 3.0,
    unrealizedPlPct: 0.0008,
    weight: 0.0609,
    riskContribution: 3_651.0,
    riskBasis: 'MARKET_VALUE',
    sourceStrategy: 'Revision momentum',
    provenance: 'DEMO',
  },
  {
    symbol: 'AMD',
    assetClass: 'us_equity',
    side: 'LONG',
    quantity: 22,
    marketValue: 3_636.6,
    costBasis: 3_520.0,
    currentPrice: 165.3,
    unrealizedPl: 116.6,
    unrealizedPlPct: 0.0331,
    weight: 0.0607,
    riskContribution: 3_636.6,
    riskBasis: 'MARKET_VALUE',
    sourceStrategy: 'Revision momentum',
    provenance: 'DEMO',
  },
  {
    symbol: 'MSFT 520/530C 16 OCT 26',
    assetClass: 'us_option',
    side: 'LONG',
    quantity: 5,
    marketValue: 2_180.0,
    costBasis: 2_075.0,
    currentPrice: 4.36,
    unrealizedPl: 105.0,
    unrealizedPlPct: 0.0506,
    weight: 0.0364,
    // For a defined-risk structure the honest risk figure is the maximum loss,
    // not the mark-to-market value and never the stock-equivalent notional.
    riskContribution: 2_075.0,
    riskBasis: 'DEFINED_MAX_LOSS',
    sourceStrategy: 'Cloud backlog · debit spread',
    provenance: 'DEMO',
  },
];

/** Exposure by asset type, derived from `POSITIONS` so the two never disagree. */
export function exposureByAssetType(positions: Position[] = POSITIONS) {
  const equity = positions.filter((p) => p.assetClass === 'us_equity');
  const options = positions.filter((p) => p.assetClass === 'us_option');
  const sum = (xs: Position[]) => xs.reduce((t, p) => t + p.marketValue, 0);
  return [
    { label: 'US equity', value: sum(equity), count: equity.length, riskBasis: 'Market value' as const },
    { label: 'US options (defined risk)', value: sum(options), count: options.length, riskBasis: 'Defined maximum loss' as const },
  ];
}
