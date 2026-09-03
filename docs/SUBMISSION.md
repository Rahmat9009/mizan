# Mizan — submission

**Mizan is a governance layer that sits between an AI trading agent and a broker.** The agent
proposes; Mizan decides. Every decision is evaluated by a deterministic engine against a versioned
policy, written to an append-only hash-chained ledger, and can be re-derived byte for byte from its
own record, months later, by someone who does not trust us and has no access to our infrastructure.

The one-line claim, and it is the only claim this submission makes:

> Every AI action receives the minimum authority it needs, for the shortest time it needs, under the
> exact policy and state that justified it — and Mizan can replay, byte for byte, exactly why.

It is **paper trading only**. There is no live-trading code path in this build. `ALPACA_PAPER` must
be explicitly `true` before a credential is read or a socket is opened, the SDK client's base URL is
re-checked against the paper host immediately before every submission, and the account that answers
must carry Alpaca's paper account prefix. Three independent proofs, all re-derived rather than
cached, because an adapter that merely *claims* to be in paper mode proves nothing.

**Account under test:** `5b61edf2-7440-4d4a-9c5a-186a4f262ab0` — Alpaca paper, started at exactly
$100,000.00.

### Where the numbers stood when this was written

| | |
|---|---|
| decisions governed and recorded | 11, hash-chained, one tenant |
| chain verification | **PASS** — over the ledger and over the bundle's own export |
| decision replay | **PASS** — `11/11 decisions reproduced identically \| engine cc482f27` |
| equity | 99,991.95, recorded at 2026-09-03T19:49:51Z |
| **profit and loss** | **−8.05** — a loss, over 57 minutes |

