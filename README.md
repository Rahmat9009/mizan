# Mizan

**A governance layer between an AI trading agent and a broker.** The agent proposes; Mizan decides.
Every decision is checked by a deterministic engine against a versioned policy, written to an
append-only hash-chained ledger, and re-derivable byte for byte from its own record by someone with no
credentials and no reason to trust us.

> **Start here: [`WRITEUP.md`](WRITEUP.md)** — the one-pager. What this is, what is proven, and how to
> check the central claim in under five minutes with no Alpaca key and no network.

**Paper trading only.** There is no live-trading code path in this build. `ALPACA_PAPER` must be
explicitly `true` before a credential is read or a socket is opened; absent is not permission, and
`false` fails closed with nothing to fall through to.

---

## The finding

Alpaca's official MCP server (`alpaca-mcp-server==2.3.1`) exposes **72 tools**. **Seven of them
liquidate an account in one call, with no decision recorded anywhere** — `close_all_positions`,
`close_position`, `cancel_all_orders`, `cancel_order_by_id`, `replace_order_by_id`, and the two
exercise tools. Mizan's `BrokerAdapter` has no vocabulary for any of them: four reads and exactly one
mutation.

That boundary is re-imposed on the MCP surface three independent ways — the server is started with
fewer toolsets (**72 → 53**), a client-side allowlist is enforced **before a byte reaches the pipe**,
and a `FORBIDDEN_TOOLS` set is asserted disjoint from the allowlist **at import time** so a future edit
fails the suite rather than shipping. **42 of the 53 tools are unreachable.**

```
$ python scripts/mizan_cli.py mcp-call close_all_positions --no-credentials
DENIED  MCP tool 'close_all_positions' is not on this client's allowlist; allowed: [...]
```

Details, including the deltas found against the real API: [`docs/MCP-INTERFACE.md`](docs/MCP-INTERFACE.md).

---

## Verify it — no credentials, no network

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# the central claim: build a ledger from nothing, then re-derive every decision in it
python examples/seed_ledger.py --out ./evidence/ledger
python -m mizan.replay --ledger ./evidence/ledger --assert-identical
#   4/4 decisions reproduced identically | engine 0a6f11cb         (exit 0)

# and the 12 real governed decisions shipped here have not been altered
python -m mizan.audit.verify_chain evidence/live-ledger/tenant-a.sqlite
#   RESULT: CHAIN VERIFIED — 12 links, opened read-only
#   head      : sequence 12, 34a9ec30...  (keep it; a chain cannot detect its own truncation)

# 26 invariants, one test file per rule, named for the rule
python -m pytest -q -p no:cacheprovider tests/invariants
#   INVARIANT TOTALS: PASS=26 PENDING-IMPL=0 BLOCKING=0 NOT-RUN=0 (of 26)

# the decision path is byte-identical to the committed reference
python scripts/determinism_fingerprint.py --check determinism-reference.json
#   MATCH 0a6f11cb2626ffd2c5d061b4299305e49a631459389ac1038eea44cc81542ca2
```

CI re-checks that fingerprint on **3 operating systems × 2 Python versions** (Linux, macOS, Windows;
x86-64 and ARM), under a randomised hash seed and a hostile locale and timezone.

Do not trust the shipped ledger either — build your own:

```bash
python examples/seed_ledger.py --out ./evidence/ledger    # no credentials, no network
python -m mizan.replay --ledger ./evidence/ledger
```

Two runnable demos, both credential-free:

```bash
python examples/killer_demo.py             # reject → revise → approve → replay → policy change → kill switch → prompt injection
python examples/tradingagents_ten_lines.py # a framework-shaped agent, governed, in about ten lines
```

---

## The shape

The usual MCP trading integration is `agent → broker MCP server → venue`, which puts every control in
the agent's prompt — the one place an attacker can write to. Mizan inverts it:

```
agent  --MCP-->   MIZAN   --MCP-->   Alpaca   -->   paper venue
                    |
                    +-- deterministic risk engine, governor, authorization,
                        hash-chained ledger, replay
```

The agent is handed no tool that reaches a venue. `submit_governed_order` runs every check, appends a
chained `DecisionRecord`, and only then — if the order survived — lets it through. A refusal comes back
as machine-readable reason codes with the actual value against the threshold for each failed check, so
an agent can revise rather than guess.

Three surfaces, one dispatch — the CLI calls the same `call_tool` an MCP client reaches over stdio, so
they cannot drift apart:

```bash
python scripts/mizan_cli.py doctor              # what is in force; works with no credentials
python -m mizan.mcp --print-tools               # Mizan's own MCP server
python scripts/mizan_cli.py mcp-tools --server alpaca   # Alpaca's server, and what Mizan will not send it
```

```bash
python scripts/mizan_cli.py evaluate --symbol AAPL \
    --leg "side=buy,qty=50,limit=1.85,type=call,strike=230,expiry=2026-09-25"
```

```
MIZAN -> REJECTED
  requested        50
  authorized       0
  reason codes     HARD_REJECTION_UPHELD, OPTIONS_DELTA_LIMIT_EXCEEDED, ...
    position_limit             blocking  actual 50 vs threshold 20
    options_delta_limit        blocking  actual 840 vs threshold 500
  policy           options-conservative v1.4.0
  checks run       45
