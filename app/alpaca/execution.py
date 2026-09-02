from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.alpaca.client import resolve_alpaca_paper_credentials
from app.execution.models import (
    BrokerOrder,
    ExecutionAsset,
    IntendedPaperOrder,
    MarketClockSnapshot,
)
from app.models import Side


class AlpacaExecutionError(RuntimeError):
    """A paper-broker operation failed without exposing provider secrets."""


class AlpacaAssetNotFoundError(AlpacaExecutionError):
    pass


class AlpacaOrderNotFoundError(AlpacaExecutionError):
    pass


class PaperExecutionAlpacaAdapter:
    """The only module allowed to invoke Alpaca's order mutation API."""

    __slots__ = ("__sdk_client",)

    def __init__(self, sdk_client: Any) -> None:
        raw_base_url = getattr(sdk_client, "_base_url", "")
        actual_base_url = str(getattr(raw_base_url, "value", raw_base_url)).rstrip("/")
        expected_base_url = BaseURL.TRADING_PAPER.value.rstrip("/")
        if actual_base_url != expected_base_url:
            raise AlpacaExecutionError(
                "Execution adapter cannot prove that the SDK client targets Alpaca paper."
            )
        self.__sdk_client = sdk_client

    @classmethod
    def from_environment(cls) -> "PaperExecutionAlpacaAdapter":
        api_key, secret_key = resolve_alpaca_paper_credentials()
        try:
            sdk_client = TradingClient(api_key, secret_key, paper=True)
        except Exception as exc:
            raise AlpacaExecutionError(
                f"Alpaca paper SDK initialization failed ({type(exc).__name__})."
            ) from exc
        return cls(sdk_client)

    @property
    def paper_mode_verified(self) -> bool:
        return True

    # Read capabilities used for immediate portfolio revalidation.
    def get_account(self) -> Any:
        return self.__sdk_client.get_account()

    def get_all_positions(self) -> Any:
        return self.__sdk_client.get_all_positions()

    def get_clock(self) -> MarketClockSnapshot:
        try:
            raw = self.__sdk_client.get_clock()
            return MarketClockSnapshot(
                timestamp=self._required_field(raw, "timestamp"),
                is_open=self._required_bool(raw, "is_open"),
                next_open=self._required_field(raw, "next_open"),
                next_close=self._required_field(raw, "next_close"),
            )
        except AlpacaExecutionError:
            raise
        except Exception as exc:
            raise self._provider_error("market clock lookup", exc) from exc

    def get_asset(self, symbol: str) -> ExecutionAsset:
        try:
            raw = self.__sdk_client.get_asset(symbol)
            return ExecutionAsset(
                symbol=str(self._required_field(raw, "symbol")).upper(),
                asset_class=self._enum_text(self._required_field(raw, "asset_class")),
                status=self._enum_text(self._required_field(raw, "status")),
                tradable=self._required_bool(raw, "tradable"),
            )
        except APIError as exc:
            if self._status_code(exc) == 404:
                raise AlpacaAssetNotFoundError(f"Asset {symbol} is not known to Alpaca.") from exc
            raise self._provider_error("asset lookup", exc) from exc
        except AlpacaExecutionError:
            raise
        except Exception as exc:
            raise self._provider_error("asset lookup", exc) from exc

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            raw = self.__sdk_client.get_order_by_client_id(client_order_id)
            return self._map_order(raw)
        except APIError as exc:
            if self._status_code(exc) == 404:
                return None
            raise self._provider_error("order lookup", exc) from exc
        except Exception as exc:
            raise self._provider_error("order lookup", exc) from exc

    def get_order_by_id(self, alpaca_order_id: str) -> BrokerOrder:
        try:
            raw = self.__sdk_client.get_order_by_id(alpaca_order_id)
            return self._map_order(raw)
        except APIError as exc:
            if self._status_code(exc) == 404:
                raise AlpacaOrderNotFoundError("Alpaca PAPER order was not found.") from exc
            raise self._provider_error("order lookup", exc) from exc
        except Exception as exc:
            raise self._provider_error("order lookup", exc) from exc

    def submit_market_order(self, intended: IntendedPaperOrder) -> BrokerOrder:
        """Submit exactly one simple DAY market order to a verified paper client."""

        order_side = OrderSide.BUY if intended.side == Side.BUY else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=intended.symbol,
            qty=intended.quantity,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            extended_hours=False,
            client_order_id=intended.client_order_id,
        )
        try:
            raw = self.__sdk_client.submit_order(order_data=request)
            return self._map_order(raw)
        except Exception as exc:
            raise self._provider_error("order submission", exc) from exc

    @classmethod
    def _map_order(cls, raw: Any) -> BrokerOrder:
        return BrokerOrder(
            alpaca_order_id=str(cls._required_field(raw, "id")),
            client_order_id=str(cls._required_field(raw, "client_order_id")),
            symbol=str(cls._required_field(raw, "symbol")).upper(),
            side=Side(cls._enum_text(cls._required_field(raw, "side")).upper()),
            quantity=cls._required_number(raw, "qty"),
            status=cls._enum_text(cls._required_field(raw, "status")),
            submitted_at=cls._required_field(raw, "submitted_at"),
            filled_at=cls._field(raw, "filled_at"),
            filled_quantity=cls._optional_number(raw, "filled_qty"),
            filled_avg_price=cls._optional_number(raw, "filled_avg_price"),
        )

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    @classmethod
    def _required_field(cls, value: Any, name: str) -> Any:
        field = cls._field(value, name)
        if field is None or (isinstance(field, str) and not field.strip()):
            raise AlpacaExecutionError(f"Alpaca response is missing {name}.")
        return field

    @classmethod
    def _required_bool(cls, value: Any, name: str) -> bool:
        field = cls._required_field(value, name)
        if not isinstance(field, bool):
            raise AlpacaExecutionError(f"Alpaca response field {name} is not boolean.")
        return field

    @classmethod
    def _required_number(cls, value: Any, name: str) -> float:
        field = cls._required_field(value, name)
        return cls._parse_number(field, name)

    @classmethod
    def _optional_number(cls, value: Any, name: str) -> float | None:
        field = cls._field(value, name)
        if field is None or (isinstance(field, str) and not field.strip()):
            return None
        return cls._parse_number(field, name)

    @staticmethod
    def _parse_number(value: Any, name: str) -> float:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AlpacaExecutionError(f"Alpaca response field {name} is not numeric.") from exc
        if not parsed.is_finite():
            raise AlpacaExecutionError(f"Alpaca response field {name} must be finite.")
        return float(parsed)

    @staticmethod
    def _enum_text(value: Any) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        try:
            value = getattr(exc, "status_code", None)
            return value if isinstance(value, int) else None
        except Exception:
            return None

    @classmethod
    def _provider_error(cls, operation: str, exc: Exception) -> AlpacaExecutionError:
        status = cls._status_code(exc)
        suffix = f", HTTP {status}" if status is not None else ""
        return AlpacaExecutionError(
            f"Alpaca paper {operation} failed ({type(exc).__name__}{suffix})."
        )
