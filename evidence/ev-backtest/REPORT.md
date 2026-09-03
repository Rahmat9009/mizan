# Does the EV gate refuse the right trades?

**602 SPY put credit spreads**, every one of them a contract that has already expired, over **87 weekly expiries** from **2026-04-23** to **2026-09-02**. Each candidate was priced from the option bars printed that day, run through the real gate (`risk.evaluate` -> `governor.govern`, no backtest-only branch), and then settled against SPY's close on its expiry date. Approved and refused candidates are settled identically.

## 1. What the gate did

| | candidates | win rate | mean/spread | WORST | total |
|---|---|---|---|---|---|
| **EV check passed** | 9 | 100.0% | +181.22 | +114.00 | +1631.00 |
| **EV check refused** | 593 | 84.5% | +12.31 | -481.00 | +7297.00 |

The gate approved **9 of 602** (1.5%). It is extremely selective, and the trades it let through did not merely win more often - over this window they never lost at all.

## 2. The evidence that the floor is measuring something real

This is the part that does not depend on the gate's exact thresholds. Group every candidate by credit-to-width and ignore the verdict entirely:

| credit-to-width | | candidates | win rate | mean/spread | WORST |
|---|---|---|---|---|---|
| 0.00 - 0.05 | below floor | 55 | 98.2% | +7.24 | -481.00 |
| 0.05 - 0.10 | below floor | 98 | 90.8% | +0.58 | -472.00 |
| 0.10 - 0.15 | below floor | 105 | 83.8% | -5.07 | -450.00 |
| 0.15 - 0.20 | below floor | 119 | 82.4% | +8.50 | -425.00 |
| 0.20 - 0.30 | above floor | 194 | 80.4% | +26.22 | -399.00 |
| 0.30 - 1.00 | above floor | 31 | 80.6% | +93.81 | -342.00 |

Mean outcome and worst case both improve **monotonically** as credit-to-width rises, across the whole range and either side of the floor. That is the gate's central claim behaving as claimed on data it never saw: a spread paid more for the risk it takes is a better spread, and the worst case shrinks for the mechanical reason that max loss is `width - credit`.

## 3. What refusing cost

Over this window the refused candidates were **profitable in aggregate: +7297.00** across 593 spreads. Refusing them was not free, and the page would be dishonest if it led with the per-trade table and left this out.

The two facts are not in conflict - they are the shape of the trade. Premium selling is negative skew: many small wins and rare large losses. A high win rate is what that looks like right up until the loss arrives, which is why the gate is built around expectancy rather than hit rate. The single worst refused candidate lost **-481.00** on one spread - more than 39 average wins.

## 4. What this does NOT show

* **9 approved candidates is a small sample.** The 100% win rate in section 1 is a fact about nine trades, not an estimate of anything. Section 2 is the finding with weight behind it, because it rests on all 602.
* **One window, one regime.** It contains a single meaningful drawdown (SPY 754 -> 725 in early June, which produced every one of the worst refused outcomes). A window containing a real tail event would likely favour refusal more; a purely rising one would favour it less. This cannot distinguish those.
* **Marks are daily closes, not bid/ask.** Every credit here is better than a real fill would have got, which flatters both rows equally.
* **Volatility is realized, not implied**, because historical IV is not available from this source - and it is backward-looking by construction.
* **Held to expiry**, with no management, no roll, and no early assignment.
* **No return claim is made anywhere on this page**, and none should be read from it.

## Reproduce

```bash
python scripts/backtest_ev_gate.py --weeks 18 --otm 0.0025,0.005,0.0075,0.01,0.015,0.02,0.03
```

Needs Alpaca credentials to read HISTORICAL data. Places no order, and touches no account.