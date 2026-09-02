# Portfolio Governor — durable Alpaca PAPER backend

This repository is the risk, authorization, execution, persistence, and API backend for an AI trading-agent hackathon project.

```text
Research / Strategy / Probability Agents
                  ↓
            TradeProposal
                  ↓
       deterministic RiskEngine
                  ↓
       Featherless AI Risk review
                  ↓
         PortfolioGovernor
                  ↓
   authorization + ExecutionGate
                  ↓
     isolated Alpaca PAPER executor
                  ↓
       persistent broker order
                  ↓
     read-only REST reconciliation
                  ↓
      durable audit + FastAPI
                  ↓
               frontend
```

**LIVE TRADING IS NOT SUPPORTED.** There is no supported live-client configuration. `ALPACA_PAPER=false` fails closed before an Alpaca client can be used for execution and fails again at the execution gate.

## Safety architecture

The hard policy is deterministic because limits such as buying power, daily drawdown, allocation, concentration, volatility, liquidity, and minimum strategy confidence must be reproducible and testable. Missing risk-critical portfolio data is not converted into a safe-looking zero; it blocks the proposal.

Featherless performs a contextual second review using JSON structured output. The model authors only recommendation, confidence, quantity, thesis, hidden risks, and reasoning. Pydantic rejects extra, malformed, truncated, or semantically unsafe output. Provider failure produces an explicit conservative fallback. The LLM may reduce or reject, but `PortfolioGovernor` prevents it from overriding a hard rejection or increasing the deterministic quantity cap.

Execution is disabled by default and dry-run is enabled by default. A trade can reach the sole `TradingClient.submit_order` call only after:

1. a persisted proposal, deterministic report, and executable Governor decision agree;
2. the decision and authorization are fresh;
3. paper mode is proven and the kill switch is off;
4. deterministic `pgv5-<SHA256(proposal_id)>` idempotency finds no matching order;
5. a fresh Alpaca PAPER portfolio still supports the exact authorized quantity;
6. the symbol is an active, tradable US equity;
7. the regular market clock is open; and
8. authorization freshness is checked again immediately before the mutation.

Execution never silently resizes. If fresh risk supports fewer shares, it returns `REAUTHORIZATION_REQUIRED`. Existing broker orders are reconciled after restart instead of submitted again. Sell execution cannot create an intentional short position. Cancellation, replacement, close-position, close-all, and other broker mutations are absent.

The execution levels are:

- `ALPACA_EXECUTION_ENABLED=false`: execution adapter is never reached.
- enabled plus `ALPACA_EXECUTION_DRY_RUN=true`: all read-only broker and safety checks run, then `WOULD_SUBMIT` is returned.
- enabled plus dry-run false: a simple whole-share DAY order may reach Alpaca PAPER only when every gate passes.
- `ALPACA_EXECUTION_KILL_SWITCH=true`: new execution is blocked immediately.

## Durable state and audit

SQLite is used through Python's built-in `sqlite3`; there is no hosted database dependency. The default location is `./data/portfolio_governor.db`, configurable with `APP_DB_PATH`. Parent directories are created automatically. Runtime `.db`, WAL, and shared-memory files are gitignored.

Initialization is idempotent and versioned. Every connection enables foreign keys, a busy timeout, and WAL mode for local demo concurrency. Structured values use Pydantic JSON only—never pickle or `eval`—with UTC ISO-8601 datetimes.

Durable entities include proposals, evaluation portfolio snapshots, caller-supplied market-risk snapshots, risk reports, AI analyses, Governor decisions, execution authorizations, execution results, broker orders, and audit events. Evaluation records are immutable per proposal identity; execution and broker state advance safely. Audit ordering uses an SQLite sequence so retrieval is deterministic oldest to newest.

Recursive audit sanitization redacts API keys, secrets, credentials, passwords, tokens, authorization headers, and nested header collections before persistence. API responses do not expose database paths, account IDs, credentials, or raw Alpaca objects.

## Broker lifecycle and reconciliation

