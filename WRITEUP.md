# Mizan — the one-pager

**Mizan is a governance layer that sits between an AI trading agent and a broker.** The agent
proposes; Mizan decides. Every decision is evaluated by a deterministic engine against a versioned
policy, written to an append-only hash-chained ledger, and re-derivable byte for byte from its own
record by someone who has no credentials, no network access to us, and no reason to trust us.

Paper trading only. There is no live-trading code path in this build.

---

## 1. The finding that started this

We wired Alpaca's **official** MCP server (`alpacahq/alpaca-mcp-server==2.3.1`) in as Mizan's broker
transport, as the hackathon asks. Then we started it over stdio with nothing removed and read its
`tools/list`:

```
TOOLS OFFERED WITH NO ALPACA_TOOLSETS RESTRICTION: 72
```

**Seven of those 72 can empty an account in one call, with no decision recorded anywhere:**

```
close_all_positions   close_position        cancel_all_orders     cancel_order_by_id
replace_order_by_id   exercise_options_position   do_not_exercise_options_position
```

They are ordinary tools with ordinary schemas. Any agent handed that server unmediated can liquidate
a book, and the only artifact left behind is a model transcript.

Mizan's `BrokerAdapter` protocol has **no vocabulary for any of them** — four reads and exactly one
mutation, by design. Wiring the raw server into the loop would have handed back every capability the
protocol was shaped to remove. So the boundary is re-imposed on the MCP surface in three independent
places, because a capability removed in one place and granted in another is removed nowhere:

1. **`ALPACA_TOOLSETS`** is set so the server *creates* fewer tools — **72 → 53**. No crypto, no
   watchlists, no locates, no corporate actions.
2. **A client-side allowlist**, enforced *before a byte is written to the pipe*. A denied tool is
   unreachable, not merely uncalled. The check lives at the transport, because "we don't call it" is
   still reachable by the next bug or the next helpful refactor.
3. **`FORBIDDEN_TOOLS`** names each banned tool explicitly and is asserted disjoint from the allowlist
   **at import time**. A future edit that adds `close_all_positions` fails the suite instead of
   shipping. A ban by omission is not testable.

**Net: 42 of the 53 tools are unreachable.** Eleven are allowed — nine reads and the two
order-placement tools — and they map exactly onto the protocol's four reads and one mutation.

You can watch it refuse:

```
$ python scripts/mizan_cli.py mcp-call close_all_positions --no-credentials
DENIED  MCP tool 'close_all_positions' is not on this client's allowlist; allowed: [...]
```

The same run also finds that the official server computes its base URL from a single variable,
`ALPACA_PAPER_TRADE`, which it **defaults to true** — so a parent shell setting it to `false` silently
points the broker at a non-paper venue. Mizan constructs the child environment rather than inheriting
it, and **refuses loudly** on an inherited non-paper value rather than quietly correcting it.

---

## 2. Verify the central claim in under five minutes, with no credentials

The claim is reproducibility. None of what follows needs an Alpaca key, a network connection, or
anything of ours.

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

**Build a ledger from nothing and re-derive every decision in it.** You generate the evidence and you
check it, so there is no artifact of ours to trust:

```bash
python examples/seed_ledger.py --out ./evidence/ledger   # no credentials, no network
python -m mizan.replay --ledger ./evidence/ledger
```

```
4/4 decisions reproduced identically | engine d5f5a5e8
RESULT: IDENTICAL
```

Each decision is recomputed from its own record — the same policy snapshot, the same market and
portfolio state, the same engine — with **both the verdict and its hash** required to match. The
verdict alone would not be enough: a changed reason code or a changed authorized quantity would slip
past it, and `verdict_hash` covers those too. Two of the four are refusals, which is the half that
matters — it means the *reason* a trade was refused is reproducible, not merely the fact of it.

**Verify the twelve real governed decisions shipped in this repo.** These are the actual records
behind the actual orders, in `evidence/live-ledger/`:

