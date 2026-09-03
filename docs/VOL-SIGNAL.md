# VOL-SIGNAL — a realized-volatility signal, in shadow

**Status: SHADOW ONLY. Default OFF. Zero authority.**
Owner lane: `vol-signal-shadow`. Paths: `mizan/signal/`, `tests/signal/`, this file.

---

## 1. Why this exists, and why it does not trade

The interesting version of this feature is an options-premium signal: sell premium when implied
volatility is rich, stand down when it is cheap. That version cannot be built here tonight, and saying
so precisely matters more than shipping something that looks like it.

**What the data tier actually returns.** The options-data feed (`feed=opra`) answers `HTTP 403 — "OPRA
agreement is not signed"`. There are no greeks and no implied volatility on any endpoint available to
this account. A live implied-volatility signal therefore cannot be computed, let alone validated,
against real data.

So this lane builds the part that *can* be proven tonight:

* the **computation** is real, from real price bars, deterministic and Decimal-only;
* the **seam** by which a market signal reaches a governed decision is built and tested;
* the **proof** that it changes nothing is the deliverable, not a caveat on it.

The signal computes, logs and appears as advisory metadata. It never causes, sizes, approves, delays or
blocks an order.

## 2. What is computed

Everything comes from **daily price bars**. No greeks are needed and none are used.

| Field               | Definition                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------ |
| `realized_vol`      | Standard deviation (sample, n−1) of the last **21** close-to-close **log** returns, annualised by √252 |
| `realized_vol_rank` | Percentile of that value within the last **252** such observations, in **[0, 100]**                 |
| `atr`               | **ATR(14)**, Wilder: seeded with the mean of the first 14 true ranges, then `((n−1)·prev + tr) / n` |
| `regime`            | `HIGH` if rank ≥ 50 · `MID` if 25 ≤ rank < 50 · `LOW` if rank < 25                                  |

`method` is recorded alongside the numbers (`rv21-close-to-close-annualized-252/pctrank-252/atr14-wilder`)
so a stored reading stays interpretable if the windows are ever retuned.

Minimum input: **81 daily bars** (21 for one observation + 60 observations for a meaningful percentile).
Fewer bars raises `InsufficientBars`. The signal refuses; it never extrapolates.

### 2.1 The name is a correctness requirement

It is called **`realized_vol_rank`**, not "IV rank".

This project cannot see implied volatility (§1). A field named `iv_rank` would tell every future reader
of a stored decision that an implied number was an input, when the input was a price series. That is a
false statement about provenance in an audit record, which is the one kind of error this system exists
to prevent. `tests/signal/test_vol_signal.py::test_nothing_in_the_package_claims_to_be_implied_volatility`
scans the package's identifiers, keys and non-docstring strings and fails on `iv_rank` / `implied_vol`.

Realized volatility is a *proxy* for implied volatility, and a lagging one: it says what the market has
done, not what it is charging for what might happen. Every rendered line says so in words.

## 3. Shape of the code

```
mizan/signal/
  bars.py      Bar (DecimalStr OHLCV) + venue payload parsing
  vol.py       compute_vol_signal(bars) -> VolSignal          <- the pure function
  source.py    fetch_daily_bars(...)                          <- the only socket in the lane
  shadow.py    shadow_enabled()                               <- SIGNAL_SHADOW, default OFF
  advisory.py  VolSignalAdvisoryProvider                      <- the seam
```

```python
from mizan.signal import compute_vol_signal, fetch_daily_bars

bars    = fetch_daily_bars("SPY")                    # network, once, at the edge
reading = compute_vol_signal(bars, symbol="SPY")     # pure, Decimal-only, deterministic
```

Fetching and evaluating are separate on purpose. `compute_vol_signal` is a pure function of a bar
series: no clock, no environment, no socket, so the same bars reproduce the same reading forever and a
stored series can be re-evaluated with no credentials at all.

### 3.1 Decimal only, and where the float would have got in

Hard Rule A6. The venue returns OHLC as JSON **numbers**, so `json.loads` would hand back binary floats
and a price nobody quoted would enter the record silently. One line prevents it:

```python
json.loads(body, parse_float=Decimal, parse_int=Decimal)
```

From there every number is a `Decimal` evaluated in `DECIMAL_CONTEXT` (precision 28, trapping
`InvalidOperation` / `DivisionByZero` / `Overflow`) and leaves as a normalised `DecimalStr`.
`Decimal.ln` and `Context.sqrt` are correctly-rounded decimal operations, so there is no `math` import
anywhere in the package. `tests/signal/test_vol_signal.py::test_no_float_appears_anywhere_in_the_signal_package`
is an AST scan mirroring the adapter's own (`tests/adapters/test_alpaca_paper.py`).

### 3.2 Credentials

