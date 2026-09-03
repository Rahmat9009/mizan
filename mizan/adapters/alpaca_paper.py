"""The Alpaca PAPER adapter. Constructing one against anything else is impossible.

Three things make that true rather than merely intended:

* :meth:`AlpacaPaperBroker.from_environment` proves paper mode *before* it reads a credential and long
  before it opens a socket, so a misconfigured environment cannot leak a key or reach a venue (B1);
* the client's own base URL is checked at construction **and again immediately before every
  submission** - finding F-19 was an adapter whose ``paper_mode_verified`` was a constant ``True``, so
  this build re-derives the answer from the object that will actually carry the request;
* no method here can cancel, replace or close anything (B4). The class has four reads and one write.

Credentials are read from the customer's environment, handed to the SDK and never stored on the
instance or in a contract object (B2): Mizan holds no broker keys and no funds.

Every SDK value is mapped into a contract type at the boundary. Nothing with a vendor's class, a
vendor's number format or a vendor's mutability escapes into the engine.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mizan.adapters.base import PAPER_HOST, BrokerOrder, OrderRequest
from mizan.contracts import (
    DECIMAL_CONTEXT,
    AccountState,
    Environment,
    MarketSnapshot,
    OptionQuote,
    PortfolioSnapshot,
    Position,
    Quote,
    ReasonCode,
    dstr,
    format_ts,
    normalize_ts,
)
from mizan.contracts.errors import BrokerError, ConfigurationError, LiveTradingForbidden

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["AlpacaPaperBroker"]

_TRUE = frozenset({"1", "true", "yes", "on"})

#: Environment variables the customer sets. Read once, at construction, and never persisted (B2).
_KEY_VARIABLES = ("ALPACA_API_KEY", "APCA_API_KEY_ID")
_SECRET_VARIABLES = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

#: Alpaca's asset classes, mapped to the contract's two.
_ASSET_CLASSES = {
    "us_equity": "equity",
    "equity": "equity",
    "us_option": "equity_option",
    "option": "equity_option",
}


def _require_paper_environment() -> None:
    """``ALPACA_PAPER`` must be present and explicitly true. Absent is not permission."""
    raw = os.getenv("ALPACA_PAPER")
    if raw is None or raw.strip().casefold() not in _TRUE:
        raise LiveTradingForbidden(
            message="ALPACA_PAPER must be explicitly true; this build has no live trading path."
        )


def _first_env(names: Sequence[str]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _assert_paper_client(client: Any) -> None:
    """The client that will carry the request must be pointed at the paper host. Re-derived, not cached.

    The SDK stores the base URL as an enum member, so the *value* is read rather than ``str()`` of the
    member - a member's repr names the constant, and a check that passes on a name rather than on the
    host it resolves to is exactly the kind of proof finding F-19 warned about.

    The comparison is host *equality*, not containment: a URL whose host merely starts with the paper
    host - ``paper-api.alpaca.markets.example.invalid`` - contains it and is not it. A client with no
    discoverable base URL is refused for the same reason an absent ``ALPACA_PAPER`` is: silence is not
    permission.
    """
    raw = getattr(client, "_base_url", None) or getattr(client, "base_url", None)
    base_url = str(getattr(raw, "value", raw) or "")
    if (urlsplit(base_url).hostname or "").casefold() != PAPER_HOST:
        raise LiveTradingForbidden(
            message="The broker client is not pointed at the paper endpoint.",
            detail="base url does not name the paper host",
        )


#: Alpaca stamps every paper account number with this prefix. A live account never carries it.
PAPER_ACCOUNT_PREFIX = "PA"


def _bool_or_none(value: Any) -> bool | None:
    """A tri-state read: True, False, or "the broker did not say". Never coerce absence to False.

    ``bool(None)`` is False, and False here means "not blocked" - which would turn a missing field into
    a grant of permission. That is the exact shape of an ESC-4 defect, so absence stays absent and the
    check blocks on ACCOUNT_STATE_MISSING.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _account_state_of(account: Any, *, as_of: datetime) -> AccountState:
    """The account's PERMISSIONS, mapped into the contract (REQ-35).

    Deliberately carries no account number: that identifier is sensitive, `redact` strips the key, and a
    record carrying one would be unbuildable. The paper proof reads it live instead and never persists it.
    """
    status = getattr(account, "status", None)
    return AccountState(
        as_of=format_ts(as_of),
        status=None if status is None else str(getattr(status, "value", status)),
        trading_blocked=_bool_or_none(getattr(account, "trading_blocked", None)),
        account_blocked=_bool_or_none(getattr(account, "account_blocked", None)),
        trade_suspended_by_user=_bool_or_none(getattr(account, "trade_suspended_by_user", None)),
        shorting_enabled=_bool_or_none(getattr(account, "shorting_enabled", None)),
        options_trading_level=_int_or_none(getattr(account, "options_trading_level", None)),
        source="alpaca:paper:account",
    )


