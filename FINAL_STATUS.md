# FINAL STATUS — frozen 2026-09-04

Everything below was verified against commit **`f2be3ac`**, engine **`mizan-core/0.3.0`**
(fingerprint `0a6f11cb`), on Python 3.12.4 / Windows 11. Every command on this page was run before it
was printed here.

---

## Read this first

**No trade was placed, closed, rolled or modified at the freeze.** That was a decision, not an
omission. The open positions expire **2026-09-10**, five days after the freeze, so the number below
is a *mark* and not an outcome — and in the 30 minutes of market time between the open (13:30Z) and
the freeze (14:00Z), every available action lowers it: opening anything crosses the bid-ask and marks
negative immediately, and closing only locks the loss in. There is also no close path in this build
by design (Hard Rule B4).

**The expected-value gate refuses the position this account is holding.** The put spread was entered
at a credit-to-width of `0.114`, which is the market's own implied probability of loss of `0.886`,
against a POP estimate of `0.788` — an expected value of roughly `-0.098` per unit of width. That is
a floor-independent REJECT. The gate was added after the position was opened and it was not tuned to
produce this answer; it produces it about our own live money.

---

## 1. The account

| | |
|---|---|
| account | `5b61edf2-7440-4d4a-9c5a-186a4f262ab0` (Alpaca **paper**, `PA`-prefixed, ACTIVE) |
| starting equity | `100,000.00` |
| final equity | `99,949.45` |
| **P&L** | **`-50.55`** (`-0.051%`) |
| window | 2026-09-03 → 2026-09-04 |

Not annualised, not extrapolated, not alpha. One day of paper marks on two defined-risk spreads,
which is mostly noise in either direction. The loss is not bid-ask cost: SPY fell and all of the
positions were bullish.

Open at freeze (max loss fixed at construction, capped by the long leg of each spread):

| contract | qty | unrealized |
|---|---|---|
| `SPY260910C00774000` / `C00775000` | +1 / −1 | `-21.00 / +11.00` |
| `SPY260910P00760000` / `P00765000` | +10 / −10 | `-30.00 / -10.00` |

## 2. Orders — all five atomic, all governed

Every order is `order_class=mleg`: a spread reaches the venue as ONE order or not at all, because two
single-leg orders have a window that holds a naked leg — the exact undefined-risk position
`structure_valid` refuses at decision time.

| status | broker order id | qty |
|---|---|---|
| filled | `302a8c37-e6c1-4794-b80f-c30bcdf3e33e` | 1 |
| filled | `9ff8436d-55ec-46f1-b15c-e03d19bbbd6b` | 10 |
| expired unfilled | `3869a51a-e26a-4d21-8a74-21a3dfba6e80` | 1 |
| expired unfilled | `b8a4d7a6-3fca-42a6-96ea-589407f6571c` | 5 |
| expired unfilled | `cc6f8f62-119a-4fcf-b120-68993c075561` | 5 |

The three expirations are mid-priced spreads that simply never filled. They are listed because
leaving them out would make the fill rate look better than it was.

## 3. Gate state

```
2708 passed, 11 skipped, 42 xfailed          0 failed
INVARIANT TOTALS: PASS=26 PENDING-IMPL=0 BLOCKING=0 NOT-RUN=0 (of 26)
ruff .............................. All checks passed
secret-scan (all) ................. clean; 366 files
secret-scan (history) ............. clean; 51 commits
replay (credential-free) .......... 4/4 IDENTICAL
verify-chain (12 live records) .... CHAIN VERIFIED
```

The 42 `xfail`s are **named open findings carrying their finding id in the reason string**, not
silenced failures. Six were addressed in the final session: F-27, F-28, F-30 and F-33 closed by fixes, half of F-32
closed by making the over-ask visible in the record, and F-29 found to have been closed already by
REQ-34 - verified rather than assumed.

## 4. Reproduce it — no credentials, no network, no trust in us

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# Build a ledger from nothing, then re-derive every decision in it.
# You generate the evidence AND check it, so there is no artifact of ours to trust.
python examples/seed_ledger.py --out ./evidence/ledger
python -m mizan.replay --ledger ./evidence/ledger --assert-identical
#   4/4 decisions reproduced identically | engine 0a6f11cb        (exit 0)

