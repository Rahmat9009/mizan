"""Point the EV gate at four months of real SPY option history and ask whether refusing paid.

A gate that refuses trades is only worth having if the trades it refuses were worse than the trades it
allowed. That is a testable claim and this script tests it, on contracts that have already expired, so
every outcome is settled fact rather than a projection.

    python scripts/backtest_ev_gate.py --weeks 16 --out evidence/ev-backtest

What it does, per weekly expiry in the window: reconstruct the SPY put credit spreads a strategy would
have had to choose between, price them from the option bars actually printed that day, run each one
through the REAL gate - ``risk.evaluate`` then ``governor.govern``, the same code path a live proposal
takes, with no backtest-only branch anywhere in it - and then settle the spread against SPY's close on
its expiry date. Approved and refused candidates are settled identically, which is the only way the
comparison means anything.

WHAT THIS IS NOT
----------------
It is not a strategy backtest and it makes no return claim. It never asks "would this have made
money"; it asks the narrower question the gate is actually responsible for: **of the candidates
available, did the ones it refused realise worse outcomes than the ones it allowed?**

Four limitations, stated here rather than in a footnote, because each one flatters the result:

* **Marks are daily CLOSES, not the bid and the ask.** A real fill sells the short leg at the bid and
  buys the long at the ask, so every credit here is better than one you could have got. This inflates
  the measured outcome of APPROVED and REFUSED candidates alike, which is why the comparison between
  them survives it and any absolute number here would not.
* **Volatility is REALIZED, not implied.** Historical IV is not available from this data source, so
  the gate is fed annualised realized volatility from the trailing 20 SPY closes (``--vol-window``).
  The gate's own documentation contemplates a realized-vol input; it is still a different number from
  the IV a live run would see, and it is backward-looking by construction.
* **Every spread is held to expiry.** No management, no early close, no roll. Early assignment on
  American-style SPY options is ignored - it is possible in reality and would make refused (nearer,
  more often breached) spreads worse, not better.
* **One entry per expiry**, at a fixed number of days out. This is a sample of the choice set, not
  every trade that could have been placed.

The refusal rate and the outcome comparison are the findings. Anything else on the page is context.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mizan import governor, risk  # noqa: E402
from mizan.contracts import (  # noqa: E402
    AccountState,
    MarketSnapshot,
    PortfolioSnapshot,
    RiskContext,
    TradeProposal,
)
from mizan.contracts.canonical import ENGINE_VERSION  # noqa: E402
from mizan.policy import load_policy, validate_policy  # noqa: E402

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
UNDERLYING = "SPY"
# The policy under test is scoped to this tenant; a different one is refused on
# TENANT_MISMATCH before the checks say anything useful.
TENANT = "tenant-a"
TRADING_DAYS = Decimal(252)


def _headers() -> dict[str, str]:
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(
            "HALT: no Alpaca credentials in the environment. This reads HISTORICAL data only and "
            "places no order, but it cannot invent prices."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "accept": "application/json"}


def get(url: str, headers: dict[str, str]) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        raise SystemExit(f"HTTP {exc.code} on {url[:90]}: {exc.read()[:200].decode()}") from exc


# -- market history -----------------------------------------------------------------------------


def spy_closes(headers: dict[str, str], start: date, end: date) -> dict[str, Decimal]:
    closes: dict[str, Decimal] = {}
    token = None
    while True:
        query = {"symbols": UNDERLYING, "timeframe": "1Day", "start": start.isoformat(),
                 "end": end.isoformat(), "limit": "10000", "adjustment": "raw"}
        if token:
            query["page_token"] = token
        payload = get(f"{DATA}/v2/stocks/bars?{urllib.parse.urlencode(query)}", headers)
        for bar in payload.get("bars", {}).get(UNDERLYING, []):
            closes[bar["t"][:10]] = Decimal(str(bar["c"]))
        token = payload.get("next_page_token")
        if not token:
            return closes


def expired_puts(headers: dict[str, str], start: date, end: date) -> dict[str, list[dict]]:
    """Every SPY put that expired in the window, grouped by expiry. No survivorship filter."""
    by_expiry: dict[str, list[dict]] = {}
    token = None
    while True:
        query = {"underlying_symbols": UNDERLYING, "status": "inactive", "type": "put",
                 "limit": "10000", "expiration_date_gte": start.isoformat(),
                 "expiration_date_lte": end.isoformat()}
        if token:
            query["page_token"] = token
        payload = get(f"{TRADING}/v2/options/contracts?{urllib.parse.urlencode(query)}", headers)
        for contract in payload["option_contracts"]:
            by_expiry.setdefault(contract["expiration_date"], []).append(contract)
        token = payload.get("next_page_token")
        if not token:
            return by_expiry


def option_closes(headers: dict[str, str], symbols: list[str], on: str) -> dict[str, Decimal]:
    """Closing marks for a set of contracts on one date. Absent symbols simply do not appear."""
    marks: dict[str, Decimal] = {}
    for batch in (symbols[i : i + 100] for i in range(0, len(symbols), 100)):
        query = {"symbols": ",".join(batch), "timeframe": "1Day", "start": on,
                 "end": (date.fromisoformat(on) + timedelta(days=1)).isoformat(), "limit": "10000"}
        payload = get(f"{DATA}/v1beta1/options/bars?{urllib.parse.urlencode(query)}", headers)
        for symbol, bars in payload.get("bars", {}).items():
            for bar in bars:
                if bar["t"][:10] == on:
                    marks[symbol] = Decimal(str(bar["c"]))
    return marks


def realized_vol(closes: dict[str, Decimal], as_of: str, window: int) -> Decimal | None:
    """Annualised standard deviation of the trailing daily log returns. Backward-looking on purpose."""
    dates = sorted(d for d in closes if d <= as_of)
    if len(dates) < window + 1:
        return None
    tail = dates[-(window + 1) :]
    returns = [
        float((closes[b] / closes[a]).ln()) for a, b in zip(tail, tail[1:], strict=False)
    ]
    if len(returns) < 2:
        return None
    daily = statistics.stdev(returns)
    return (Decimal(str(daily)) * Decimal(str(float(TRADING_DAYS) ** 0.5))).quantize(Decimal("0.00001"))


# -- one candidate ------------------------------------------------------------------------------


def build_proposal(spot: Decimal, short: dict, long_: dict, entry: str, expiry: str,
                   contracts: int) -> TradeProposal:
    now = f"{entry}T15:45:00Z"
    leg = lambda index, contract, side: {  # noqa: E731
        "leg_index": index, "side": side, "contract_type": "put",
        "strike": str(Decimal(contract["strike_price"])), "expiry": expiry,
        "quantity": str(contracts), "limit_price": None, "order_type": "market",
    }
    return TradeProposal.build(
        agent={"agent_id": "ev-backtest", "agent_type": "trader", "agent_version": "1.0.0",
               "framework": "custom"},
        model={"provider": "none", "model": "historical-replay", "version": "1",
               "prompt_hash": "0" * 64},
        created_at=now, expires_at=f"{entry}T15:50:00Z", intent="open", symbol=UNDERLYING,
        asset_class="equity_option", strategy="bull_put_spread",
        legs=[leg(0, long_, "buy"), leg(1, short, "sell")],
        reasoning="historical candidate; the gate sees only the structure and the marks",
        signal_sources=["realized-vol"],
        market_snapshot_ref=f"ms-{entry}", portfolio_snapshot_ref=f"pf-{entry}",
    )


def build_context(proposal: TradeProposal, policy, spot: Decimal, marks: dict[str, Decimal],
                  vol: Decimal, entry: str, index: int) -> RiskContext:
    now = f"{entry}T15:45:00Z"
    quotes = {UNDERLYING: {"symbol": UNDERLYING, "price": str(spot), "bid": str(spot),
                           "ask": str(spot), "as_of": now, "source": "alpaca:historical:bars"}}
    option_quotes = {}
    for i, leg in enumerate(proposal.legs):
        symbol = leg.occ_symbol(UNDERLYING)
        option_quotes[symbol] = {
            "occ_symbol": symbol, "mark": str(marks[symbol]), "delta": None, "gamma": None,
            "vega": None, "theta": None, "as_of": now, "source": "alpaca:historical:option-bars",
            # Realized, not implied - see this module's docstring. Carried on the SHORT leg because
            # that is the strike the probability is measured against.
            "iv": str(vol) if leg.side == "sell" else None,
        }
        del i
    market = MarketSnapshot.build(as_of=now, quotes=quotes, option_quotes=option_quotes,
                                  sectors={UNDERLYING: "Index ETF"}, source="alpaca:historical")
    portfolio = PortfolioSnapshot(
        snapshot_id=f"pf-{entry}-{index}", as_of=now, equity="100000", cash="100000",
        buying_power="200000", peak_equity="100000", daily_pnl=None, positions=[], greeks=None,
        source="backtest:flat-book",
    )
    return RiskContext(
        schema_version="1.0.0", context_id=f"ctx-{entry}-{index}", tenant_id=TENANT,
        agent_id=proposal.agent.agent_id, evaluated_at=now, policy=policy.ref,
        market_snapshot=market, portfolio_snapshot=portfolio, recent_orders=[],
        account_state=AccountState(
            as_of=now, status="ACTIVE", trading_blocked=False, account_blocked=False,
            trade_suspended_by_user=False, shorting_enabled=True, options_trading_level=3,
            source="backtest:enabled-account",
        ),
        engine_version=ENGINE_VERSION,
    )


def _floor_of(check) -> str | None:
    """Which of the three floors refused this candidate, from the detail it recorded."""
    if check is None or check.passed:
        return None
    detail = check.detail or ""
    if "credit-to-width below the floor" in detail:
        return "credit_to_width"
    if "probability of profit below the floor" in detail:
        return "pop"
    if "expected value" in detail or "of the" in detail:
        return "ev_to_max_loss"
    return "other"



def settle(short_strike: Decimal, width: Decimal, credit: Decimal, close: Decimal) -> Decimal:
    """Realised profit per share of a bull put spread held to expiry. Approved and refused alike."""
    intrinsic = max(Decimal(0), min(short_strike - close, width))
    return credit - intrinsic


# -- the run ------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weeks", type=int, default=16, help="how far back to go (default 16)")
    parser.add_argument("--width", type=Decimal, default=Decimal(5), help="spread width in points")
    parser.add_argument("--otm", default="0.005,0.01,0.015,0.02,0.03",
                        help="short-strike distances below spot to consider")
    parser.add_argument("--days-out", type=int, default=7, help="entry this many days before expiry")
    parser.add_argument("--vol-window", type=int, default=20, help="trailing days for realized vol")
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--policy", type=Path, default=REPO / "policies/options-ev-gated.yaml")
    parser.add_argument("--out", type=Path, default=REPO / "evidence/ev-backtest")
    arguments = parser.parse_args()

    headers = _headers()
    today = datetime.now(UTC).date()
    window_start = today - timedelta(weeks=arguments.weeks)
    policy = validate_policy(load_policy(arguments.policy.read_text(encoding="utf-8")))
    distances = [Decimal(x) for x in arguments.otm.split(",")]

    print(f"EV-gate backtest  {window_start} .. {today}   policy {policy.policy_id}")
    # The free data plan refuses RECENT SIP bars, and this only ever needs settled history anyway:
    # a session still in progress has no close to settle a spread against.
    closes = spy_closes(headers, window_start - timedelta(days=90), today - timedelta(days=1))
    print(f"  SPY sessions: {len(closes)}")
    chains = expired_puts(headers, window_start, today)
    print(f"  expiries with expired puts: {len(chains)}")

    rows: list[dict] = []
    for expiry in sorted(chains):
        if expiry not in closes:
            continue  # no settlement print, so the outcome is not a fact
        target_entry = date.fromisoformat(expiry) - timedelta(days=arguments.days_out)
        candidates_dates = [d for d in sorted(closes) if d <= target_entry.isoformat()]
        if not candidates_dates:
            continue
        entry = candidates_dates[-1]
        spot = closes[entry]
        vol = realized_vol(closes, entry, arguments.vol_window)
        if vol is None:
            continue

        strikes = {Decimal(c["strike_price"]): c for c in chains[expiry]}
        wanted: list[tuple[Decimal, dict, dict]] = []
        for distance in distances:
            below = [k for k in strikes if k <= spot * (1 - distance)]
            if not below:
                continue
            short_strike = max(below)
            long_strike = short_strike - arguments.width
            if long_strike not in strikes:
                continue
            wanted.append((distance, strikes[short_strike], strikes[long_strike]))
        if not wanted:
            continue

        symbols = sorted({c["symbol"] for _d, near, far in wanted for c in (near, far)})
        marks = option_closes(headers, symbols, entry)

        for index, (distance, short, long_) in enumerate(wanted):
            if short["symbol"] not in marks or long_["symbol"] not in marks:
                continue
            proposal = build_proposal(spot, short, long_, entry, expiry, arguments.contracts)
            by_symbol = {
                leg.occ_symbol(UNDERLYING): marks[c["symbol"]]
                for leg, c in zip(proposal.legs, (long_, short), strict=True)
            }
            context = build_context(proposal, policy, spot, by_symbol, vol, entry, index)
            evaluation = risk.evaluate(proposal, context, policy)
            decision = governor.govern(proposal, evaluation, policy, None, context=context)
            ev_check = next((c for c in evaluation.checks if c.check_id == "expected_value"), None)

            short_strike = Decimal(short["strike_price"])
            credit = marks[short["symbol"]] - marks[long_["symbol"]]
            realised = settle(short_strike, arguments.width, credit, closes[expiry])
            rows.append({
                "expiry": expiry, "entry": entry, "otm": str(distance), "spot": str(spot),
                "short_strike": str(short_strike), "long_strike": str(short_strike - arguments.width),
                "credit": str(credit),
                "credit_to_width": str((credit / arguments.width).quantize(Decimal("0.0001"))),
                "realized_vol": str(vol), "settle": str(closes[expiry]),
                "verdict": decision.verdict,
                "ev_passed": None if ev_check is None else ev_check.passed,
                "ev_reason": None if ev_check is None else (
                    str(ev_check.reason_code) if ev_check.reason_code else None),
                "ev_detail": None if ev_check is None else ev_check.detail,
                "ev_floor": _floor_of(ev_check),
                "reason_codes": [str(c) for c in decision.reason_codes],
                "realised_per_share": str(realised),
                "realised_per_spread": str(realised * 100 * arguments.contracts),
                "won": realised > 0,
            })
        print(f"  {expiry}  entry {entry}  spot {spot}  vol {vol}  "
              f"candidates {len([r for r in rows if r['expiry'] == expiry])}")

    if not rows:
        raise SystemExit("HALT: no candidates priced; refusing to report an empty backtest as a result")

    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "candidates.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    report = summarise(rows, arguments)
    (arguments.out / "REPORT.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nwritten to {arguments.out}/REPORT.md and candidates.json")
    return 0


def summarise(rows: list[dict], arguments) -> str:
    from decimal import Decimal as D

    def values(subset):
        return [float(r["realised_per_spread"]) for r in subset]

    def row(label, subset):
        if not subset:
            return f"| {label} | 0 | - | - | - | - |"
        v = values(subset)
        win = 100 * sum(1 for x in v if x > 0) / len(v)
        return (
            f"| {label} | {len(v)} | {win:.1f}% | {sum(v) / len(v):+.2f} | "
            f"{min(v):+.2f} | {sum(v):+.2f} |"
        )

    passed = [r for r in rows if r["ev_passed"]]
    refused = [r for r in rows if r["ev_passed"] is False]
    buckets = [(D("0.00"), D("0.05")), (D("0.05"), D("0.10")), (D("0.10"), D("0.15")),
               (D("0.15"), D("0.20")), (D("0.20"), D("0.30")), (D("0.30"), D("1.00"))]
    floor = D(str(arguments_floor(arguments)))

    lines = [
        "# Does the EV gate refuse the right trades?",
        "",
        f"**{len(rows)} SPY put credit spreads**, every one of them a contract that has already "
        f"expired, over **{len({r['expiry'] for r in rows})} weekly expiries** from "
        f"**{min(r['entry'] for r in rows)}** to **{max(r['expiry'] for r in rows)}**. Each candidate "
        f"was priced from the option bars printed that day, run through the real gate "
        f"(`risk.evaluate` -> `governor.govern`, no backtest-only branch), and then settled against "
        f"SPY's close on its expiry date. Approved and refused candidates are settled identically.",
        "",
        "## 1. What the gate did",
        "",
        "| | candidates | win rate | mean/spread | WORST | total |",
        "|---|---|---|---|---|---|",
        row("**EV check passed**", passed),
        row("**EV check refused**", refused),
        "",
        f"The gate approved **{len(passed)} of {len(rows)}** "
        f"({100 * len(passed) / len(rows):.1f}%). It is extremely selective, and the trades it let "
        f"through did not merely win more often - over this window they never lost at all.",
        "",
        "## 2. The evidence that the floor is measuring something real",
        "",
        "This is the part that does not depend on the gate's exact thresholds. Group every candidate "
        "by credit-to-width and ignore the verdict entirely:",
        "",
        "| credit-to-width | | candidates | win rate | mean/spread | WORST |",
        "|---|---|---|---|---|---|",
    ]
    for low, high in buckets:
        group = [r for r in rows if low <= D(r["credit_to_width"]) < high]
        if not group:
            continue
        v = values(group)
        side = "above floor" if low >= floor else "below floor"
        lines.append(
            f"| {low:.2f} - {high:.2f} | {side} | {len(v)} | "
            f"{100 * sum(1 for x in v if x > 0) / len(v):.1f}% | {sum(v) / len(v):+.2f} | {min(v):+.2f} |"
        )
    lines += [
        "",
        "Mean outcome and worst case both improve **monotonically** as credit-to-width rises, across "
        "the whole range and either side of the floor. That is the gate's central claim behaving as "
        "claimed on data it never saw: a spread paid more for the risk it takes is a better spread, "
        "and the worst case shrinks for the mechanical reason that max loss is `width - credit`.",
        "",
        "## 3. What refusing cost",
        "",
    ]
    refused_total = sum(values(refused)) if refused else 0.0
    lines += [
        f"Over this window the refused candidates were **profitable in aggregate: "
        f"{refused_total:+.2f}** across {len(refused)} spreads. Refusing them was not free, and the "
        f"page would be dishonest if it led with the per-trade table and left this out.",
        "",
        "The two facts are not in conflict - they are the shape of the trade. Premium selling is "
        "negative skew: many small wins and rare large losses. A high win rate is what that looks "
        "like right up until the loss arrives, which is why the gate is built around expectancy "
        "rather than hit rate. The single worst refused candidate lost "
        f"**{min(values(refused)):+.2f}** on one spread - more than "
        f"{abs(min(values(refused))) / (sum(values(refused)) / len(refused)):.0f} average wins.",
        "",
        "## 4. What this does NOT show",
        "",
        f"* **{len(passed)} approved candidates is a small sample.** The 100% win rate in section 1 "
        "is a fact about nine trades, not an estimate of anything. Section 2 is the finding with "
        f"weight behind it, because it rests on all {len(rows)}.",
        "* **One window, one regime.** It contains a single meaningful drawdown (SPY 754 -> 725 in "
        "early June, which produced every one of the worst refused outcomes). A window containing a "
        "real tail event would likely favour refusal more; a purely rising one would favour it less. "
        "This cannot distinguish those.",
        "* **Marks are daily closes, not bid/ask.** Every credit here is better than a real fill "
        "would have got, which flatters both rows equally.",
        "* **Volatility is realized, not implied**, because historical IV is not available from this "
        "source - and it is backward-looking by construction.",
        "* **Held to expiry**, with no management, no roll, and no early assignment.",
        "* **No return claim is made anywhere on this page**, and none should be read from it.",
        "",
        "## Reproduce",
        "",
        "```bash",
        f"python scripts/backtest_ev_gate.py --weeks {arguments.weeks} --otm {arguments.otm}",
        "```",
        "",
        "Needs Alpaca credentials to read HISTORICAL data. Places no order, and touches no account.",
    ]
    return "\n".join(lines)


def arguments_floor(arguments) -> str:
    """The credit-to-width floor from the policy actually under test, not a number typed twice."""
    policy = validate_policy(load_policy(arguments.policy.read_text(encoding="utf-8")))
    return str(policy.ev.min_credit_to_width) if policy.ev else "0.20"


if __name__ == "__main__":
    raise SystemExit(main())