```bash
python -m mizan.audit.verify_chain evidence/live-ledger/tenant-a.sqlite
```

```
  links     : 12 (12 decision record(s), 0 control event(s))
  verifier  : mizan-core/0.2.0 (offline; no network, no credentials, read-only)

  RESULT: CHAIN VERIFIED
  Every audit_hash recomputes from the record's own content and every record links to
  its predecessor. 12 record(s) verified.
```

**And confirm this engine is the engine we say it is:**

```bash
python scripts/determinism_fingerprint.py --check determinism-reference.json
#   MATCH d5f5a5e8fa46093f3bd94d816853e233a2eddb7137f1563ea62f22a83245afeb

python -m pytest -q -p no:cacheprovider tests/invariants
#   INVARIANT TOTALS: PASS=26 PENDING-IMPL=0 BLOCKING=0 NOT-RUN=0 (of 26)
```

CI re-checks that fingerprint on **3 operating systems × 2 Python versions** — Linux, macOS and
Windows, x86-64 and ARM — and also under a randomised hash seed and a hostile environment (comma
decimal separator, shifted timezone), because iteration order, locale and clock are process and
machine properties that no in-process test can see.

### The one number that is not what you would expect, and why we left it that way

Replay the twelve live records under **this** engine and you get `0/12`, beside a chain that verifies
perfectly. That is not a hedge; it is the system reporting something true:

```
0/12 decisions reproduced identically | engine d5f5a5e8
ENGINE VERSION MISMATCH: the record was written by mizan-core/0.1.0 and this decision
replay ran on mizan-core/0.2.0. Hard Rule A1 guarantees an identical verdict only for the
same engine version, so a match here is not proof and a difference is not necessarily a
defect - it is a version comparison.
```

Those twelve were decided by engine `0.1.0`. This build is `0.2.0`, which adds the `expected_value`
check and therefore genuinely decides differently — the verdicts still come out `APPROVE → APPROVE`
and `REJECT → REJECT`, but `verdict_hash` covers the check set, so the hashes move. Replay them
against the engine that wrote them and they match:

```bash
# a worktree, not a checkout: evidence/ is tracked here and absent at that tag, so checking the
# tag out in place would delete the very ledger you are trying to replay.
git worktree add /tmp/mizan-0.1.0 engine-0.1.0
cp -r evidence/live-ledger /tmp/mizan-0.1.0/evidence/
cd /tmp/mizan-0.1.0 && python -m mizan.replay --ledger ./evidence/live-ledger
```

```
12/12 decisions reproduced identically | engine cc482f27
RESULT: IDENTICAL
```

We could have made that `0/12` disappear by leaving the version at `0.1.0`. That is exactly the bug
we had, and finding it was the most useful thing that happened in the last day of this build.

### The finding: our own audit trail broke, and it was nobody's fault

Adding one optional policy section made **all twelve records unreadable** — `CHAIN_INTEGRITY_ERROR`,
first bad sequence 1. The records were fine. Hashing the stored bytes matched exactly. What had
changed was the *reader*: verification re-serialised each record through the current contract before
hashing it, so every stored record silently acquired an `ev: null` it was never written with, and
every hash moved.

That is the entire promise of an audit ledger failing to an ordinary, additive, backwards-compatible
change — one that any schema will eventually make, at which point every record ever written stops
verifying and reports what looks like tampering.

Both halves are now fixed, and neither fix loosens a check:

* **A hash covers what was written, not what we would write today.** `verify_presented_hash` compares
  against the content as presented — which is the check an outside party performs with no Mizan code
  at all: take the stored JSON, drop the hash field, canonicalise, compare. Hashing a re-serialisation
  was the bug; hashing what was stored is the definition, and it is the only version we and an auditor
  can both compute. Tamper detection is unchanged — modify a stored record and the chain still fails
  at that record's own sequence.
