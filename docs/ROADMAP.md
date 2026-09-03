# ROADMAP — what Mizan does not do yet

Everything on this page is **NEXT, not done**. It is written in the same register as the rest of the
repository: a gap named precisely is worth more than a gap implied by silence, and a governance
product that is vague about its own boundary has already failed at the thing it sells.

Each item says what exists today, what is missing, what it would take, and — where it matters — what
goes wrong if you assume it is there. Nothing here is a promise about a date.

For what *is* built and how to verify it, see [`EVIDENCE.md`](../EVIDENCE.md). For the limitations of
the current submission as a whole, see [`docs/SUBMISSION.md`](SUBMISSION.md).

---

## Ranked by what would hurt most if you assumed it existed

| # | Gap | Status today | If you assume it exists |
|---|---|---|---|
| 1 | [F-27 — an adjusted OCC root is redacted](#1-f-27--an-adjusted-occ-root-is-destroyed-by-redaction) | OPEN, pinned `xfail` | Total governance outage for the tenant |
| 2 | [Hard auto-armed halt](#2-there-is-no-hard-auto-armed-halt) | Not built | You think losses stop trading. They only shrink it |
| 3 | [Kill-switch shared state](#3-the-kill-switch-is-not-distributed) | Refuses the unsafe shape | You think the switch stops the fleet |
| 4 | [Position monitor exits](#4-no-exit-management-no-50-of-max-profit-rule) | Reports only | You think winners are taken off |
| 5 | [Illiquid-option rejection](#5-illiquid-option-rejection-is-not-wired-to-data) | Check exists, no data, off | You think a 40-wide market is refused |
| 6 | [Event blackout](#6-event-blackout-is-not-wired-to-a-calendar) | Check exists, no data, off | You think earnings are avoided |
| 7 | [IV-rank floor](#7-there-is-no-iv-rank-floor-and-there-cannot-be-one-on-this-data-tier) | Impossible on this data tier | You think premium is sold when it is rich |
| 8 | [Paired-trade accounting](#8-paired-trade-accounting) | Not built | You misread the P&L of a spread |

---

## 1. F-27 — an adjusted OCC root is destroyed by redaction

**The most serious open item in the repository, and the only one that can take a tenant down
completely.**

*What happens.* `mizan/contracts/canonical.py` redacts values that look like an Alpaca key id. The
pattern carries a negative lookahead so that OCC option symbols are exempt, but the lookahead body is
`[A-Z]{1,6}\d{6}[CP]\d{8}` — it recognises only a **purely alphabetic** OCC root. After a corporate
action the OCC assigns an **adjusted** root with a numeric suffix: `AKAM1`, `AKAM2`, `PKG1`, `AKRO1`,
`PKOH1`. Those are valid under the contract's own `OCC_SYMBOL_PATTERN`
(`^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$`), they are copied verbatim from the broker into
`Position.occ_symbol`, and they still match the key-id pattern `(?:PK|AK)[A-Z0-9]{16,}`. So redaction
writes `[REDACTED]` where an `OccSymbol` belongs, and `DecisionRecord.build` refuses the record.

*Why it is not merely a bug.* The portfolio snapshot sits inside `risk_context` on **every** decision
record. One held adjusted-option position on any `AK*`/`PK*` underlying stops the tenant recording
**any** decision — equity decisions included. `Mizan.evaluate` raises and nothing is chained. It fails
closed, so nothing leaks and no wrong record is written, but the governance layer stops governing, and
the trigger is ordinary broker data with no attacker anywhere near it.

*State.* Raised as F-27 (HIGH) in `security/findings.md`, requested as REQ-27 in `ledger/requests.md`.
Regression tests are committed and currently marked `xfail(strict=False)` in
`tests/security/test_sweep7_redaction.py`:

* `test_f27_an_adjusted_occ_symbol_must_survive_redaction` (5 roots)
* `test_f27_one_adjusted_option_position_must_not_stop_the_tenant_recording_anything`

Two sibling tests stay green either way and pin the shape of the failure:
`test_f27_the_failure_is_closed_not_silent` and
`test_f27_an_adjusted_occ_root_is_a_valid_contract_value`.

*The fix.* One line: make the lookahead body the contract's own OCC pattern body,
`[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}`, so the exemption and the contract cannot drift apart, and remove
the two `xfail` markers in the same change. It is small, it is owned by the contracts lane, and it is
deliberately **not** being done under a deadline by whoever noticed it.

```bash
python -m pytest -q tests/security/test_sweep7_redaction.py -k f27 -rxX
```

---

## 2. There is no hard, auto-armed halt

**Today's ladder reduces size. It does not stop.**

What exists:

* `drawdown_size_scaling` (`mizan/risk/checks.py`) scales an order down as drawdown deepens, from the
  `path.size_scaling_by_drawdown` steps in the policy. In `policies/institutional.yaml` those steps
  are `0.05 → ×1`, `0.10 → ×0.5`, `0.15 → ×0.25`. **No step is `×0`.** The ladder gets small; it never
  reaches a stop.
* `drawdown_limit` does block outright above `portfolio.max_drawdown_pct`, but it is a *portfolio
  drawdown from peak equity*, not a daily loss stop, and it needs a peak-equity figure in the
  portfolio snapshot to compute at all.
* `response_level_gate` **does** halt at level 4–5 — and `context.response_level` is an **input**.
  Nothing in this build derives it, arms it, or escalates it. A human sets it.
* Neither policy used in the live run (`options-conservative`, `options-defined-risk`) carries a
  `path:` section at all, so the ladder did not even run for the recorded decisions.

What is missing: a **daily realised-loss stop that arms itself**. Cross a loss threshold for the
session and the tenant stops opening risk until a person clears it, without anyone having to be
watching. That requires realised-P&L-to-date derived from the tenant's own ledger (see item 8), a
session boundary from the exchange calendar, and a control event written to the chain when it arms so
the halt is itself auditable.

Until then, state it plainly: **a bad day makes Mizan trade smaller. It does not make Mizan stop.**

---

## 3. The kill switch is not distributed

What exists, and it is not nothing: `assert_kill_switch_covers_every_worker`
(`mizan/execution/__init__.py`) **refuses to boot** a multi-worker deployment behind a process-local
kill switch. If `create_app` sees more than one configured worker and the switch does not declare
`shared_state = True`, construction fails. That closes the worst failure mode — the one where an
operator trips the switch, gets `200 {"active": true}`, stops one worker and leaves the other N−1
trading, with the control *reporting success while not working*.

What is missing: **the shared switch itself.** Refusing the unsafe shape is not the same as making the
control distributed. `InMemoryKillSwitch` is process-local by its own docstring. `EnvKillSwitch`
re-reads `MIZAN_KILL_SWITCH` on every call, which fixes staleness within a host but is not shared
across hosts, and it has no `activate`/`deactivate`, so the API's control route cannot flip it.

Next: a store-backed switch — a row in the tenant's own database, read on every `is_active()`, written
by the control route, and recorded as a `kill_switch_activated` control event in the chain. Raised as
REQ-24 in `ledger/requests.md`.

---

## 4. No exit management, no 50%-of-max-profit rule

`scripts/position_monitor.py` reports unrealised P&L, days to expiry and distance from the short
strike, grouped so a defined-risk vertical reads as the one structure it is. **It reports. There is no
close path anywhere in Mizan**, by design — `mizan.adapters.base.BrokerAdapter` has four reads and
exactly one mutation, and the names `cancel_order`, `replace_order`, `close_position` and
`close_all_positions` appear nowhere in it.

So the conventional premium-selling discipline — *take the spread off at 50% of maximum profit, or at
21 days to expiry, whichever comes first* — **does not exist here**. Positions run to expiry or are
closed by hand, outside the system. The monitor will tell you the short strike has been breached with
one day left and offer you no button.

That boundary is deliberate and is argued for in `docs/SUBMISSION.md` §3. It is on this roadmap
because the *consequence* is real, not because the decision is regretted. Adding an automated exit is
not a feature increment; it is a change to what Mizan is, and it would need its own governed decision
path — a close is an order, and an ungoverned close is exactly the hole this design refuses to open.

The credible next step is not a close button. It is **the monitor proposing a close as a governed
proposal**, evaluated by the same engine against the same policy and recorded in the same chain, with
a human authorising the mutation.

---

## 5. Illiquid-option rejection is not wired to data

The engine already has the checks. `LiquidityPolicy` carries `max_pct_of_adv`,
`max_option_spread_pct`, `min_option_open_interest` and `max_estimated_impact_bps`, and
`option_liquidity` and `liquidity_adv` are implemented checks with reason codes.

What is missing is **the data and the policy that turns them on**. Neither policy used in the live run
carries a `liquidity:` section, so neither check ran for any recorded decision. Feeding them needs
average daily volume, option open interest and a live bid-ask spread per contract, sourced and
snapshotted into the market state so that a refusal is replayable from the record — which is the whole
bar for a Mizan check.

Consequence today: **a spread quoted 0.05 bid / 0.45 ask is not refused for being illiquid.** The
notional, quantity, leg-count, structure and concentration limits still apply; the width of the market
does not.

---

## 6. Event blackout is not wired to a calendar

Same shape as item 5. `TimePolicy` carries `earnings_blackout_days_before`,
`earnings_blackout_days_after`, `macro_event_blackout_minutes`, `no_trade_first_minutes` and
`no_trade_last_minutes`; `time_blackout` and `session_window` are implemented checks with
`EARNINGS_BLACKOUT`, `MACRO_EVENT_BLACKOUT` and `SESSION_WINDOW_RESTRICTED` reason codes.

What is missing is an **earnings and macro-event calendar**, snapshotted per decision so the blackout
is replayable. Alpaca does not supply one on the endpoints available here, so it has to come from
outside — and, consistent with everything else in this engine, an *absent* calendar must block rather
than default to "no event today". A blackout check that silently passes when it cannot see the
calendar is worse than no check, because it reports safety it does not have.

Consequence today: **Mizan will govern a trade into an earnings print without noticing the print.**

---

## 7. There is no IV-rank floor, and there cannot be one on this data tier

This is a data-access boundary, not an omission of work.

The options-data feed available to this account answers `HTTP 403 — "OPRA agreement is not signed"`.
There are no greeks and no implied volatility on any endpoint reachable here. An implied-volatility
rank therefore cannot be computed, and — more importantly — **cannot be validated** against anything.

What was built instead is `mizan/signal/`: a **realized**-volatility signal from daily price bars,
deterministic and Decimal-only, running in shadow with zero authority. It is deliberately **not**
called `iv_rank`; a field with that name in a stored decision would be a false statement about
provenance in an audit record, and
`tests/signal/test_vol_signal.py::test_nothing_in_the_package_claims_to_be_implied_volatility` scans
the package and fails on `iv_rank` / `implied_vol`. Realized volatility is a lagging proxy: it says
what the market has done, not what it is charging for what might happen. See `docs/VOL-SIGNAL.md`.

Next, in order: an OPRA agreement; then IV rank as a first-class snapshot field with its own
provenance; then an `ev`/premium floor expressed in policy. Not before — a premium-richness floor
computed from a proxy, and named as if it were the real thing, is the kind of quiet lie this project
exists to make impossible.

---

## 8. Paired-trade accounting

The ledger records **decisions**. It does not record **trades** as opened-and-closed pairs.

Missing, concretely:

* no linkage from an opening order to the closing order (or expiry, or assignment) that ended it;
* no realised P&L per structure, so a two-leg vertical's result has to be inferred from four position
  rows and their fills;
* no win rate, no average winner or loser, no holding period — and therefore no honest basis for any
  performance statement at all;
* `RiskContext.path_state` (realised-P&L path, consecutive losses) and `aggregate_state` are
  **inputs** in this build, not derivations. The seam for computing them from the tenant's own ledger
  exists; the derivation does not.

This is the prerequisite for item 2 (an auto-armed daily stop needs realised P&L to date) and for any
claim about returns whatsoever. It is why the submission reports a raw equity delta over a stated
window and refuses to compute a ratio over it.

---

## Not on this roadmap, on purpose

* **A live-trading path.** `ALPACA_PAPER=true` is not a default and not optional, and there is no live
  code path to enable. That is a boundary, not a backlog item.
* **Cancel or replace.** Same reason as item 4. Adding them is a change to what Mizan is, not a
  feature.
* **A better strategy.** Mizan is not a strategy and has no view on what should be traded. The
  strategy in the demo exists so that there is something real to govern.

---

*Paper trading only. A governance demonstration, not investment advice, and it asserts nothing about
returns.*