def _assert_paper_account(account: Any) -> None:
    """The SECOND paper signal: the account itself must say it is a paper account.

    The base-URL check (F-19) proves where the request is going. It cannot prove what is waiting there:
    a correct-looking host with a live account behind it passes it. Alpaca prefixes every paper account
    number with ``PA``, so the account is asked to identify itself and the two signals must AGREE.

    An absent, empty or non-``PA`` account number is refused, for the same reason an absent
    ``ALPACA_PAPER`` is: silence is not permission, and this is the one decision where a permissive
    default is unacceptable. Never proceed on one signal.
    """
    raw = getattr(account, "account_number", None)
    number = "" if raw is None else str(raw).strip()
    if not number.startswith(PAPER_ACCOUNT_PREFIX):
        raise LiveTradingForbidden(
            message="The broker account does not identify itself as a paper account.",
            detail=(
                "account_number is absent or does not carry the paper prefix; the base URL and the "
                "account must agree and this build proceeds on neither alone"
            ),
        )


def _decimal_str(value: Any) -> str | None:
    """Any SDK number into a canonical DecimalStr, or None when the SDK said nothing.

    ``Decimal(str(value))`` is deliberate: it goes through the *text* the SDK produced rather than a
    binary approximation of it, so a price never acquires digits nobody quoted.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dstr(Decimal(text))
    except (ArithmeticError, ValueError):
        return None


def _required_decimal(value: Any, field: str) -> str:
    result = _decimal_str(value)
    if result is None:
        raise BrokerError(
            "The broker did not report a value the engine requires.",
            reason_codes=[ReasonCode.MARKET_DATA_MISSING],
            detail=f"missing {field}",
        )
    return result


def _timestamp(value: Any, *, fallback: datetime) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return format_ts(moment)
    if isinstance(value, str) and value.strip():
        try:
            return normalize_ts(value)
        except (ValueError, TypeError):
            return format_ts(fallback)
    return format_ts(fallback)


def _underlying_of(occ_symbol: str) -> str:
    """The root symbol of an OCC option symbol: the leading alphabetic run."""
    root = ""
    for character in occ_symbol:
        if character.isdigit():
            break
        root += character
    return root


def _midpoint(bid: str | None, ask: str | None) -> str | None:
    if bid is None or ask is None:
        return None
    low, high = Decimal(bid), Decimal(ask)
    if low <= 0 or high <= 0:
        return None
    return dstr(DECIMAL_CONTEXT.divide(DECIMAL_CONTEXT.add(low, high), Decimal(2)))


class AlpacaPaperBroker:
    """Alpaca PAPER adapter. Four reads and one mutation, all in contract types."""

    name = "alpaca"
    environment: Environment = "paper"

    def __init__(
        self,
        client: Any,
        *,
        stock_data_client: Any | None = None,
        option_data_client: Any | None = None,
    ) -> None:
        _assert_paper_client(client)
        self._client = client
        self._stock_data = stock_data_client
        self._option_data = option_data_client

    @classmethod
    def from_environment(cls) -> AlpacaPaperBroker:
        """Prove paper mode FIRST, then read credentials, then build a client.

        The ordering matters: a live-configured environment must fail before credentials are read and
        long before a socket is opened, so a misconfiguration cannot leak a key or reach a venue.
        """
        _require_paper_environment()
        api_key = _first_env(_KEY_VARIABLES)
        secret_key = _first_env(_SECRET_VARIABLES)
        if not api_key or not secret_key:
            raise ConfigurationError(
                message="Broker credentials are not configured in this environment.",
                detail="set ALPACA_API_KEY and ALPACA_SECRET_KEY in the deployment environment",
            )
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        return cls(
            TradingClient(api_key=api_key, secret_key=secret_key, paper=True),
            stock_data_client=StockHistoricalDataClient(api_key=api_key, secret_key=secret_key),
            option_data_client=OptionHistoricalDataClient(api_key=api_key, secret_key=secret_key),
        )

    # -- reads --------------------------------------------------------------------------------
    def get_account_state(self, *, as_of: datetime) -> AccountState:
        """The account's permissions. Both paper signals are proven on the way past."""
        account = _call(self._client.get_account)
        _assert_paper_account(account)
        return _account_state_of(account, as_of=as_of)

    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot:
        """Account and positions, mapped into the contract. Absent numbers stay absent (E2)."""
        account = _call(self._client.get_account)
        # Both paper signals, on every account read: where the request goes, and what answered.
        _assert_paper_account(account)
        raw_positions = _call(self._client.get_all_positions)
        positions = [_position_of(raw) for raw in raw_positions or ()]
        return PortfolioSnapshot(
            snapshot_id=f"alpaca-pf-{format_ts(as_of)}",
            as_of=format_ts(as_of),
            equity=_required_decimal(getattr(account, "equity", None), "account.equity"),
            cash=_required_decimal(getattr(account, "cash", None), "account.cash"),
            buying_power=_decimal_str(getattr(account, "buying_power", None)),
            peak_equity=None,
            daily_pnl=None,
            positions=positions,
            greeks=None,
            source="alpaca:paper:account",
            gross_exposure=None,
            net_exposure=None,
            margin_requirement=_decimal_str(getattr(account, "maintenance_margin", None)),
            maintenance_excess=None,
            factor_exposures=None,
        )

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: datetime
    ) -> MarketSnapshot:
        """Quotes for the symbols the engine will value. A symbol with no quote is simply absent.

        Absent is the correct answer: ``mizan.risk`` blocks on a missing price (E2), and inventing one
        here - a last close, a zero, the proposal's own limit price (F-1) - is how that block is lost.
        """
        quotes: dict[str, Quote] = {}
        for symbol, raw in _latest_quotes(self._stock_data, symbols).items():
            quote = _quote_of(symbol, raw, as_of=as_of)
            if quote is not None:
                quotes[symbol] = quote
        option_quotes: dict[str, OptionQuote] = {}
        for occ_symbol, raw in _latest_option_quotes(self._option_data, occ_symbols).items():
            option_quote = _option_quote_of(occ_symbol, raw, as_of=as_of)
            if option_quote is not None:
                option_quotes[occ_symbol] = option_quote
        # REQ-34. Two changes, and BOTH are needed or neither works.
        #
        # 1. snapshot_id is DERIVED from the content rather than minted from the clock. The old
        #    f"alpaca-mkt-{now}" id made every read unique, so the execution gate - which compares the
        #    market half of a BoundState by id - saw a state change on every single execution and
        #    therefore told the operator nothing.
        # 2. as_of is taken from the DATA, not from the caller's clock. `as_of` arrives here as the
        #    context provider's `now`, so hashing it would put the read time inside the content and
        #    every id would still be unique - the same defect, moved. The snapshot is as fresh as its
        #    freshest quote; with no quotes at all there is nothing to be fresh, so the read time is
        #    the honest fallback.
        observed = [q.as_of for q in quotes.values()] + [q.as_of for q in option_quotes.values()]
        snapshot_as_of = max(observed) if observed else format_ts(as_of)
        return MarketSnapshot.build(
            as_of=snapshot_as_of,
            quotes=quotes,
            option_quotes=option_quotes,
            sectors={},
            source="alpaca:paper",
        )

    def find_order(self, client_order_id: str) -> BrokerOrder | None:
        """The idempotency read (E7). A broker that cannot answer is an error, never a silent None."""
        try:
            raw = self._client.get_order_by_client_id(client_order_id)
        except Exception as failure:  # noqa: BLE001 - every SDK error becomes one machine code
            if _is_not_found(failure):
                return None
            raise _broker_error(failure) from failure
        return None if raw is None else _order_of(raw)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        try:
            raw = self._client.get_order_by_id(broker_order_id)
        except Exception as failure:  # noqa: BLE001
            raise _broker_error(failure) from failure
        return _order_of(raw)

    # -- the one and only mutation ------------------------------------------------------------
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Submit to the paper venue. The paper proof is re-derived here, not trusted from before.

        F-19: the class attribute says paper; this line proves it, against the very client object that
        is about to carry the request, at the moment it carries it.
        """
        _assert_paper_client(self._client)
        # The account is re-asked at the mutation boundary too. One extra read is a cheap price for
        # never submitting into an account that has not just identified itself as paper.
        _assert_paper_account(_call(self._client.get_account))
        if len(request.legs) != 1:
            raise BrokerError(
                "Multi-leg submission is not supported by this adapter.",
                reason_codes=[ReasonCode.BROKER_REJECTED],
                detail=f"{len(request.legs)} legs",
            )
        leg = request.legs[0]
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        symbol = leg.occ_symbol if leg.occ_symbol is not None else leg.symbol
        common: dict[str, Any] = {
            "symbol": symbol,
            "qty": leg.quantity,
            "side": OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": request.client_order_id,
        }
        # Annotated because the two SDK request types are siblings, not subclasses of one another, and
        # the alpaca package is imported lazily - so there is no shared base to narrow to here.
        order_request: Any
        if leg.order_type == "limit":
            order_request = LimitOrderRequest(limit_price=leg.limit_price, **common)
        else:
            order_request = MarketOrderRequest(**common)
        try:
            raw = self._client.submit_order(order_data=order_request)
        except Exception as failure:  # noqa: BLE001
            raise _broker_error(failure) from failure
        return _order_of(raw)


# -----------------------------------------------------------------------------------------------
# SDK -> contract mapping. Everything below turns a vendor object into a Mizan object exactly once.
# -----------------------------------------------------------------------------------------------
def _call(method: Any) -> Any:
    try:
        return method()
    except Exception as failure:  # noqa: BLE001
        raise _broker_error(failure) from failure


def _broker_error(failure: BaseException) -> BrokerError:
    """One machine code and one generic sentence. The vendor's text goes to ``detail``, for logs only."""
    return BrokerError(
        "The broker could not be reached.",
        reason_codes=[ReasonCode.BROKER_UNAVAILABLE],
        detail=type(failure).__name__,
    )


