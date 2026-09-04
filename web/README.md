# Mizan — web frontend

The operator interface for the portfolio intelligence and governance platform:
market-intelligence agents propose trades, a deterministic risk engine and the
Portfolio Governor decide what size may reach Alpaca Paper, and every step is
recorded.

The finalized interface can run on its typed demo dataset or on the authenticated
authoritative `mizan.api` v1 surface. HTTP mode never falls back to demo data.

## Run it

```bash
npm install --prefix web
```

```bash
npm run dev --prefix web
```

Then open <http://localhost:5173>.

Other scripts:

```bash
npm run typecheck --prefix web
```

```bash
npm run lint --prefix web
```

```bash
npm run build --prefix web
```

## Stack

React 18 · TypeScript (strict) · Vite 6 · React Router 6 · Lucide icons ·
Framer Motion (landing reveals only) · tokenised plain CSS.

No CSS framework and no component library: the token system in
`src/styles/tokens.css` is the design system, and every component reads semantic
roles from it. **No component stylesheet contains a raw colour value.**

## Layout

```
web/
├── REFERENCE_RESEARCH.md    design research behind the baseline
├── docs/                    frontend copies of the Step 1 artefacts
│   ├── DECISIONS.md
│   └── SPEC.md
└── src/
    ├── types/domain.ts      the domain model, mirroring the backend contract
    ├── data/                typed demo data (one file per domain area)
    ├── services/            the API adapter boundary
    │   ├── types.ts         GovernorApi — the only surface components see
    │   ├── mockClient.ts    Step 2 implementation, reads src/data
    │   ├── httpClient.ts    Step 3 implementation, transport in place
    │   └── api.ts           picks one via VITE_API_MODE
    ├── state/app.tsx        theme, density and operator controls
    ├── lib/                 formatting and hooks
    ├── components/
    │   ├── ui/              Panel, Badge, Button, DataTable, Drawer, Meter, Tabs…
    │   ├── domain/          AgentPipeline, ProvenanceBadge, QuantityLedger,
    │   │                    MetricStrip, RiskChecks, AuditTimeline, DecisionReplay
    │   └── shell/           AppShell, Sidebar, TopBar, CommandPalette, Mark
    ├── routes/
    │   ├── app/             the nine application views
    │   └── landing/         the public page
    └── styles/              tokens · base · ui · shell · domain · pages · landing
```

## Data source

`src/services/api.ts` chooses the implementation:

| `VITE_API_MODE` | Behaviour |
| --- | --- |
| unset or `mock` | Demo dataset from `src/data` (default) |
| `http` | Authenticated Mizan v1 API through the `/api` dev proxy |

For a local read-only integration run against the shipped evidence ledger:

```powershell
$env:ALPACA_PAPER='true'
$env:MIZAN_API_TOKEN='choose-a-local-token-at-least-16-characters'
python scripts/run_operator_api.py --ledger evidence/live-ledger --broker none

$env:VITE_API_MODE='http'
$env:MIZAN_API_URL='http://127.0.0.1:8000'
npm run dev --prefix web
```

The Vite development proxy reads `MIZAN_API_TOKEN` on the server side and adds
the tenant bearer to `/api` requests. No credential is placed in a `VITE_*`
variable or compiled into the browser bundle. A deployed frontend must use an
equivalent same-origin authenticated gateway; deployment is outside this step.

`--broker none` is explicit: recorded decisions, policy, health, and chain verification
remain available, while no broker operation can occur. Use `--broker alpaca-py` only
with Alpaca paper credentials; `ALPACA_PAPER=true` is mandatory in either mode.

The HTTP client maps decisions, policy, audit verification, controls, portfolio
snapshots, and genuine execution evidence into the UI domain. Unsupported values
render as unavailable and genuine empty collections remain empty. The live mode is shown in
**Settings → Safety & system**, so which source you are looking at is never a
guess.

## Rules this frontend keeps

- **Paper only.** Live trading is never presented as configurable.
- **Provenance on anything mistakable for market truth.** `ProvenanceBadge`
  values: Alpaca Paper, Live portfolio, Live agent, AI risk model, Caller
  supplied, Demo, Synthetic. The AI infrastructure provider is not one of them.
- **Unavailable is not zero.** `Sourced<T>` carries `value: T | null`; a missing
  value renders the word `Unavailable` with the reason, never `0.00`.
- **No invented analytics.** No implied volatility, Greeks, probability of profit,
  sector exposure or correlations — nothing supplies them, so nothing displays
  them.
- **No credentials anywhere.** Connections show state only; there is no view in
  the product capable of rendering a key.
- **Colour is never the only signal.** Every status has a word and a marker shape.
