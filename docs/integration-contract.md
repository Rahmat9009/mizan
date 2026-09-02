# Upstream and frontend integration contract

The upstream research, stock-selection, and probability agents produce a `TradeProposal` plus an explicit `MarketRiskSnapshot`. They never call Alpaca's execution API.

## Evaluate a proposal

`POST /proposals/evaluate`

```json
{
  "proposal": {
    "proposal_id": "faisal-strategy-20260901-aapl-001",
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 10,
    "estimated_price": 250.0,
    "strategy_confidence": 0.82,
    "thesis": "The upstream strategy's concise evidence-based thesis.",
    "invalidation_condition": "The precise observable condition that invalidates the thesis.",
    "created_at": "2026-09-01T12:00:00Z"
  },
  "market_risk": {
    "symbol": "AAPL",
    "annualized_volatility": 0.30,
    "max_drawdown_30d": 0.10,
    "liquidity_score": 0.95
  }
}
```

`proposal_id` and `created_at` may be omitted; the backend will generate them. Upstream systems should normally provide a stable, unique `proposal_id` so retries have one identity. `symbol` must be an uppercase US-equity symbol. `side` is `BUY` or `SELL`. `quantity` is a positive whole-share count. `estimated_price` is a positive finite price used by deterministic sizing. `strategy_confidence` is from 0 through 1. `thesis` and `invalidation_condition` must be non-empty.

The market-risk values are decimal ratios, not percentages: `0.30` means 30% annualized volatility and `0.10` means 10% maximum drawdown. `liquidity_score` is a normalized 0–1 score whose methodology and timestamp must be owned by the upstream provider. The current API treats this as caller-supplied/manual data and does not pretend it is live. `MarketRiskProvider` in `app/market_risk.py` is the provider interface for Faisal's pipeline. The backend validates the fields and symbol but does not fabricate or fetch them.

The backend fetches the current Alpaca PAPER portfolio itself. Clients cannot inject portfolio equity, buying power, positions, or daily P&L.

## Ownership boundary

Upstream agents do not:

- execute orders or call Alpaca trading mutations;
- override deterministic risk policy;
- send a risk report, AI analysis, Governor decision, authorization, or execution quantity;
- resubmit a replacement trade to the execution endpoint.

The Portfolio Governor owns deterministic risk, Featherless contextual review, final Governor sizing, authorization freshness, idempotency, asset and market checks, fresh portfolio revalidation, PAPER execution, reconciliation, persistence, and audit.

Evaluation never executes. If the returned Governor decision is acceptable to the product workflow, call `POST /proposals/{proposal_id}/execute` with no trade body. The backend loads the immutable stored proposal and decision. Disabled and dry-run states remain normal typed `ExecutionResult` responses.

## Read model for the frontend

- `GET /proposals/{proposal_id}` returns the full stored lifecycle.
- `GET /proposals/{proposal_id}/audit` returns oldest-to-newest durable events.
- `GET /orders/{client_order_id}` returns a sanitized internal broker-order snapshot and may perform a read-only refresh.
- `POST /orders/{client_order_id}/reconcile` explicitly performs a read-only refresh. It never cancels, replaces, or modifies an order.
- `GET /recent?limit=20` returns a bounded list for demo UI views.

Errors use `{"error":{"code":"...","message":"..."}}`. Validation errors also contain safe field details. Credentials, raw Alpaca SDK objects, account IDs, local database paths, and stack traces are never response fields.

**LIVE TRADING IS NOT SUPPORTED.** `ALPACA_PAPER=false` fails closed and no live-client configuration path exists.