`OrderReconciliationService` uses the installed `alpaca-py` `get_order_by_id`/client-order lookup read path, maps raw status into `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `REJECTED`, `PENDING`, or `UNKNOWN`, and preserves the raw broker status. Filled quantity, average price, and fill timestamp are persisted. Partial fills are not terminal.

Only meaningful state changes append `ORDER_STATE_CHANGED`; repeated unchanged polls do not spam the audit. Reconciliation never cancels, replaces, or mutates an order. Bounded polling defaults to 30 seconds at two-second intervals and returns the terminal or latest state without automatically canceling. REST reconciliation is the authoritative implementation; optional `TradingStream` support was intentionally not added because it is unnecessary for the demo and would make WebSocket availability a runtime dependency.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure Featherless and an Alpaca **paper** account in `.env`. `.env` is gitignored.

```dotenv
FEATHERLESS_API_KEY=
FEATHERLESS_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
FEATHERLESS_BASE_URL=https://api.featherless.ai/v1

ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER=true
ALPACA_EXECUTION_ENABLED=false
ALPACA_EXECUTION_DRY_RUN=true
ALPACA_EXECUTION_KILL_SWITCH=false
EXECUTION_MAX_DECISION_AGE_SECONDS=120

APP_DB_PATH=./data/portfolio_governor.db
CORS_ORIGINS=http://localhost:5173,http://localhost:3000
ORDER_RECONCILE_TIMEOUT_SECONDS=30
ORDER_RECONCILE_INTERVAL_SECONDS=2
```

CORS accepts a comma-separated list or JSON string array. Wildcard origins are rejected; local ports 5173 and 3000 are the safe defaults.

## API

Start the local API:

```powershell
uvicorn app.api:app --reload
```

Endpoints:

- `GET /health` — safe runtime flags, AI provider, and database health.
- `GET /portfolio` — sanitized live Alpaca PAPER `PortfolioSnapshot`.
- `POST /proposals/evaluate` — fetch current portfolio, evaluate, govern, persist, and audit; never executes.
- `POST /proposals/{proposal_id}/execute` — load stored decision and pass every execution gate; it accepts no replacement trade.
- `GET /proposals/{proposal_id}` — full durable lifecycle.
- `GET /proposals/{proposal_id}/audit` — ordered durable timeline.
- `GET /orders/{client_order_id}` — stored order with safe best-effort read-only refresh.
- `POST /orders/{client_order_id}/reconcile` — explicit read-only broker reconciliation.
- `GET /recent?limit=20` — bounded demo list, maximum 100.

Structured errors use a stable code and do not contain tracebacks:

```json
{"error":{"code":"PROPOSAL_NOT_FOUND","message":"Proposal was not found."}}
```

Example PowerShell flow:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/portfolio

$body = @{
  proposal = @{
    proposal_id = "faisal-aapl-001"
    symbol = "AAPL"
    side = "BUY"
    quantity = 1
    estimated_price = 250.00
    strategy_confidence = 0.82
    thesis = "Upstream strategy thesis."
    invalidation_condition = "The strategy signal reverses."
  }
  market_risk = @{
    symbol = "AAPL"
    annualized_volatility = 0.30
    max_drawdown_30d = 0.10
    liquidity_score = 0.95
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -ContentType application/json -Body $body `
  http://localhost:8000/proposals/evaluate
Invoke-RestMethod http://localhost:8000/proposals/faisal-aapl-001
Invoke-RestMethod -Method Post http://localhost:8000/proposals/faisal-aapl-001/execute
Invoke-RestMethod http://localhost:8000/proposals/faisal-aapl-001/audit
```

The market-risk fields above are caller-supplied examples, not a live data feed. Faisal's probability/research pipeline should implement the `MarketRiskProvider` contract and supply measured annualized volatility, 30-day maximum drawdown, and a documented normalized liquidity score. See [the complete integration contract](docs/integration-contract.md).

## Upstream ownership

Upstream agents create `TradeProposal`; they do not send a Governor decision, final execution quantity, authorization, portfolio values, or broker order. They do not call Alpaca execution. This backend owns risk, contextual review, Governor sizing, authorization, broker execution, persistence, reconciliation, and audit.

## Tests and demos

All automated tests mock external services and never contact Alpaca or Featherless:

```powershell
python -m pytest -q
```

Available demos:

```powershell
python demo_risk_agent.py
python demo_alpaca_portfolio.py
python demo_integrated_risk.py
python demo_paper_execution.py
python demo_full_lifecycle.py
python probe_featherless.py
```

`demo_full_lifecycle.py` labels its market-risk values as manual demo input, persists every phase, runs execution or dry-run through the normal service, performs bounded REST reconciliation only when a broker order exists, and prints the durable audit timeline. Its default configuration submits no order. It uses a stable default demo proposal ID so a known broker order is reconciled rather than duplicated; set a new `DEMO_PROPOSAL_ID` only for a genuinely new proposal.

`probe_featherless.py` remains an isolated provider diagnostic. Claude remains an optional stable adapter; Featherless is the primary API provider.
