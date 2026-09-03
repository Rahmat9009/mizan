# The expected-value gate

`expected_value` is a blocking risk check. It asks one question about a defined-risk credit spread:

> Does this trade expect to make money, and is the answer robust to how badly we can estimate it?

If the answer is no, or the answer is unknowable from the recorded inputs, the order is REFUSED and the
arithmetic that refused it is written into the decision record.

**A recorded refusal is the deliverable.** This check exists to say no. A gate that has never said no is
indistinguishable from a gate that cannot.

---

## 1. The floors were fixed BEFORE the gate was run

This is the load-bearing claim of this document, so it is stated first and plainly.

The three floors in section 4 were chosen from first principles and written into this file **before**
`expected_value` was executed against any live proposal. They were not moved afterwards, in either
direction. Tuning a floor until a trade passes — or until it fails — is the exact failure this project
exists to prevent, and it would invalidate every number below.

Two honest qualifications, because the claim is only worth as much as its caveats:

1. **Prior knowledge existed.** The lane brief stated in advance that the open position sits at roughly
   11.4% credit-to-width, around 0.8 sigma from the short strike, in a LOW realized-vol regime. That
   number could not be un-known while choosing a floor on credit-to-width.

2. **The mitigation is structural, not a promise.** Two things defend the result:

   - Each floor is set at the **loosest** of the standard candidate values, never the strictest
     (section 4 names the candidates it rejected and why). A floor chosen to fail a trade would be set
     tight; these are set slack.
   - **The verdict on the live position does not depend on the floors at all.** Section 6 shows the
     expected value of that spread is *negative* on its own arithmetic. No non-negative EV floor can
     approve a negative EV. The refusal survives setting `min_credit_to_width` and `min_pop` to zero.

   The second point is the real answer to "did you tune it?". The floor-independent result is the one to
   check.

---

## 2. What is computed, and from what

Everything is `Decimal` (Hard Rule A6 / INV-15). No binary float appears anywhere in this path — not in
the square root, not in the normal CDF, not in the intermediate products.

Nothing is fetched. The check is a pure function of `(proposal, context, policy)`, so a replay a year
from now recomputes the same refusal from the record alone (Hard Rule A1).

Prices come from the market snapshot's option **marks**, never from `leg.limit_price` (findings F-1/F-2).
The limit price is what the agent asked for; the mark is what the market says. Valuing an EV gate off the
agent's own number would let the agent choose its own verdict.

### 2.1 Credit-to-width

For a two-leg vertical, per share of the underlying:

```
width  = |strike_short − strike_long|
credit = mark(short leg) − mark(long leg)
credit_to_width  r = credit / width
max_loss           = width − credit          (per share; × 100 per contract)
```

`credit_to_width` is the single most diagnostic number for a credit spread, for a reason that is not
obvious until it is written down (section 3).

### 2.2 POP (probability of profit) — an approximation, and named as one

POP is estimated from the distance to the short strike measured in standard deviations of the
underlying over the remaining life of the trade:

```
sigma_period = spot × vol_annualized × sqrt(days_to_expiry / 365)
z            = |spot − strike_short| / sigma_period
POP          ≈ Φ(z)
```

`Φ` is the standard normal CDF, evaluated in `Decimal` by the Abramowitz & Stegun 26.2.17 rational
approximation (stated absolute error < 7.5e-8, which is four orders of magnitude smaller than the
estimation error discussed below).

**This is an approximation and the record says so.** Its limits, all of which push the same way:

- **The distribution is wrong.** Equity returns are not normal. They are fat-tailed and left-skewed, so
  a normal CDF *overstates* the probability that a strike is not breached. The error is largest exactly
  where a credit spread lives: in the tail. POP as computed here is an **upper bound** on the truth, not
  a point estimate.
- **It is terminal-only.** Φ(z) is the probability of finishing on the right side at expiry. It ignores
  the path — a spread breached at day 10 and recovering by day 30 is scored as a win here. For a
  position that may be managed, assigned early, or margin-called mid-life, that is optimistic again.
