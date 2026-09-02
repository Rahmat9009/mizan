from __future__ import annotations

import sys

from app.alpaca import AlpacaConfigurationError, AlpacaPortfolioError, AlpacaPortfolioProvider


def money(value: float | None) -> str:
    return "Unavailable" if value is None else f"${value:,.2f}"


def main() -> int:
    print("Mode: ALPACA PAPER / READ ONLY")
    print("Execution: DISABLED")
    try:
        snapshot = AlpacaPortfolioProvider().get_snapshot()
    except (AlpacaConfigurationError, AlpacaPortfolioError) as exc:
        print(f"Portfolio retrieval failed safely: {exc}")
        return 1

    print("\nAccount")
    print(f"Equity: {money(snapshot.equity)}")
    print(f"Cash: {money(snapshot.cash)}")
    print(f"Buying Power: {money(snapshot.buying_power)}")

    print("\nPositions")
    if not snapshot.positions:
        print("None")
    for position in snapshot.positions:
        print(
            f"{position.symbol} qty={position.quantity:g} "
            f"market_value={money(position.market_value)} "
            f"current_price={money(position.current_price)} "
            f"unrealized_pl={money(position.unrealized_pl)} "
            f"unrealized_pl_pct="
            + (
                "Unavailable"
                if position.unrealized_pl_pct is None
                else f"{position.unrealized_pl_pct:.2%}"
            )
        )

    print("\nDaily P&L")
    print(
        "Unavailable (deterministic risk will block)"
        if snapshot.daily_pnl_pct is None
        else f"{snapshot.daily_pnl_pct:.2%}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
