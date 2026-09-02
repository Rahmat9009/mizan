from __future__ import annotations

from typing import Protocol

from app.models import MarketRiskSnapshot


class MarketRiskProvider(Protocol):
    """Contract for upstream research/probability systems; no values are fabricated."""

    def get_snapshot(self, symbol: str) -> MarketRiskSnapshot: ...


class SuppliedMarketRiskProvider:
    """Validates a snapshot explicitly supplied by an upstream caller."""

    def __init__(self, snapshot: MarketRiskSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self, symbol: str) -> MarketRiskSnapshot:
        if self.snapshot.symbol != symbol:
            raise ValueError("Market-risk symbol does not match the proposal symbol.")
        return self.snapshot