def _is_not_found(failure: BaseException) -> bool:
    return getattr(failure, "status_code", None) == 404


def _position_of(raw: Any) -> Position:
    raw_symbol = str(getattr(raw, "symbol", ""))
    raw_class = getattr(raw, "asset_class", "us_equity")
    asset_class = _ASSET_CLASSES.get(str(getattr(raw_class, "value", raw_class)), "equity")
    is_option = asset_class == "equity_option"
    return Position(
        symbol=_underlying_of(raw_symbol) if is_option else raw_symbol,
        asset_class=asset_class,  # type: ignore[arg-type]
        quantity=_required_decimal(getattr(raw, "qty", None), "position.qty"),
        market_value=_required_decimal(getattr(raw, "market_value", None), "position.market_value"),
        sector=None,
        occ_symbol=raw_symbol if is_option else None,
        delta=None,
        gamma=None,
        vega=None,
    )


def _latest_quotes(data_client: Any, symbols: Sequence[str]) -> dict[str, Any]:
    if data_client is None or not symbols:
        return {}
    from alpaca.data.requests import StockLatestQuoteRequest

    wanted = sorted({str(symbol) for symbol in symbols})
    try:
        result = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=wanted))
    except Exception as failure:  # noqa: BLE001
        raise _broker_error(failure) from failure
    return dict(result or {})


