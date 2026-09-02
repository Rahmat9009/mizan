from __future__ import annotations

import os
from typing import Any, Callable, Protocol

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class AlpacaConfigurationError(RuntimeError):
    """Alpaca read-only integration is missing or has invalid configuration."""


class AlpacaLiveModeDisabledError(AlpacaConfigurationError):
    """Raised before any client is created when live brokerage mode is requested."""


class AlpacaClientInitializationError(RuntimeError):
    """The official SDK client could not be initialized safely."""


class ReadOnlyAlpacaClientProtocol(Protocol):
    def get_account(self) -> Any: ...

    def get_all_positions(self) -> Any: ...


class ReadOnlyAlpacaClient:
    """Capability wrapper exposing only account and open-position reads."""

    __slots__ = ("__sdk_client",)

    def __init__(self, sdk_client: Any) -> None:
        self.__sdk_client = sdk_client

    def get_account(self) -> Any:
        return self.__sdk_client.get_account()

    def get_all_positions(self) -> Any:
        return self.__sdk_client.get_all_positions()


def alpaca_paper_mode(value: bool | None = None) -> bool:
    if value is not None:
        return value
    raw = os.getenv("ALPACA_PAPER")
    if raw is None or not raw.strip():
        return True
    normalized = raw.strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise AlpacaConfigurationError(
        "ALPACA_PAPER must be a true/false value; paper mode is the only permitted mode."
    )


def create_read_only_alpaca_client(
    *,
    api_key: str | None = None,
    secret_key: str | None = None,
    paper: bool | None = None,
    client_factory: Callable[..., Any] = TradingClient,
) -> ReadOnlyAlpacaClient:
    """Create one fail-closed, paper-only SDK client behind a read-only facade."""

    resolved_key, resolved_secret = resolve_alpaca_paper_credentials(
        api_key=api_key,
        secret_key=secret_key,
        paper=paper,
    )
    try:
        sdk_client = client_factory(resolved_key, resolved_secret, paper=True)
    except Exception as exc:
        raise AlpacaClientInitializationError(
            f"Alpaca SDK initialization failed ({type(exc).__name__})."
        ) from exc
    return ReadOnlyAlpacaClient(sdk_client)


def resolve_alpaca_paper_credentials(
    *,
    api_key: str | None = None,
    secret_key: str | None = None,
    paper: bool | None = None,
) -> tuple[str, str]:
    """Resolve credentials only after proving that paper mode is requested."""

    load_dotenv()
    if not alpaca_paper_mode(paper):
        raise AlpacaLiveModeDisabledError(
            "ALPACA_PAPER=false is forbidden: this project supports paper accounts only."
        )

    resolved_key = (api_key or os.getenv("ALPACA_API_KEY") or "").strip()
    resolved_secret = (secret_key or os.getenv("ALPACA_SECRET_KEY") or "").strip()
    missing: list[str] = []
    if not resolved_key:
        missing.append("ALPACA_API_KEY")
    if not resolved_secret:
        missing.append("ALPACA_SECRET_KEY")
    if missing:
        raise AlpacaConfigurationError(f"{', '.join(missing)} is not configured.")
    return resolved_key, resolved_secret