Eight dollars and five cents, down, over under an hour. That is the honest number and it is
deliberately not dressed up: see [limitation 2](#2-one-day-of-paper-pl-is-noise-and-is-reported-as-such).
Re-run `python scripts/evidence_pack.py` for the current figures; the ledger grows as the account
keeps trading, so treat every number in this table as a timestamped observation rather than a
constant.

---

## Why this is not a risk-limits library

Position limits, a kill switch and prompt logging are commodity. Everything below is built, none of
it is the pitch. What is hard, and what this repository actually is:

**The verdict is deterministic, and an LLM cannot move it upward.** The engine is a pure function.
An advisory model may make a decision *more* conservative — reduce, or reject — and can never
increase a size, overturn a hard rejection, or reach the enforcement path at all. Free-text
`reasoning` from a model is recorded and is structurally incapable of reaching a check. Run the whole
engine with no LLM library installed and the verdicts are identical; that is a pinned invariant, not
an aspiration.

**Missing data blocks, and never becomes a convenient zero.** A portfolio state that says "no
positions" when it means "I could not read the account" turns a fail-closed engine into a fail-open
one. Absent prices, absent buying power and absent greeks each produce a refusal with a reason code —
visible in the recorded decisions in the evidence pack, where a substantial fraction of the real
decisions were rejected for exactly this, carrying `GREEKS_MISSING` or `SECTOR_DATA_MISSING` rather
than a made-up number that would have let them through.

**Authorization is bound to the state that justified it.** An approval is not a token that says "this
trade is fine". It carries hashes of the policy, the portfolio and the market snapshot it was granted
against, it is single-use, and it expires in seconds. If the book moves between the decision and the
mutation, the authorization is no longer valid for the world it now faces, and execution stops rather
than silently resizing.

**The evidence outlives us.** The ledger is a plain SQLite file per tenant, append-only at the
storage layer, hash-chained. The verifier and the replay engine are shipped commands that take a file
and nothing else — no service, no network, no credentials, no Mizan account.

---

## The three artifacts

Each is a command. Each produces its own evidence. Two of the three need no credentials of any kind.

### Artifact 1 — the evidence pack

**What it is:** the whole submission in one directory — the account, every position, every order with
its `order_class` and leg count, the audit trail exported verbatim, and both proofs run and
transcribed.

```
python scripts/evidence_pack.py
```

Writes to `evidence/pack/`. Read `evidence/pack/SUMMARY.md` top to bottom; it is written to be read
by a person in a hurry, and it names its own limitations in its last section. `pack.json` is the same
content, machine-readable. Exit status is `0` when everything passed, `1` when a proof failed, and
`2` when the broker could not be read — in which case the credential-free half is still written in
full and the account section says so rather than inventing a number.

Read-only against the broker: three calls, `get_account`, `get_all_positions`, `get_orders`, and no
fourth. It borrows the adapter's own already-proven paper client rather than building a second path
to a venue.

### Artifact 2 — the hash-chained audit trail, verified offline

**What it is:** every governed decision, in order, each link committing to the one before it.

```
python -m mizan.audit.verify_chain evidence/live-ledger/tenant-a.sqlite
```

Re-derives every `audit_hash` from the record's own content, re-checks every link, and names the
first sequence that disagrees. Exit `0` verified, `1` broken, `2` unreadable — it drops straight into
a cron job or an auditor's checklist. The database is opened **read-only**: verification never writes
to the evidence it is verifying.

The evidence pack exports the same chain to JSON Lines and then runs this command **on the export as
well as on the ledger**. That closes the gap between "the ledger is intact" and "the file in this
bundle is that ledger" — the export is the same bytes the hashes were taken over, not a prettified
description of them.

### Artifact 3 — credential-free decision replay

**What it is:** the reproducibility claim, in its strongest form.

```
python -m mizan.replay --ledger ./evidence/live-ledger
```

Prints `N/N decisions reproduced identically | engine cc482f27` and exits non-zero on any mismatch.

Each decision is recomputed from its record alone — the same policy snapshot, the same market and
portfolio state, the same engine — and **both the verdict and its hash** must match. The verdict
alone would not be enough: a changed reason code or a changed authorized quantity would slip past it,
and `verdict_hash` covers those too.

No Alpaca key. No network. No access to anything of ours. Anyone handed the `.sqlite` file and this
repository can check every decision in it, which is the entire point — evidence that requires our
cooperation to check is not evidence.

### Supporting: the position monitor

```
python scripts/position_monitor.py
```

Reports each open position's unrealised P&L, days to expiry, and distance from the short strike,
grouped so a defined-risk vertical shows as the one structure it is. **It reports only.** See
[limitations](#3-there-is-no-close-path-and-that-is-deliberate) — there is no close path in Mizan and
this monitor is not a back door to one.

---

## Reproduce everything

```bash
# 1. install
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# 2. the two proofs — NO CREDENTIALS NEEDED, run these first
python -m mizan.audit.verify_chain evidence/live-ledger/tenant-a.sqlite
python -m mizan.replay --ledger ./evidence/live-ledger

# 3. the invariants: 26 Hard Rules, each with a test that fails when the rule is broken
python -m pytest -q tests/invariants

# 4. the full suite
python -m pytest -q tests/

# 5. the evidence pack (broker section needs paper credentials; the rest does not)
python scripts/evidence_pack.py

# 6. the position monitor (needs paper credentials)
python scripts/position_monitor.py
```

For steps 5 and 6, set in your environment or a local `.env`:

```dotenv
ALPACA_API_KEY=...        # or APCA_API_KEY_ID
ALPACA_SECRET_KEY=...     # or APCA_API_SECRET_KEY
ALPACA_PAPER=true
```

`ALPACA_PAPER=true` is not a default and not optional — absent is not permission, and `false` fails
closed with no live path to fall through to. `.env` is gitignored and Mizan never persists a
credential: the keys are read from your environment, handed to the SDK, and never stored on an
object, in a contract, or in a record.

If the credentials are absent, steps 5 and 6 still run. They report that the account was not read,
fall back to the account state **recorded inside the hash-chained ledger** — Alpaca's own numbers,
captured at the moment of the last governed decision, tamper-evident because they sit inside the
chain — and label every one of those numbers *recorded, not live*. Nothing is estimated or carried
forward.

### If you want to build the ledger yourself

You do not have to trust the committed one either:

```bash
python examples/seed_ledger.py --out ./evidence/ledger    # no credentials, no network
python -m mizan.replay --ledger ./evidence/ledger
```

The seeded decisions are deliberately a mix of APPROVE, REDUCE and REJECT. A replay proof over four
identical APPROVEs would demonstrate much less; a REJECT that reproduces bit-for-bit means the
*reason* a trade was refused is reproducible, not merely the fact of it.

### What the ten-line integration looks like

```bash
python examples/tradingagents_ten_lines.py    # framework-shaped agent, governed, no network
python examples/killer_demo.py                # reject → revise → approve → replay → policy change → kill switch
```

---

## Limitations

Written plainly, because a submission that hides these is worth less than one that names them.

### 1. Option greeks are unavailable on this account

Greeks require an OPRA market-data agreement this paper account does not hold. Mizan's response is to
**block, not guess**: the delta/gamma/vega checks return `GREEKS_MISSING` and the proposal is
rejected. That is the correct behaviour and it is visible in the recorded decisions — but it does
mean the greeks-based portfolio limits are exercised in tests rather than against live option data.
The defined-risk structural checks (both legs present, the long leg caps the loss, leg count, notional
cap) do run against real orders, and are what actually governed the trades in the evidence pack.

### 2. One day of paper P&L is noise, and is reported as such

The account traded for hours, not months, on a handful of small defined-risk verticals. The evidence
pack reports the profit-and-loss number and the window it was measured over **and does nothing else
with it** — it does not annualise, does not extrapolate, does not compute a risk-adjusted ratio over
a sample far too small to support one, and never calls it alpha or edge. If the number is negative it
says so, with the minus sign intact.

**This submission makes no claim about returns.** Mizan is not a strategy and does not have a view on
what should be traded. The strategy in the demo exists so that there is something real to govern. A
governance layer that made money on a Tuesday would be no more trustworthy than one that lost it.

### 3. There is no close path, and that is deliberate

Mizan cannot cancel, replace or close anything. This is a Hard Rule enforced at the abstraction
itself: `mizan.adapters.base.BrokerAdapter` has four reads and exactly one mutation, and the names
`cancel_order`, `replace_order`, `close_position` and `close_all_positions` appear nowhere in it. A
capability that cannot be named cannot be reached by a bug, a debug flag, a panicking operator or a
helpful refactor.

The reasoning: an automated close path is a second, less-reviewed way for software to reach a venue,
and it runs precisely when things are going badly and supervision is at its worst. v1 governs what
goes **on**. Taking risk **off** stays with a person.

The consequences are real and are not hidden. Positions run to expiry or are closed by hand, outside
this system. `scripts/position_monitor.py` will tell you a short strike has been breached with one
day to expiry and offer you no button — that is the design working, not a missing feature. Adding one
is not a small change; it is a change to what Mizan is.

### 4. Scope of this build

* **Paper only.** No live path exists, and none is configurable. This is a boundary, not a setting.
* **Sector data is unavailable from Alpaca** on any endpoint, so the sector-concentration check
  blocks with `SECTOR_DATA_MISSING` until a classification is supplied out of band. Again: it blocks
  rather than assuming a sector.
* **Path-dependent and aggregate state are inputs, not derivations** in this build. The engine is
  pure by design (realised-P&L path, book-level exposure across agents, per-agent budgets and the
  exchange calendar are `RiskContext` fields), and the seam for deriving them from the tenant's own
  ledger exists — but they are supplied, not yet computed from history.
* **The advisory LLM is optional and off by default.** The deterministic engine must evaluate and
  reject with no LLM installed at all, and does.

### 5. What the evidence pack does not prove

It proves that every recorded decision reproduces from its record, that the chain has not been
altered, and that real defined-risk multi-leg orders were governed and submitted to a real broker
under a real policy. It does not prove the policy is a *good* policy — that is the customer's
judgement, which is exactly why the policy is versioned, hashed, snapshotted into every record, and
replayable against.

---

## Where things are

| | |
|---|---|
| the engine | `mizan/risk/`, `mizan/governor/` — pure, deterministic, no LLM |
| contracts | `mizan/contracts/` — every boundary type; Decimal-only, no binary float in the decision path |
| ledger and verifier | `mizan/audit/`, `python -m mizan.audit.verify_chain` |
| decision replay | `mizan/replay/`, `python -m mizan.replay` |
| broker adapter | `mizan/adapters/alpaca_paper.py` — four reads, one mutation, three paper proofs |
| the 26 Hard Rules | `tests/invariants/` — one file per rule, named for the rule |
| the submission scripts | `scripts/evidence_pack.py`, `scripts/position_monitor.py` |
| their tests | `tests/evidence/` |

---

*Paper trading only. A governance demonstration, not investment advice, and it asserts nothing about
returns.*