* **An engine version is a promise about behaviour, so it must move when behaviour moves.**
  `engine-versions.json` pins each published version to the determinism fingerprint it decides with,
  and `tests/replay/test_engine_version_moves_with_behaviour.py` fails if the running engine does not
  decide the way its version promises. There are two honest ways to make that test pass: revert the
  behaviour change, or publish a new version. Re-pinning an existing one rewrites what every record
  already written under it was promised, and the test says so where someone would be tempted.

## 3. What is actually enforced

**The verdict is deterministic, and an LLM cannot move it upward.** The engine is a pure function. An
advisory model may make a decision *more* conservative — reduce, or reject — and can never increase a
size, overturn a hard rejection, or reach the enforcement path at all. Free-text model `reasoning` is
recorded and is *structurally* incapable of reaching a check: an invariant walks the AST of
`mizan/risk`, `mizan/governor`, `mizan/policy`, `mizan/authorization` and `mizan/execution` and fails
if any of them so much as names the field. Run the engine with no LLM library installed and the
verdicts are identical.

**Missing data blocks; it never becomes a convenient zero.** A portfolio that says "no positions" when
it means "I could not read the account" turns a fail-closed engine into a fail-open one. Of the twelve
real governed decisions in the shipped ledger, **six are refusals**, and they carry `GREEKS_MISSING`,
`SECTOR_DATA_MISSING` and `PORTFOLIO_STATE_MISSING` — the engine refused on real absent data rather
than inventing a number that would have let the trade through.

**Authorization is bound to the state that justified it.** An approval is not a token saying "this
trade is fine". It carries hashes of the policy, the portfolio and the market snapshot it was granted
against, it is single-use, and it expires in seconds. If the book moves between the decision and the
mutation, execution stops rather than silently resizing.

**Two invariants we have not seen elsewhere, and the defects they found:**

- **INV-25 — an enabled blocking check must be able to FAIL.** Driving all 36 implemented checks
  against a hostile battery found exactly one dead control: `duplicate_order` looped over
  `RiskContext.recent_orders`, a field no caller in `mizan/` ever populated. It passed every input,
  forever. It is now derived from the tenant's own decision chain, bounded by the policy's own
  duplicate window, and it can fail.
- **INV-26 — a check that reports `passed=True` must carry evidence.** The audit found the general
  case: when a check function returned `None`, the runner *fabricated* `passed=True` at the configured
  severity with every evidence field empty. **17 of 36 checks had that path.** A blocking PASS that
  asserts a control ran when it did not is worse than no control.

**F-31 — a `bull_call_spread` of two SHORT calls was APPROVED for 10 contracts.** A named option
strategy was validated only for its leg *count*, never for being that structure, so a naked short
passed the entire decision plane. The strictest policy this build ships did not stop it; no policy
setting reached it. That is a missing control, not a loose default. It is now refused by a
`structure_valid` check:

```
$ python scripts/mizan_cli.py evaluate --symbol SPY --strategy bull_call_spread \
    --leg "side=sell,qty=10,limit=3.10,type=call,strike=560,expiry=2026-09-25" \
    --leg "side=sell,qty=10,limit=1.70,type=call,strike=565,expiry=2026-09-25"

MIZAN -> REJECTED
  reason codes     GREEKS_MISSING, HARD_REJECTION_UPHELD, NAKED_SHORT_NOT_PERMITTED, PRICE_MISSING
    structure_valid            blocking  actual 20 vs threshold 0
```

---

## 4. The P&L, plainly

**Account `5b61edf2-7440-4d4a-9c5a-186a4f262ab0`** — Alpaca paper, started at exactly $100,000.00.
Real multi-leg orders were submitted and filled, each as one atomic `order_class=mleg` order rather
than two single-leg orders, because two single-leg orders have a window in which the short leg fills
and the long one does not — which is precisely the undefined-risk position `structure_valid` refuses
at decision time.

> **Profit and loss: −$50.55.**

That is the number. It is not annualised, not extrapolated, not scaled to a notional book, and it is
not alpha. It is a loss over roughly two hours.

