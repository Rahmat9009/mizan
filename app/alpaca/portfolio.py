from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.alpaca.client import ReadOnlyAlpacaClientProtocol, create_read_only_alpaca_client
from app.models import PortfolioPosition, PortfolioSnapshot


class AlpacaPortfolioError(RuntimeError):
    """A complete, trustworthy portfolio snapshot could not be produced."""


class AlpacaPortfolioProvider:
    """Maps Alpaca paper-account reads into broker-neutral domain models."""

    def __init__(self, client: ReadOnlyAlpacaClientProtocol | None = None) -> None:
        self.client = client or create_read_only_alpaca_client()

    def get_snapshot(self) -> PortfolioSnapshot:
        try:
            account = self.client.get_account()
            raw_positions = self.client.get_all_positions()
            if not isinstance(raw_positions, (list, tuple)):
                raise AlpacaPortfolioError("Alpaca positions response is not a list.")

            equity = self._required_number(account, "equity")
            cash = self._required_number(account, "cash")
            buying_power = self._required_number(account, "buying_power")
            daily_pnl_pct = self._daily_pnl_pct(account, equity)
            positions = [self._map_position(position) for position in raw_positions]

            # Gross market value is deliberately used for deterministic concentration
            # checks so a short position cannot appear as negative/safe concentration.
            current_positions = {
                position.symbol: abs(position.market_value) for position in positions
            }
            return PortfolioSnapshot(
                equity=equity,
                cash=cash,
                buying_power=buying_power,
                daily_pnl_pct=daily_pnl_pct,
                current_positions=current_positions,
                positions=positions,
                source="ALPACA_PAPER",
            )
        except AlpacaPortfolioError:
            raise
        except ValidationError as exc:
            raise AlpacaPortfolioError("Alpaca data violates the portfolio domain model.") from exc
        except Exception as exc:
            raise AlpacaPortfolioError(
                f"Alpaca read-only portfolio request failed ({type(exc).__name__})."
            ) from exc

    @classmethod
    def _map_position(cls, value: Any) -> PortfolioPosition:
        symbol_value = cls._field(value, "symbol")
        if not isinstance(symbol_value, str) or not symbol_value.strip():
            raise AlpacaPortfolioError("Alpaca position has no valid symbol.")
        symbol = symbol_value.strip().upper()

        quantity = cls._required_number(value, "qty", context=f"position {symbol}")
        side = cls._field(value, "side")
        side_value = getattr(side, "value", side)
        if isinstance(side_value, str) and side_value.casefold() == "short" and quantity > 0:
            quantity = -quantity

        return PortfolioPosition(
            symbol=symbol,
            quantity=quantity,
            market_value=cls._required_number(
                value, "market_value", context=f"position {symbol}"
            ),
            current_price=cls._optional_number(
                value, "current_price", context=f"position {symbol}"
            ),
            unrealized_pl=cls._optional_number(
                value, "unrealized_pl", context=f"position {symbol}"
            ),
            unrealized_pl_pct=cls._optional_number(
                value, "unrealized_plpc", context=f"position {symbol}"
            ),
        )

    @classmethod
    def _daily_pnl_pct(cls, account: Any, equity: float) -> float | None:
        last_equity_raw = cls._field(account, "last_equity")
        if last_equity_raw is None or (
            isinstance(last_equity_raw, str) and not last_equity_raw.strip()
        ):
            return None
        last_equity = cls._parse_number(last_equity_raw, "account.last_equity")
        if last_equity <= 0:
            return None
        return (equity - last_equity) / last_equity

    @classmethod
    def _required_number(
        cls,
        value: Any,
        field: str,
        *,
        context: str = "account",
    ) -> float:
        raw = cls._field(value, field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise AlpacaPortfolioError(f"{context}.{field} is unavailable.")
        return cls._parse_number(raw, f"{context}.{field}")

    @classmethod
    def _optional_number(
        cls,
        value: Any,
        field: str,
        *,
        context: str,
    ) -> float | None:
        raw = cls._field(value, field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        return cls._parse_number(raw, f"{context}.{field}")

    @staticmethod
    def _parse_number(value: Any, label: str) -> float:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AlpacaPortfolioError(f"{label} is not a valid number.") from exc
        if not parsed.is_finite():
            raise AlpacaPortfolioError(f"{label} must be finite.")
        return float(parsed)

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)