def _latest_option_quotes(data_client: Any, occ_symbols: Sequence[str]) -> dict[str, Any]:
    if data_client is None or not occ_symbols:
        return {}
    from alpaca.data.requests import OptionLatestQuoteRequest

    wanted = sorted({str(symbol) for symbol in occ_symbols})
    try:
        result = data_client.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=wanted))
    except Exception as failure:  # noqa: BLE001
        raise _broker_error(failure) from failure
    return dict(result or {})


def _quote_of(symbol: str, raw: Any, *, as_of: datetime) -> Quote | None:
    """A quote only exists when a usable price exists. The midpoint, never the caller's number (F-1)."""
    bid = _decimal_str(getattr(raw, "bid_price", None))
    ask = _decimal_str(getattr(raw, "ask_price", None))
    price = _midpoint(bid, ask) or _decimal_str(getattr(raw, "price", None))
    if price is None or Decimal(price) <= 0:
        return None
    return Quote(
        symbol=symbol,
        price=price,
        bid=bid if bid is not None and Decimal(bid) > 0 else None,
        ask=ask if ask is not None and Decimal(ask) > 0 else None,
        as_of=_timestamp(getattr(raw, "timestamp", None), fallback=as_of),
        source="alpaca:paper:quotes",
    )


def _option_quote_of(occ_symbol: str, raw: Any, *, as_of: datetime) -> OptionQuote | None:
    bid = _decimal_str(getattr(raw, "bid_price", None))
    ask = _decimal_str(getattr(raw, "ask_price", None))
    mark = _midpoint(bid, ask)
    if mark is None or Decimal(mark) <= 0:
        return None
    greeks = getattr(raw, "greeks", None)
    return OptionQuote(
        occ_symbol=occ_symbol,
        mark=mark,
        delta=_decimal_str(getattr(greeks, "delta", None)),
        gamma=_decimal_str(getattr(greeks, "gamma", None)),
        vega=_decimal_str(getattr(greeks, "vega", None)),
        theta=_decimal_str(getattr(greeks, "theta", None)),
        as_of=_timestamp(getattr(raw, "timestamp", None), fallback=as_of),
        source="alpaca:paper:options",
    )


def _order_of(raw: Any) -> BrokerOrder:
    """Map an SDK order. Its identifiers become strings here and stay strings everywhere after."""
    broker_order_id = str(getattr(raw, "id", "") or "")
    client_order_id = str(getattr(raw, "client_order_id", "") or "")
    if not broker_order_id or not client_order_id:
        raise BrokerError(
            "The broker returned an order without identifiers.",
            reason_codes=[ReasonCode.BROKER_REJECTED],
        )
    status = getattr(raw, "status", None)
    submitted = getattr(raw, "submitted_at", None) or getattr(raw, "created_at", None)
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        status=str(getattr(status, "value", status) or "unknown"),
        submitted_at=_timestamp(submitted, fallback=datetime.now(UTC)),
        filled_quantity=_decimal_str(getattr(raw, "filled_qty", None)) or "0",
        avg_price=_decimal_str(getattr(raw, "filled_avg_price", None)),
    )