- **Drift is assumed zero.** No risk-free rate, no carry, no skew adjustment.
- **The volatility input is backward-looking.** Realized or implied vol as of the snapshot is not the
  vol that will be realized over the trade's life. In a LOW regime it is the least reliable it ever is
  (section 5).
- **It is not calibrated.** Nothing here has been back-tested against realized outcomes. It is a
  statement about a model, not a measured hit rate.

Every one of those limits biases POP **upward**, which biases EV **upward**. A gate whose approximation
errs toward approving is a gate whose refusals can be trusted more than its passes. That asymmetry is
deliberate: this check's refusals are the product.

### 2.3 EV

Per share, then per spread:

```
EV_per_share  = POP × credit − (1 − POP) × (width − credit)
EV_per_spread = EV_per_share × 100
EV_to_max_loss = EV_per_share / (width − credit)
```

---

## 3. Why credit-to-width is the whole game

Substituting `r = credit / width` into the EV expression and dividing through by `width`:

```
EV / width = POP × r − (1 − POP) × (1 − r)
           = POP − (1 − r)
```

That identity is the reason this check is built the way it is.

Fair pricing means `credit = width × P(max loss)`, so **`credit_to_width` IS the market's own implied
probability that the spread LOSES**, and `1 − credit_to_width` the probability it wins. A vertical
credit spread priced at that probability has an expected value of **exactly zero** — that is what
"fairly priced" means, and it falls straight out of the algebra above when `POP = 1 − r`.

Three consequences follow, and they are the whole design:

1. **There is no edge in the structure, only in the disagreement.** `EV / width = POP − (1 − r)` is your
   probability estimate minus the market's. Selling a credit spread is not a way to make money; it is a
   bet that your probability is better than the price's. If you have no independent probability
   estimate, your expected value is zero minus costs, which is negative. This is why a missing
   volatility input is a **blocking** failure and not a shrug (section 5).

2. **The floor on `r` is a floor on how much of the verdict rests on the least reliable input.** At
   `r = 0.114` the trade needs `POP > 0.886` to break even. At `r = 0.20` it needs `POP > 0.80`; at
   `r = 0.33`, `POP > 0.67`. Our POP is a normal approximation on backward-looking vol — it is not
   credible to three decimal places, and it is biased high. A trade that requires POP to be right at
   0.886 is a trade whose verdict is decided by the error bar, not by the estimate.

3. **The payoff asymmetry is the ruin geometry.** At `r = 0.114` you risk 0.886 of the width to make
   0.114 of it — **7.8 losses' worth of wins to cover one loss**. The sequence, not just the mean,
   decides survival. This is the same logic that already caps `kelly_fraction_cap` at 0.5 and motivates
   `absorbing_barrier` elsewhere in this policy set: an expectation computed over an ensemble is not the
   expectation experienced by one account that can go to zero.

---

## 4. The three floors, and the reasoning for each

Each floor measures something the other two do not. A trade must clear all three.

### 4.1 `min_credit_to_width = 0.20`

**What it measures:** the shape of the payoff, and therefore the error budget.

**Reasoning.** From section 3, `r` fixes the POP a trade must achieve to break even (`1 − r`) and fixes
the loss:win ratio (`(1 − r)/r`). At `r = 0.20`: break-even POP is 0.80, loss:win is 4:1. That is the
point at which the required POP is still inside the range a realized-vol normal approximation can
defend — a 3-to-5 point error in a POP near 0.80 changes EV by 3-5% of the width, which is material but
not decisive. Near 0.886 the same error *is* decisive, and the gate would be reporting the noise.

Fixed costs argue the same way from a different direction. Per-contract commissions and bid/ask
crossing on two legs are roughly constant in dollars; as a *share of the credit* they grow as `r`
shrinks. On a $5-wide spread, $0.05/share of round-trip friction is 1% of the width — which at
`r = 0.114` is a tenth of the entire theoretical EV budget, and at `r = 0.33` is a thirtieth.

**Candidates considered and rejected:**