Read from the process environment (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`, falling back to
`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`). Never written to a file, a log line or a reading. Absent
credentials raise `MissingCredentials`, a named failure the caller can degrade on.

The host is the read-only historical market-data host. No order endpoint is reachable from this module,
and nothing in this lane can submit, cancel or modify anything.

## 4. The seam: how a signal reaches the record without gaining authority

`VolSignalAdvisoryProvider` is an `AdvisoryProvider` (API-SURFACE §3.4). It wraps another provider — or
none — and changes **exactly one field** of that provider's opinion: `reasoning`. The recommendation,
the recommended quantity, the availability flag and the authority ceiling are passed through unchanged.

**No contract changed.** `AdvisoryOpinion.reasoning` already exists and is already the right field:

* **Invariant 17** scans `mizan/risk`, `mizan/governor`, `mizan/policy`, `mizan/authorization` and
  `mizan/execution` for any attribute access, subscript, `getattr` or string constant naming
  `reasoning`. A future check that tried to read the signal would fail that invariant rather than
  quietly begin trading on it. The door is bolted from the enforcement side.
* **`verdict_hash`** is derived from the verdict, the reason codes, the authorized quantity, the
  authorized legs and the evaluation id (`mizan.contracts.canonical.verdict_hash_for`). `reasoning` is
  not an input, so the text cannot move a verdict even by accident.

The result: the reading is **recorded**, **replayable**, and **structurally incapable of reaching a
check**.

### 4.1 Failure is inert

`advise()` never raises. If the reading is missing or cannot be rendered, the wrapped provider's opinion
is returned untouched — which is the same opinion the loop would have had if this package were not
installed. With `SIGNAL_SHADOW` unset (the default) the wrapper returns the wrapped opinion verbatim and
every code path here is dead weight.

## 5. The proof

`tests/signal/test_shadow_proof.py`, over a battery of 6 decisions producing a mix of verdicts
(APPROVE, REJECT, and a scripted-REDUCE run for the third arbitration row — a shadow proof over one
repeated APPROVE would prove very little, because the interesting case is a decision that was close to
going the other way):

1. **The flag changes nothing.** Same inputs, same provider, run at `SIGNAL_SHADOW=1` and
   `SIGNAL_SHADOW=0`: identical verdict sequence, identical verdict hashes, identical authorized
   quantities, identical reason codes. Canonical JSON of each whole decision — with `decision_id` and
   the advisory `reasoning` removed — is equal. And the signal is asserted to be *present* in the shadow
   arm and *absent* in the other, so the test cannot pass by the signal quietly not running.
2. **The wrapper is transparent.** Wrapping a provider is indistinguishable, verdict-wise, from not
   wrapping it; installing the wrapper over *no* provider is indistinguishable from having no provider.
   The signal cannot change an outcome merely by being present in the loop.
3. **It survives replay.** A record carrying the signal in its advisory reasoning replays to an
   identical verdict and an identical `verdict_hash`.

Plus determinism (same bars → byte-identical reading and digest; page order and duplicate bars cannot
change it), the no-float scan, the naming scan, and refusal on short, unordered or incoherent series.

## 6. Enabling it

```
SIGNAL_SHADOW=1     # advisory text on
                    # unset / 0 / false / no / off  ->  OFF (the default)
```

Turning it on adds one line of text to an advisory opinion. There is no setting that gives this signal
authority, because no such code path exists.

## 7. Real-versus-contract deltas

* **No implied volatility, no greeks.** `feed=opra` returns `HTTP 403 — "OPRA agreement is not signed"`.
  Any future contract field named for implied volatility would be unfillable from this entitlement.
* **Bars arrive as JSON numbers, not strings.** The rest of the venue's API quotes money as *text*
  (`"equity": "100000.10"`), and the adapter layer is built on that. The market-data bars endpoint does
  not: `o/h/l/c/vw` are JSON numbers. Any code that decodes this endpoint with a plain `json.loads` has
  binary floats in it. This is the delta most likely to bite another lane.
* **`v` (volume) is an integer and `n`/`vw` are present but unused here.** Only OHLC and the timestamp
  are inputs to the reading.
* **Bar timestamps are RFC 3339 with a session-open time**, not bare dates; the calendar date is taken
  from the first ten characters and duplicate days collapse to one bar.

## 8. What would have to be true before this could size anything

Not tonight, and not without each of these:

1. A real implied-volatility source (an OPRA entitlement, or another vendor), because a realized-vol
   proxy is not what an options-premium decision should be priced from.
2. Out-of-sample validation that the signal predicts anything at all.
3. A policy-level expression of the rule, with its own reason code, so the signal acts through
   `mizan/policy` and `mizan/risk` where it is deterministic, evidenced and replayable — never through
   the advisory layer, which is downward-only by design and is not where sizing logic belongs.
4. Invariant coverage for that new check under INV-25 and INV-26.

Until then it stays where it is: computed, recorded, and deciding nothing.