# The 12 real governed decisions shipped in this repo have not been altered.
python -m mizan.audit.verify_chain evidence/live-ledger/tenant-a.sqlite
#   RESULT: CHAIN VERIFIED — 12 links, opened read-only
#   head: sequence 12, 34a9ec303577a4c63a8001a009b35a115f9da9ffd1a157761f0ad05ed5ae4af8

# 26 invariants, one file per rule, named for the rule.
python -m pytest -q -p no:cacheprovider tests/invariants
#   INVARIANT TOTALS: PASS=26 PENDING-IMPL=0 BLOCKING=0 NOT-RUN=0 (of 26)

# This engine decides the way its version promises.
python scripts/determinism_fingerprint.py --check determinism-reference.json
#   MATCH 0a6f11cb2626ffd2c5d061b4299305e49a631459389ac1038eea44cc81542ca2
```

Two that need an Alpaca **paper** key, and place no order:

```bash
python scripts/demo_transcript.py --out evidence     # ALL SECTIONS PASSED
python scripts/backtest_ev_gate.py --weeks 18        # historical data only
```

### Why the 12 live records replay as `0/12` here

They were decided by engine `0.1.0`; this build is `0.3.0`, which adds the `expected_value` check and
changed how four capital checks treat a short. The verdicts still reproduce (`APPROVE → APPROVE`,
`REJECT → REJECT`); `verdict_hash` covers the check set, so the hashes move. The tool reports this as
an **engine-version comparison, not an integrity failure**, because those two demand opposite
responses and a tool that says the same thing for both cries wolf about fraud.

Against the engine that wrote them, they match exactly:

```bash
git worktree add /tmp/mizan-0.1.0 engine-0.1.0
cp -r evidence/live-ledger /tmp/mizan-0.1.0/evidence/
cd /tmp/mizan-0.1.0 && python -m mizan.replay --ledger ./evidence/live-ledger
#   12/12 decisions reproduced identically | engine cc482f27
```

## 5. Known limitations, stated rather than discovered

* **A hash chain cannot detect its own truncation, nor protect its own last record.** Delete the tail
  and the rest still verifies; forge the head and nothing links to it to disagree. Both are the same
  limitation — the head is the one position no internal structure can defend. Mitigation is external:
  `verify_chain` prints the head and takes `--expect-head` / `--expect-length`. 16 probes in
  `tests/audit/test_chain_stress.py` pin all of it, including the limitations themselves.
* **`ADVISORY_CLAMPED` cannot fire as a reason code** (F-32 half-open). An advisory's over-ask is now
  recorded in the decision, but making the reason code fire needs the pre-clamp quantity in a
  structured contract field. A test asserts it does not fire, and will fail the day someone fixes it.
* **Option greeks are unavailable on this account** (`OPRA agreement is not signed`), so the
  greeks-based checks block with `GREEKS_MISSING` rather than guessing. Correct behaviour, and it
  means those limits were exercised in tests rather than against live option data.
* **Sector data is unavailable from Alpaca**, so `sector_concentration` blocks with
  `SECTOR_DATA_MISSING` rather than assuming a sector.
* **F-34 (health-probe rate limiter) was dropped at freeze**, unstarted. Nothing is half-built.
* **What none of this proves:** that the policy is a *good* policy. That is the customer's judgement,
  which is exactly why the policy is versioned, hashed, snapshotted into every record, and replayable
  against.

## 6. Where to look

| | |
|---|---|
| the one-pager | [`WRITEUP.md`](WRITEUP.md) |
| every claim + the command that proves it | [`EVIDENCE.md`](EVIDENCE.md) |
| does the gate refuse the right trades? | [`evidence/ev-backtest/REPORT.md`](evidence/ev-backtest/REPORT.md) |
| what this build does NOT do | [`docs/ROADMAP.md`](docs/ROADMAP.md) |
| licence | [`LICENSE`](LICENSE) — MIT |

Paper trading only. There is no live-trading code path in this build, and `ALPACA_PAPER` is a
deployment boundary rather than a flag: any value but `true` is refused at startup.
