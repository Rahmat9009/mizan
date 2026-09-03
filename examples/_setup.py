"""Shared scaffolding for the examples: a paper broker with plausible snapshots, and one import path fix.

Nothing here is part of Mizan. It stands in for the pieces a real deployment already has - a broker
connection and a market data source - so that an example can be run with ``python examples/<name>.py``
from a checkout, with no credentials, no network and no configuration.

Every number below is illustrative test data for a governance demonstration. Nothing here is a
recommendation, and no return figure appears anywhere in these examples (B5/B6).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mizan.adapters import MockBroker  # noqa: E402
from mizan.contracts import (  # noqa: E402
    AgentIdentity,
    MarketSnapshot,
    ModelIdentity,
    PortfolioSnapshot,
    format_ts,
)

#: A fixed instant, so every run of an example prints the same thing (A1 is the point of the product).
DEMO_NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)
DEMO_AS_OF = format_ts(DEMO_NOW - timedelta(seconds=5))

AGENT = AgentIdentity(
    agent_id="tradingagents-trader-01",
    agent_type="trader",
    agent_version="0.4.2",
    framework="tradingagents",
)
MODEL = ModelIdentity(
    provider="featherless", model="qwen3-30b", version="2026-08", prompt_hash="0" * 64
)


def market_snapshot() -> MarketSnapshot:
    """What the broker's data feed would return: a two-sided quote and an option chain entry."""
    # snapshot_id is content-derived (REQ-34); build() computes it.
    return MarketSnapshot.build(
        **{
            "as_of": DEMO_AS_OF,
            "quotes": {
                "AAPL": {
                    "symbol": "AAPL",
                    "price": "228.5",
                    "bid": "228.45",
                    "ask": "228.55",
                    "as_of": DEMO_AS_OF,
                    "source": "demo:quotes",
                    "adv": "55000000",
                    "spread_pct": "0.0004",
                },
                "MSFT": {
                    "symbol": "MSFT",
                    "price": "412.1",
                    "bid": "412",
                    "ask": "412.2",
                    "as_of": DEMO_AS_OF,
                    "source": "demo:quotes",
                    "adv": "21000000",
                    "spread_pct": "0.0005",
                },
            },
            "option_quotes": {
                "AAPL260925C00230000": {
                    "occ_symbol": "AAPL260925C00230000",
                    "mark": "1.85",
                    "delta": "0.168",
                    "gamma": "0.021",
                    "vega": "0.142",
                    "theta": "-0.061",
                    "as_of": DEMO_AS_OF,
                    "source": "demo:options",
                    "open_interest": 4200,
                    "spread_pct": "0.027",
                    "iv": "0.284",
                }
            },
            "sectors": {"AAPL": "Technology", "MSFT": "Technology"},
            "source": "demo",
        }
    )


def portfolio_snapshot() -> PortfolioSnapshot:
    """What the broker's account endpoint would return."""
    return PortfolioSnapshot.model_validate(
        {
            "snapshot_id": "demo-pf-1",
            "as_of": DEMO_AS_OF,
            "equity": "100000",
            "cash": "79395",
            "buying_power": "158790",
            "peak_equity": "105000",
            "daily_pnl": "-250",
            "positions": [
                {
                    "symbol": "MSFT",
                    "asset_class": "equity",
                    "quantity": "50",
                    "market_value": "20605",
                    "sector": "Technology",
                    "occ_symbol": None,
                    "delta": "50",
                    "gamma": "0",
                    "vega": "0",
                }
            ],
            "greeks": {"delta": "50", "gamma": "0", "vega": "0"},
            "source": "demo:account",
        }
    )


def demo_broker() -> MockBroker:
    """A paper broker that never opens a socket. A real deployment passes ``AlpacaPaperBroker``."""
    return MockBroker(portfolio_snapshot=portfolio_snapshot(), market_snapshot=market_snapshot())


def clock() -> datetime:
    return DEMO_NOW


def rule(title: str) -> None:
    print()
    print(title)
    print("-" * max(len(title), 60))