The honest reading, which we would rather write than have a judge work out: the open positions were
**all bullish, all on SPY, all on one expiry** — no diversification of underlying, direction or date.
SPY ticked down. One day of paper P&L on a concentrated book is noise in either direction, and a
governance layer that made money on a Tuesday would be no more trustworthy than one that lost it.

There is a sharper version of the same admission. Mizan's own shadow volatility signal read SPY as
`regime: LOW` — realized-vol rank at the 6th percentile — while the open position **sells premium**.
By its own reading, the signal would have argued against the trade. It has **zero authority** by
design: it is advisory text, recorded in the decision and structurally unable to reach a check
(§3). So it did not stop anything, and the disagreement is logged rather than hidden.

**This submission makes no claim about returns.** Mizan is not a strategy and has no view on what
should be traded. The strategy exists so that there is something real to govern.

---

## 5. Limits, named rather than discovered

- **Option greeks are unavailable on this account.** `feed=opra` answers `HTTP 403 — "OPRA agreement
  is not signed"`. Mizan's response is to **block, not guess**: the delta/gamma/vega checks return
  `GREEKS_MISSING` and the proposal is rejected. Correct behaviour, and visible in the recorded
  decisions — but it does mean the greeks-based limits are exercised in tests rather than against
  live option data. The structural checks did govern the real orders.
- **There is no close path, by design.** Mizan cannot cancel, replace or close anything, and the names
  do not appear in the adapter. An automated close path is a second, less-reviewed way for software to
  reach a venue, and it runs precisely when things are going badly and supervision is worst. v1 governs
  what goes **on**; taking risk **off** stays with a person. The consequence is real:
  `scripts/position_monitor.py` will tell you a short strike is breached one day from expiry and offer
  you no button. That is the design working.
- **The volatility signal has zero authority** and is off by default. It computes realized volatility
  from daily bars — deliberately *not* named `iv_rank`, because this account cannot see implied
  volatility and a field name is a claim about provenance inside an audit record.
- **Sector data is unavailable from Alpaca** on any endpoint, so `sector_concentration` blocks with
  `SECTOR_DATA_MISSING` rather than assuming a sector.
- **Out of scope in this build:** the M5 console, and the Postgres per-tenant isolation (authored as
  SQL and compose, but the Docker daemon was unavailable here, so SQLite one-file-per-tenant is the
  tested isolation).
- **Six tests are red** — all of them in `tests/integration/test_the_demo_proves_the_aim.py`, which
  asserts that specific sentences appear in `CURRENT_AIM.md`; that file was rewritten for this
  submission, so the assertions no longer match its text. The rest of the suite is **2620 passed, 11
  skipped, 53 xfailed** (the xfails are named open findings with the finding id in the reason string).
- **What the evidence does not prove:** that the policy is a *good* policy. That is the customer's
  judgement, which is exactly why the policy is versioned, hashed, snapshotted into every record, and
  replayable against.

---

## 6. Where things are

| | |
|---|---|
| the engine | `mizan/risk/`, `mizan/governor/` — pure, deterministic, no LLM |
| contracts | `mizan/contracts/` — every boundary type; Decimal-only, no binary float in the decision path |
| ledger and verifier | `mizan/audit/`, `python -m mizan.audit.verify_chain` |
| decision replay | `mizan/replay/`, `python -m mizan.replay` |
| MCP client, allowlist, `FORBIDDEN_TOOLS` | `mizan/mcp/client.py`, `mizan/mcp/alpaca.py` |
| Mizan's own MCP server | `mizan/mcp/server.py`, `python -m mizan.mcp` |
| the CLI (same dispatch as the server) | `scripts/mizan_cli.py` |
| the 26 invariants | `tests/invariants/` — one file per rule, named for the rule |
| evidence bundle | `evidence/pack/SUMMARY.md`, built by `scripts/evidence_pack.py` |
| the long-form docs | `docs/SUBMISSION.md`, `docs/MCP-INTERFACE.md`, `docs/VOL-SIGNAL.md` |

---

*Paper trading only. A governance demonstration, not investment advice, and it asserts nothing about
returns.*
