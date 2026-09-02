# Mīzān backup demo UI

A minimal Streamlit client for the Portfolio Governor PAPER API. This is a
reliability backup for the demo, not the final frontend.

## Safety boundary

- The UI talks **only** to the backend over HTTP. It never imports `alpaca-py`,
  never holds credentials, and never submits an order to a broker.
- Execution happens through `POST /proposals/{id}/execute`. The backend remains
  the sole broker mutation boundary, and its dry-run and kill-switch behaviour
  is preserved unchanged.
- Operator-entered market-risk values are labelled **"Demo / manually supplied
  risk snapshot — NOT LIVE"** everywhere they appear.
- Backend execution status is displayed verbatim (`WOULD_SUBMIT`, `SUBMITTED`,
  `MARKET_CLOSED`, `EXECUTION_DISABLED`, `KILL_SWITCH_ACTIVE`, …). A dry run is
  never presented as a placed order.

## Install

```bash
python -m pip install -r ui/requirements-ui.txt
```

## Run

Terminal 1 — backend:

```bash
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Terminal 2 — UI:

```bash
python -m streamlit run ui/streamlit_app.py --server.port 8501
```

Then open <http://localhost:8501>.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MIZAN_BACKEND_URL` | `http://localhost:8000` | Initial backend base URL |
| `MIZAN_BACKEND_TIMEOUT` | `30` | Initial HTTP timeout in seconds |

Both are also editable live in the sidebar.

## Known integration gaps

- **Scout / Analyst / Bull / Bear / Trader agents are not in this backend.**
  Those pipeline cards render as `NOT INTEGRATED`; no reasoning is fabricated.
- **Options are not supported end-to-end.** `TradeProposal` is single-leg
  US-equity only and the execution service rejects any asset whose Alpaca asset
  class is not `us_equity`. Section 3 reports the mismatch instead of silently
  degrading an options structure into an equity order.
