"""``python -m mizan.signal`` - fetch bars, print the reading, exit.

One command, no arguments needed, so the reading can be produced and pasted into evidence without
anyone hand-rolling a fetch. It prints canonical JSON on stdout and nothing else; credentials are read
from the environment and never printed, logged or written anywhere.

Exit codes: 0 a reading, 2 no credentials, 3 the venue refused or sent something unusable, 4 too few
bars for the configured windows.

This command reads market data. It cannot place, size, approve or block an order - there is no code
path from here to a broker.
"""

from __future__ import annotations

import argparse
import sys

from mizan.signal.bars import BarDataError
from mizan.signal.source import DEFAULT_SYMBOL, MarketDataUnavailable, MissingCredentials, fetch_daily_bars
from mizan.signal.vol import InsufficientBars, compute_vol_signal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mizan.signal", description=__doc__)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--start", default=None, help="ISO date, e.g. 2024-01-01 (default: two years back)")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    start = args.start or _two_years_back()
    try:
        bars = fetch_daily_bars(args.symbol, start=start, timeout_seconds=args.timeout)
    except MissingCredentials as exc:
        print(f"no market-data credentials: {exc}", file=sys.stderr)
        return 2
    except (MarketDataUnavailable, BarDataError) as exc:
        print(f"market data unavailable: {exc}", file=sys.stderr)
        return 3

    try:
        reading = compute_vol_signal(bars, symbol=args.symbol)
    except InsufficientBars as exc:
        print(f"not enough bars: {exc}", file=sys.stderr)
        return 4

    print(reading.canonical())
    return 0


def _two_years_back() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(tz=UTC) - timedelta(days=760)).date().isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