```

---

## What is enforced

- **The verdict is deterministic and an LLM cannot move it upward.** The engine is a pure function. An
  advisory model may only reduce or reject; it can never increase a size, overturn a hard rejection, or
  reach the enforcement path. Free-text model `reasoning` is recorded and is structurally unable to
  reach a check — an invariant walks the AST of the enforcement packages and fails if any of them names
  the field. With no LLM library installed at all, the verdicts are identical.
- **Missing data blocks, and never becomes a convenient zero.** Absent prices, absent buying power,
  absent greeks and absent sector each produce a refusal with a reason code. Half the real decisions in
  the shipped ledger are refusals of exactly this kind.
- **Authorization is bound to the state that justified it** — hashes of the policy, portfolio and
  market snapshot, single-use, expiring in seconds. If the book moves between the decision and the
  mutation, execution stops rather than silently resizing.
- **Paper is proven, not configured** — the client's base URL and the account's own `PA` prefix, both
  re-derived rather than cached, and re-checked immediately before every submission.
- **The kill switch is checked at the mutation boundary**, after every check has already run:

```
$ MIZAN_KILL_SWITCH=true python scripts/mizan_cli.py submit --symbol AAPL --strategy long_call \
      --leg "side=buy,qty=2,limit=1.85,type=call,strike=230,expiry=2026-09-25"
MIZAN -> APPROVED  ... authorized 2
  execution
    status         BLOCKED
    reason code    KILL_SWITCH_ACTIVE
```

---

## The live account

`5b61edf2-7440-4d4a-9c5a-186a4f262ab0` — Alpaca paper, started at $100,000.00. Real orders filled, each
submitted as one atomic `order_class=mleg` multi-leg order rather than separate single-leg orders.

**Profit and loss: −$50.55.** Not annualised, not extrapolated, not alpha — a loss over roughly two
hours on positions that were all bullish, all on one underlying, all on one expiry.
[`WRITEUP.md` §4](WRITEUP.md) says why that number is noise and reports it anyway.

The full bundle, generated by one command, is in [`evidence/pack/SUMMARY.md`](evidence/pack/SUMMARY.md).

```bash
python scripts/evidence_pack.py     # add --no-broker to skip the one section that needs credentials
python scripts/position_monitor.py  # read-only; it reports and offers you no button
```

---

## Limits

Named in full in [`WRITEUP.md` §5](WRITEUP.md). The short version:

- **No greeks on this account** — `feed=opra` returns `403 "OPRA agreement is not signed"`, so the
  greeks checks block with `GREEKS_MISSING` rather than guessing.
- **No close path, by design** — Mizan cannot cancel, replace or close anything, and the names do not
  exist in the adapter. Positions run to expiry or are closed by hand, outside this system.
- **The volatility signal has zero authority** and is off by default ([`docs/VOL-SIGNAL.md`](docs/VOL-SIGNAL.md)).
- **The M5 console and Postgres per-tenant isolation are out of scope** in this build.
- **A hash chain cannot detect truncation, or protect its own last record.** Delete the tail and the
  rest still verifies; forge the head and nothing links to it to disagree. Both are the same
  limitation, both are pinned by tests, and the mitigation is external: `verify_chain` prints the head
  and takes `--expect-head` to check it against a value you already held.
- **The suite is green:** 2690 passed, 11 skipped, 53 xfailed — the xfails name open findings by id
  rather than silencing them.

---

## Repository map

| Path | What it is |
|---|---|
| `mizan/risk/`, `mizan/governor/` | the deterministic engine — pure, no LLM |
| `mizan/contracts/` | every boundary type; Decimal-only, no binary float in the decision path |
| `mizan/audit/` | the hash-chained ledger and `python -m mizan.audit.verify_chain` |
| `mizan/replay/` | `python -m mizan.replay` |
| `mizan/mcp/` | MCP stdio client (stdlib only), the Alpaca broker, Mizan's own server |
| `mizan/adapters/` | `alpaca_paper.py` — four reads, one mutation, three paper proofs |
| `mizan/signal/` | the shadow volatility signal — advisory, default off |
| `scripts/` | `mizan_cli.py`, `evidence_pack.py`, `position_monitor.py`, `determinism_fingerprint.py`, `secret_scan.py` |
| `tests/invariants/` | 26 invariants, one file per rule |
| `policies/` | versioned, hashed policy YAML; the snapshot goes into every record |
| `docs/` | [`SUBMISSION.md`](docs/SUBMISSION.md), [`MCP-INTERFACE.md`](docs/MCP-INTERFACE.md), [`VOL-SIGNAL.md`](docs/VOL-SIGNAL.md), `API-SURFACE.md`, `adr/` |
| `ledger/` | the run's own escalations, requests, progress and learnings — kept for the same reason the audit chain is |
| `security/findings.md` | every finding raised against this build, open and closed |

### Also in this repository

`app/` is the earlier FastAPI prototype from the first commit — an equities-only portfolio governor
with its own SQLite persistence and REST reconciliation. It still has tests and they pass, but it is
superseded by `mizan/` and is **not** part of what this submission claims. Nothing in `WRITEUP.md`
depends on it.

---

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q -p no:cacheprovider tests/invariants   # 26/26, BLOCKING=0
python -m pytest -q -p no:cacheprovider tests/mcp          # 138 passed, 10 skipped (credentialed ones)
python -m ruff check .
python scripts/secret_scan.py --all
```

Credentials, when you want the sections that need them, are read from the environment and never stored
— not on an object, not in a contract, not in a record:

```dotenv
ALPACA_API_KEY=...        # or APCA_API_KEY_ID
ALPACA_SECRET_KEY=...     # or APCA_API_SECRET_KEY
ALPACA_PAPER=true
```

`.env` is gitignored. Contributor guidance is in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

MIT licensed — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE), which records what this project
interoperates with and states plainly that no third-party source is vendored here.

*Paper trading only. A governance demonstration, not investment advice, and it asserts nothing about
returns.*