| Candidate | Source | Why not |
|---|---|---|
| **0.333** | The classic practitioner rule for 30-45 DTE verticals ("collect a third of the width"). | Defensible, and the one a discretionary trader would use. **Rejected as too strict for a first floor**: it would refuse a large share of legitimately-priced spreads, and a floor that refuses everything teaches nothing. |
| **0.25** | The common relaxed form of the same rule. | Also defensible. Rejected for the same reason, one notch weaker. |
| **0.20** | **CHOSEN.** | The loosest value at which the break-even POP (0.80) stays inside the range this estimator can defend. Deliberately the slackest of the three candidates, so that a refusal cannot be attributed to a strict floor. |
| 0.10 | Would admit the live position. | Rejected: break-even POP 0.90 and a 9:1 loss:win ratio put the entire verdict inside the estimator's error bar. Selecting it *because* it admits a specific open trade is precisely the forbidden move. |

### 4.2 `min_pop = 0.55`

**What it measures:** whether the trade is the kind of trade it says it is.

**Reasoning.** EV alone does not constrain this. A position can carry positive EV at POP 0.30 if the
payoff is large enough — but that is a lottery ticket, not a credit spread. A credit spread's entire
premise is "I am paid a small, capped amount to accept a large, capped, *unlikely* loss." If the
independent POP estimate is at or below a coin flip, the premise is false: the trade is a directional
bet with capped upside and a much larger capped downside, and it should be argued for on those terms
under a different check, not smuggled through as premium selling.

0.55 is set **barely above a coin flip** on purpose. It is not a quality bar — 0.55 is a bad credit
spread. It is an identity check. A tighter value (0.70, 0.80) would be defensible as a quality bar but
would duplicate what `min_ev_to_max_loss` already prices, and duplicated floors are how a gate becomes
arbitrary.

### 4.3 `min_ev_to_max_loss = 0.05`

**What it measures:** the edge itself, normalised by the capital actually at risk.

**Reasoning.** `EV / max_loss` is the expected return per occurrence on the money that can actually be
lost. It is the only one of the three that is a return number, and it is the one the other two exist to
protect from measurement error.

The floor answers "below what level is the *sign* of EV not knowable?" — not "what is attractive?".
Given that POP is biased high (section 2.2) and that friction is not modelled in the arithmetic at all,
an EV below ~5% of capital at risk is inside the combined error of the estimate. It is not a small edge;
it is an unmeasurable one. Above 5% the sign survives a reasonable haircut for both.

**Candidates considered and rejected:** 0.10 and 0.20 are the values a portfolio manager would actually
require to allocate. Both rejected: this is a **floor**, the level below which a trade is definitely not
worth doing, not the level at which it becomes attractive. A floor set at the attractiveness bar refuses
sound trades and invites exactly the pressure to loosen it that this document exists to resist.

### 4.4 What is NOT a floor

These are fail-closed rules, not thresholds. They take no number and so cannot be shopped:

- **No option marks for a leg** → blocking (`PRICE_MISSING`). Never a zero, never a skip.
- **No volatility input** → blocking (see section 5).
- **No underlying spot, or no computable days-to-expiry** → blocking.
- **`credit ≤ 0`** → not a credit spread; out of this check's scope, recorded as such with the numbers.
- **Not a two-leg vertical** → out of scope, recorded with the leg count. `structure_valid` is the check
  that guarantees defined risk; this one prices it. Multi-leg structures (condors, butterflies) are a
  genuine gap and are named as such in section 7 rather than approximated.
- **A closing order** → never gated. A control that blocks an exit strands the position it exists to
  protect, which is the control causing the harm it was built to prevent.

---

## 5. Volatility, regime, and why absence blocks

The POP estimate needs a volatility for the underlying over the remaining life of the trade. The check
resolves it in one documented order and never guesses:

1. The **short leg's `OptionQuote.iv`** in the market snapshot, annualized.
2. Nothing else. If it is absent, the check **blocks**.

Blocking is not a formality here, and section 3 is why: without an independent probability estimate,
the expected value of a credit spread is exactly zero by construction, and negative once costs are
subtracted. "We could not estimate the probability" and "the trade has no edge" are, for this
instrument, the same statement. E2 ("unknown risk is not safe") and the arithmetic agree.

