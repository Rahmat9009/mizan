"""Fetching daily bars. This is the ONLY module in the lane that touches a socket.

It is separate from ``mizan.signal.vol`` on purpose: the signal is a pure function of bars, so the
network appears once, at the edge, and evaluation stays reproducible from a stored series. Credentials
are read from the process environment and are never written to a file, a log line or a reading.

Market data is a read. Nothing in this module can place, size or modify an order, and the host it
talks to serves historical bars only - it is not a trading endpoint.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

from mizan.signal.bars import Bar, BarDataError, parse_bars_payload

__all__ = [
    "DEFAULT_SYMBOL",
    "MARKET_DATA_HOST",
    "MarketDataUnavailable",
    "MissingCredentials",
    "bars_url",
    "fetch_daily_bars",
]

#: Historical market data. Read-only; no order endpoint is reachable from here.
MARKET_DATA_HOST = "data.alpaca.markets"
_BARS_PATH = "/v2/stocks/{symbol}/bars"
DEFAULT_SYMBOL = "SPY"
_TIMEFRAME = "1Day"
_PAGE_LIMIT = "10000"
_MAX_PAGES = 10
_KEY_ENV = "APCA_API_KEY_ID"
_SECRET_ENV = "APCA_API_SECRET_KEY"
_FALLBACK_KEY_ENV = "ALPACA_API_KEY"
_FALLBACK_SECRET_ENV = "ALPACA_SECRET_KEY"


class MissingCredentials(RuntimeError):
    """No market-data credentials in the environment. Named, so the caller can degrade rather than crash."""


class MarketDataUnavailable(RuntimeError):
    """The venue did not return a usable bar series. Carries the status class, never the response body."""


def bars_url(symbol: str, *, start: str | None = None, page_token: str | None = None) -> str:
    """The historical daily-bars URL for ``symbol``. Pure string building; no request is made here."""
    query: dict[str, str] = {"timeframe": _TIMEFRAME, "limit": _PAGE_LIMIT, "adjustment": "raw"}
    if start is not None:
        query["start"] = start
    if page_token is not None:
        query["page_token"] = page_token
    path = _BARS_PATH.format(symbol=urllib.parse.quote(symbol, safe=""))
    return f"https://{MARKET_DATA_HOST}{path}?{urllib.parse.urlencode(query)}"


def _credentials() -> tuple[str, str]:
    key = os.environ.get(_KEY_ENV) or os.environ.get(_FALLBACK_KEY_ENV)
    secret = os.environ.get(_SECRET_ENV) or os.environ.get(_FALLBACK_SECRET_ENV)
    if not key or not secret:
        raise MissingCredentials(
            f"set {_KEY_ENV} and {_SECRET_ENV} in the environment to fetch bars "
            "(they are read from the process environment and never written anywhere)"
        )
    return key, secret


def _get(url: str, key: str, secret: str, *, timeout_seconds: int) -> str:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - fixed https host, built above
    request.add_header("APCA-API-KEY-ID", key)
    request.add_header("APCA-API-SECRET-KEY", secret)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return str(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MarketDataUnavailable(f"market-data request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MarketDataUnavailable(f"market-data request failed: {type(exc).__name__}") from None


def fetch_daily_bars(
    symbol: str = DEFAULT_SYMBOL,
    *,
    start: str | None = None,
    timeout_seconds: int = 20,
    max_pages: int = _MAX_PAGES,
) -> tuple[Bar, ...]:
    """Fetch daily bars for ``symbol``, oldest first, following the venue's pagination.

    Raises ``MissingCredentials`` when the environment has none and ``MarketDataUnavailable`` when the
    venue refuses or answers with something that is not a bar series. It never returns a partial
    series dressed up as a complete one.
    """
    key, secret = _credentials()
    collected: list[Bar] = []
    token: str | None = None
    for _ in range(max_pages):
        body = _get(bars_url(symbol, start=start, page_token=token), key, secret,
                    timeout_seconds=timeout_seconds)
        try:
            page, token = parse_bars_payload(body)
        except BarDataError as exc:
            raise MarketDataUnavailable(f"unusable market-data payload: {exc}") from None
        collected.extend(page)
        if not token:
            break
    if not collected:
        raise MarketDataUnavailable(f"no daily bars returned for {symbol}")
    # One page order, one series: pages are merged by day so a retry or an overlap cannot duplicate a bar.
    by_day = {bar.day: bar for bar in collected}
    return tuple(by_day[day] for day in sorted(by_day))