**Regime.** `mizan/signal` is imported defensively — it is a shadow signal with no authority, and its
absence must not break the engine. Where a regime reading is available it is recorded as evidence, and
it matters for one reason worth stating: a credit spread is a **short-volatility** position. Selling
premium while realized vol sits at the bottom of its own range means being paid the least for the same
distance, at the moment when a backward-looking vol input is *most* likely to understate the vol that
will actually be realized. The POP estimate is biased high exactly when the payoff is thinnest. That is
not a threshold; it is a reason to distrust a marginal pass in a LOW regime, and it is recorded so a
reader can apply that distrust.

**Known gap:** `RiskContext` is a frozen contract and carries no volatility or regime field. On a data
tier without an OPRA agreement the option snapshots carry no IV (see `policies/options-defined-risk.yaml`
— `feed=opra` returns HTTP 403), so on that tier this check blocks on missing volatility for every
credit spread. That is correct fail-closed behaviour and it is also a real limitation. A request for a
`VolState` on `RiskContext` — carrying `realized_vol`, `realized_vol_rank` and `regime` — is filed in
`ledger/requests.md`. Until it is granted, the vol path is exercised only where the broker supplies IV.

---

## 6. Result on the live position

*This section was written after running the gate. Nothing above it was changed afterwards.*

See the run report for the recorded figures. The structure of the finding:

- `credit_to_width ≈ 0.114` implies a **market-implied probability of loss of 0.886**, so the trade
  needs `POP > 0.886` merely to break even.
- The independent estimate at ~0.8 sigma to the short strike is `Φ(0.8) ≈ 0.788`.
- `EV / width = POP − (1 − r) = 0.788 − 0.886 ≈ −0.098`. **Negative.**
- `EV / max_loss ≈ −0.098 / 0.886 ≈ −0.11` — the trade expects to lose about **11% of the capital it
  puts at risk**, per occurrence.

The verdict is **REJECT**, and it is floor-independent: negative EV is refused by the EV floor at any
non-negative setting, and by `min_credit_to_width` at any setting above 0.114. Setting every floor in
section 4 to zero does not approve this trade.

This is the honest result, and it is the deliverable. The gate was built to be able to say no; on the
first real position it was pointed at, it said no, and the record carries the arithmetic that says why.

---

## 7. What this check does not do

Stated so that nobody reads more assurance into a passing result than it carries.

- It does **not** model friction. Commissions, bid/ask crossing and slippage are outside the arithmetic.
  Real EV is below the number this check computes, always.
- It does **not** handle multi-leg structures. Iron condors, butterflies and calendars are out of scope
  and are recorded as out of scope, not approximated.
- It does **not** model early assignment, pin risk, or dividend-driven exercise. `assignment_risk` and
  `pin_risk` are separate, and currently deferred.
- It does **not** calibrate. POP is a model output that has never been compared against realized
  outcomes. `PathState.realized_expectancy` exists for that comparison and this check does not yet read it.
- It is **not** a sizing rule. It refuses or it permits; `risk_per_trade` and the Kelly cap size.
- A **pass is weak evidence; a refusal is strong evidence.** Every approximation in section 2.2 biases
  the estimate toward approving. Read a pass as "no disqualifying arithmetic was found", never as
  "this trade is good".

---

## 8. Configuration

The check reads the optional `ev` policy section. A tenant that has not configured `ev` does not get a
blocking check it never asked for — `expected_value` is simply not enabled, exactly like `trade`,
`liquidity` or `account`.

```yaml
ev:
  min_credit_to_width: "0.20"
  min_pop: "0.55"
  min_ev_to_max_loss: "0.05"

checks:
  expected_value: {enabled: true, severity: blocking}
```

`policies/options-ev-gated.yaml` is the shipped example.

Reason codes are drawn from the frozen catalogue in `contracts/reason_codes.json`; none were invented.
`REWARD_RISK_BELOW_MINIMUM` is the nearest honest existing code for "the reward-to-risk arithmetic of
this trade does not clear its floor", and it is used with the exact figures in `threshold` and `actual`
so the record is unambiguous about which floor failed. A request for a dedicated
`EXPECTED_VALUE_BELOW_FLOOR` code is filed in `ledger/requests.md`.
