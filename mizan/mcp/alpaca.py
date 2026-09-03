"""Alpaca reached through Alpaca's OFFICIAL MCP server, behind the same rules as every other adapter.

``alpaca-mcp-server`` (alpacahq/alpaca-mcp-server on PyPI) exposes 72 tools. Seven of them can destroy
an account without a decision ever being recorded::

    cancel_all_orders   close_all_positions   close_position   cancel_order_by_id
    replace_order_by_id   exercise_options_position   do_not_exercise_options_position

Mizan's broker abstraction deliberately has no vocabulary for any of those (Hard Rule B4: four reads
and exactly one mutation, and cancel/replace/close are out of scope for v1). Handing an agent the raw
server would give back every capability the Protocol was shaped to remove, so this module re-imposes
B4 on the MCP surface in three independent places:

1. ``ALPACA_TOOLSETS`` restricts which tools the server *creates* at all;
2. :data:`ALLOWED_TOOLS` is enforced by :class:`~mizan.mcp.client.StdioMCPClient` *before a byte is
   written to the pipe* - a denied tool is unreachable, not merely uncalled;
3. :data:`FORBIDDEN_TOOLS` is asserted disjoint from the allowlist at import time, so a future edit
   that adds ``close_all_positions`` to the allowlist fails the test suite rather than shipping.

The two paper signals from ``mizan.adapters.alpaca_paper`` are re-derived here rather than assumed,
because the MCP transport changes *how* they can be proven, not *whether* they must be:

* the base URL is not inspectable through an MCP tool call, so instead the child's environment is
  CONSTRUCTED rather than inherited - ``ALPACA_PAPER_TRADE`` is forced to ``true``. The official
  server computes its base URL as ``paper-api`` vs ``api`` from exactly that variable, so a parent
  environment carrying ``ALPACA_PAPER_TRADE=false`` cannot reach a live venue through this class;
* the account must still identify itself with Alpaca's ``PA`` prefix, checked at construction and
  again immediately before every submission. Silence is not permission.

Vendor JSON is mapped into contract types exactly once, here. A field the API did not send stays
absent and is recorded in :attr:`AlpacaMCPBroker.deltas`; nothing here invents a price, a quantity or
a permission to make a response parse.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from functools import reduce
from math import gcd
from typing import Any

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
from mizan.contracts.types import dec
from mizan.mcp.client import MCPError, MCPToolResult, StdioMCPClient

__all__ = [
    "ALLOWED_TOOLS",
    "UNAUTHENTICATED_PROBE",
    "ALPACA_MCP_PACKAGE",
    "FORBIDDEN_TOOLS",
    "READ_TOOLS",
    "REQUIRED_TOOLSETS",
    "WRITE_TOOLS",
    "AlpacaMCPBroker",
    "alpaca_mcp_environment",
    "resolve_alpaca_mcp_command",
]

#: The official server, pinned. An MCP server is a separate process, so pinning it here costs the
#: decision path nothing and buys a reproducible tool surface.
ALPACA_MCP_PACKAGE = "alpaca-mcp-server==2.3.1"

#: Only the toolsets Mizan reads from. The server builds no tool for anything else.
REQUIRED_TOOLSETS = "account,trading,assets,stock-data,options-data"

#: The four reads of the BrokerAdapter Protocol, expressed in the official server's tool names.
READ_TOOLS = frozenset(
    {
        "get_account_info",  # account permissions and balances
        "get_all_positions",  # portfolio
        "get_stock_latest_quote",  # equity marks
        "get_option_latest_quote",  # option marks
        "get_option_snapshot",  # option marks WITH greeks
        "get_option_contracts",  # the chain
        "get_order_by_client_id",  # the idempotency read (Hard Rule E7)
        "get_order_by_id",
        "get_clock",  # session state
    }
)

#: The ONE mutation. ``place_crypto_order`` is not here: this build trades equities and equity options
#: and an adapter that can reach a venue Mizan's policy language cannot describe is a hole.
WRITE_TOOLS = frozenset({"place_stock_order", "place_option_order"})

#: Everything the Protocol has no vocabulary for. Named explicitly so the ban is testable, and so a
#: reader can see exactly which capabilities were taken away rather than inferring it from an absence.
FORBIDDEN_TOOLS = frozenset(
    {
        "cancel_all_orders",
        "cancel_order_by_id",
        "replace_order_by_id",
        "close_all_positions",
        "close_position",
        "exercise_options_position",
        "do_not_exercise_options_position",
        "update_account_config",
        "place_crypto_order",
        "create_watchlist",
        "update_watchlist_by_id",
        "delete_watchlist_by_id",
        "add_asset_to_watchlist_by_id",
        "remove_asset_from_watchlist_by_id",
        "create_locate",
    }
)

ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS

# A capability that is denied in one place and granted in another is denied nowhere. Checked at import
# so the two sets cannot drift apart silently.
assert not (ALLOWED_TOOLS & FORBIDDEN_TOOLS), "a forbidden MCP tool is on the allowlist"

_TRUE = frozenset({"1", "true", "yes", "on"})
_KEY_VARIABLES = ("ALPACA_API_KEY", "APCA_API_KEY_ID")
_SECRET_VARIABLES = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

#: Alpaca stamps every paper account number with this prefix. A live account never carries it.
PAPER_ACCOUNT_PREFIX = "PA"

#: Stands in for a credential when only the server's TOOL SURFACE is being inspected. Deliberately a
#: sentence rather than a plausible key, so that it can never be mistaken for one in a log, a process
#: list or a screenshot - and so that any request carrying it is refused by Alpaca, as it should be.
UNAUTHENTICATED_PROBE = "mizan-unauthenticated-surface-probe"

_ASSET_CLASSES = {
    "us_equity": "equity",
    "equity": "equity",
    "us_option": "equity_option",
    "option": "equity_option",
}


# ---------------------------------------------------------------------------------------------------
# Starting the server
# ---------------------------------------------------------------------------------------------------
def resolve_alpaca_mcp_command(command: Sequence[str] | None = None) -> list[str]:
    """How to start the official Alpaca MCP server on this machine.

    An explicit ``command`` wins. Otherwise: ``uvx``, which runs the pinned package in its own
    ephemeral environment and is the reason Mizan's own pins cannot be disturbed; then an
    ``alpaca-mcp-server`` already on PATH; then the module in this interpreter.

    The override is an ARGUMENT rather than an environment variable on purpose. Which executable
    becomes the broker transport is at least as consequential as which broker is chosen, and an
    inherited variable can redirect it without appearing in the command anyone typed.

    Raises rather than guessing. A broker that cannot be started is an error the gate can refuse on.
    """
    if command:
        return list(command)
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", ALPACA_MCP_PACKAGE, "alpaca-mcp-server", "--transport", "stdio"]
    on_path = shutil.which("alpaca-mcp-server")
    if on_path:
        return [on_path, "--transport", "stdio"]
    try:  # pragma: no cover - depends on what is installed
        import alpaca_mcp_server  # type: ignore[import-not-found] # noqa: F401
    except ImportError:
        raise ConfigurationError(
            message="The Alpaca MCP server is not available on this machine.",
            detail=(
                "install uv (which provides uvx) or `pip install alpaca-mcp-server`, or set "
                "MIZAN_ALPACA_MCP_CMD to the command that starts it"
            ),
        ) from None
    import sys

    return [sys.executable, "-m", "alpaca_mcp_server", "--transport", "stdio"]


def alpaca_mcp_environment(
    base: Mapping[str, str] | None = None, *, require_credentials: bool = True
) -> dict[str, str]:
    """The child's environment, CONSTRUCTED rather than inherited on the two variables that matter.

    The official server selects the paper endpoint or the other one purely from ``ALPACA_PAPER_TRADE``.
    Inheriting that variable would mean a parent shell could point Mizan's broker away from the paper
    venue, which is the F-19 defect with a new transport. So it is *overwritten*, not defaulted, and
    Mizan's own ``ALPACA_PAPER`` is proven true before we get this far. The only host this module
    names is :data:`~mizan.adapters.base.PAPER_HOST`; there is no constant, and no prose, for another.

    Credentials are copied from the parent process into the child and are never written anywhere: the
    child needs them to authenticate, and Mizan holds no broker keys (Hard Rule B2).

    ``require_credentials=False`` is for inspecting the server's *tool surface*, which is a question
    about capabilities rather than about an account. The official server refuses to start with no
    credentials at all, so :data:`UNAUTHENTICATED_PROBE` is substituted - a string chosen to be
    unmistakable in a process list. Every account call then fails 401 at Alpaca, which is the correct
    answer to asking about an account with no key, and no read in this mode can be mistaken for data.
    """
    env = dict(os.environ if base is None else base)
    api_key = _first(env, _KEY_VARIABLES)
    secret_key = _first(env, _SECRET_VARIABLES)
    if not api_key or not secret_key:
        if require_credentials:
            raise ConfigurationError(
                message="Broker credentials are not configured in this environment.",
                detail=(
                    "set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                    "(or APCA_API_KEY_ID/APCA_API_SECRET_KEY)"
                ),
            )
        api_key = secret_key = UNAUTHENTICATED_PROBE
    env["ALPACA_API_KEY"] = api_key
    env["ALPACA_SECRET_KEY"] = secret_key
    # Not setdefault. This build has no live path, and a variable that could be inherited is a path.
    #
    # The inherited value is also REFUSED rather than quietly overwritten, so an operator whose shell
    # says non-paper is told their environment is wrong instead of having it silently corrected. Note
    # that this reads the mapping the child is about to receive - it is not a switch on Mizan's own
    # behaviour, and the only outcome it can produce is a refusal.
    inherited = str(env.get("ALPACA_PAPER_TRADE") or "true").strip().casefold()
    if inherited not in _TRUE:
        raise LiveTradingForbidden(
            message="ALPACA_PAPER_TRADE is set to a non-paper value in this environment.",
            detail="the Alpaca MCP server selects its endpoint from this variable",
        )
    env["ALPACA_PAPER_TRADE"] = "true"
    env["ALPACA_TOOLSETS"] = env.get("ALPACA_TOOLSETS") or REQUIRED_TOOLSETS
    env["ALPACA_MCP_USER_AGENT"] = "mizan-governance/0.1.0"
    return env


def _first(env: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _require_paper_environment() -> None:
    """``ALPACA_PAPER`` must be present and explicitly true. Absent is not permission (B1).

    The only ambient variable this module gates on, and the only outcome it has is a refusal.
    """
    raw = os.getenv("ALPACA_PAPER")
    if raw is None or raw.strip().casefold() not in _TRUE:
        raise LiveTradingForbidden(
            message="ALPACA_PAPER must be explicitly true; this build has no live trading path."
        )


# ---------------------------------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------------------------------
class AlpacaMCPBroker:
    """A ``BrokerAdapter`` whose every call is an MCP ``tools/call`` against Alpaca's own server.

    Structurally identical to ``AlpacaPaperBroker`` - four reads and one mutation, contract types at
    the boundary, both paper signals proven - and different only in the transport underneath.
    """

    name = "alpaca-mcp"
    environment: Environment = "paper"

    def __init__(self, client: StdioMCPClient) -> None:
        if client.allowed_tools is None or (client.allowed_tools & FORBIDDEN_TOOLS):
            raise ConfigurationError(
                message="This MCP client may reach broker capabilities Mizan does not permit.",
                detail="an AlpacaMCPBroker requires a client allowlisted to ALLOWED_TOOLS (B4)",
            )
        self._client = client
        #: Fields the API did not send where the contract expected one. Reported, never patched over.
        self.deltas: list[str] = []

    # -- construction ------------------------------------------------------------------------------
    @classmethod
    def connect(
        cls, *, timeout: float = 60.0, command: Sequence[str] | None = None
    ) -> AlpacaMCPBroker:
        """Prove paper mode FIRST, then read credentials, then start the server. Order is the point.

        A non-paper environment must fail before a credential is read and long before a socket is
        opened, so a misconfiguration cannot leak a key or reach a venue.
        """
        _require_paper_environment()
        env = alpaca_mcp_environment()
        client = StdioMCPClient(
            resolve_alpaca_mcp_command(command),
            env=env,
            allowed_tools=ALLOWED_TOOLS,
            timeout=timeout,
        )
        client.start()
        broker = cls(client)
        # The second paper signal, before this object is handed to anything: the account itself must
        # say it is a paper account. One signal is never enough.
        broker.get_account_state(as_of=datetime.now(UTC))
        return broker

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AlpacaMCPBroker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def client(self) -> StdioMCPClient:
        return self._client

    @property
    def server_info(self) -> dict[str, Any]:
        """Who answered, and on which revision of the protocol. Printed by the CLI, never a decision."""
        return {
            "server": self._client.server_info,
            "protocol_version": self._client.negotiated_version,
            "argv": self._client.argv[:1] + ["..."] if self._client.argv else [],
            "tools_allowed": sorted(ALLOWED_TOOLS),
            "tools_forbidden": sorted(FORBIDDEN_TOOLS),
            "base_url": f"https://{PAPER_HOST}",
        }

    # -- reads -------------------------------------------------------------------------------------
    def get_account_state(self, *, as_of: datetime) -> AccountState:
        """The account's PERMISSIONS. Carries no account number: that identifier is sensitive (REQ-35)."""
        account = self._account()
        return AccountState(
            as_of=format_ts(as_of),
            status=_text_or_none(account.get("status")),
            trading_blocked=_bool_or_none(account.get("trading_blocked")),
            account_blocked=_bool_or_none(account.get("account_blocked")),
            trade_suspended_by_user=_bool_or_none(account.get("trade_suspended_by_user")),
            shorting_enabled=_bool_or_none(account.get("shorting_enabled")),
            options_trading_level=_int_or_none(account.get("options_trading_level")),
            source="alpaca:mcp:paper:account",
        )

    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot:
        """Account and positions. An absent number stays absent (Hard Rule E2)."""
        account = self._account()
        raw_positions = self._call("get_all_positions", {}).raise_for_error("get_all_positions").json()
        positions = [_position_of(raw) for raw in _as_list(raw_positions)]
        return PortfolioSnapshot(
            snapshot_id=f"alpaca-mcp-pf-{format_ts(as_of)}",
            as_of=format_ts(as_of),
            equity=self._required_decimal(account.get("equity"), "account.equity"),
            cash=self._required_decimal(account.get("cash"), "account.cash"),
            buying_power=_decimal_str(account.get("buying_power")),
            peak_equity=None,
            daily_pnl=None,
            positions=positions,
            greeks=None,
            source="alpaca:mcp:paper:account",
            gross_exposure=None,
            net_exposure=None,
            margin_requirement=_decimal_str(account.get("maintenance_margin")),
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
        for symbol, raw in self._stock_quotes(symbols).items():
            quote = _quote_of(symbol, raw, as_of=as_of)
            if quote is not None:
                quotes[symbol] = quote
        option_quotes: dict[str, OptionQuote] = {}
        for occ_symbol, raw in self._option_snapshots(occ_symbols).items():
            option_quote = _option_quote_of(occ_symbol, raw, as_of=as_of)
            if option_quote is not None:
                option_quotes[occ_symbol] = option_quote
        # REQ-34: the id is derived from the CONTENT and as_of comes from the DATA, not from the
        # caller's clock, or every read would look like a state change to the execution gate.
        observed = [q.as_of for q in quotes.values()] + [q.as_of for q in option_quotes.values()]
        return MarketSnapshot.build(
            as_of=max(observed) if observed else format_ts(as_of),
            quotes=quotes,
            option_quotes=option_quotes,
            sectors={},
            source="alpaca:mcp:paper",
        )

    def find_order(self, client_order_id: str) -> BrokerOrder | None:
        """The idempotency read (E7). A broker that cannot answer is an error, never a silent None."""
        result = self._call("get_order_by_client_id", {"client_order_id": client_order_id})
        if result.is_error:
            if _looks_like_not_found(result.text):
                return None
            raise _broker_unavailable(result.text)
        payload = result.json()
        return None if not isinstance(payload, Mapping) else _order_of(payload)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        payload = self._call("get_order_by_id", {"order_id": broker_order_id})
        payload.raise_for_error("get_order_by_id")
        body = payload.json()
        if not isinstance(body, Mapping):
            raise _broker_unavailable("get_order_by_id returned no order object")
        return _order_of(body)

    def get_option_chain(self, underlying: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """The tradable contracts for an underlying. Not part of the BrokerAdapter Protocol - this is
        the agent's *research* read, surfaced by the CLI and by ``mizan.mcp.server``."""
        result = self._call(
            "get_option_contracts", {"underlying_symbols": underlying.upper(), "limit": limit}
        ).raise_for_error("get_option_contracts")
        payload = result.json()
        if isinstance(payload, Mapping):
            contracts = payload.get("option_contracts") or payload.get("contracts") or []
            return [dict(c) for c in contracts if isinstance(c, Mapping)]
        return [dict(c) for c in _as_list(payload) if isinstance(c, Mapping)]

    def get_clock(self) -> dict[str, Any]:
        payload = self._call("get_clock", {}).raise_for_error("get_clock").json()
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    # -- the one and only mutation -----------------------------------------------------------------
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Submit to the paper venue through the official server's order tool.

        The paper account signal is re-derived HERE, against the account that is about to receive the
        order, at the moment it receives it - not trusted from construction time.
        """
        self._account()
        if request.legs[0].occ_symbol is None and len(request.legs) == 1:
            return self._submit_equity(request)
        return self._submit_option(request)

    def _submit_equity(self, request: OrderRequest) -> BrokerOrder:
        leg = request.legs[0]
        arguments = {
            "symbol": leg.symbol,
            "side": leg.side,
            "qty": str(leg.quantity),
            "type": leg.order_type,
            "time_in_force": "day",
            "client_order_id": request.client_order_id,
        }
        if leg.order_type == "limit":
            if leg.limit_price is None:
                raise BrokerError(
                    "A limit order carries no limit price.",
                    reason_codes=[ReasonCode.BROKER_REJECTED],
                    detail=f"leg {leg.leg_index} order_type=limit with no limit_price",
                )
            arguments["limit_price"] = str(leg.limit_price)
        return self._submitted("place_stock_order", arguments)

    def _submit_option(self, request: OrderRequest) -> BrokerOrder:
        """One atomic order, single-leg or ``mleg``, so no naked leg can ever exist in between.

        Two single-leg orders have a window in which the short leg fills and the long one does not,
        which is exactly the undefined-risk position ``structure_valid`` refuses at decision time.
        Defending a rule in the engine and breaking it in the adapter is worse than not having it.
        """
        quantities = [int(dec(leg.quantity)) for leg in request.legs]
        if any(quantity <= 0 for quantity in quantities):
            raise BrokerError(
                "A leg quantity is not a positive whole number of contracts.",
                reason_codes=[ReasonCode.BROKER_REJECTED],
                detail="option legs require whole contracts",
            )
        for leg in request.legs:
            if leg.occ_symbol is None:
                raise BrokerError(
                    "An option order requires an OCC symbol on every leg.",
                    reason_codes=[ReasonCode.BROKER_REJECTED],
                    detail=f"leg {leg.leg_index} carries no occ_symbol",
                )
        closing = request.intent == "close"

        arguments: dict[str, Any]
        if len(request.legs) == 1:
            leg = request.legs[0]
            arguments = {
                "symbol": leg.occ_symbol,
                "side": leg.side,
                "qty": str(quantities[0]),
                "type": leg.order_type,
                "time_in_force": "day",
                "client_order_id": request.client_order_id,
                "position_intent": _position_intent(leg.side, closing=closing),
            }
            if leg.order_type == "limit":
                if leg.limit_price is None:
                    raise BrokerError(
                        "A limit order carries no limit price.",
                        reason_codes=[ReasonCode.BROKER_REJECTED],
                        detail=f"leg {leg.leg_index} order_type=limit with no limit_price",
                    )
                arguments["limit_price"] = str(leg.limit_price)
            return self._submitted("place_option_order", arguments)

        # mleg: `qty` is the number of SPREADS and each leg carries a ratio relative to it. Mizan's
        # legs hold absolute quantities, so the ratio is their GCD - a 2:2 spread is two 1:1 spreads,
        # never one 2:2 order.
        spreads = reduce(gcd, quantities)
        legs = [
            {
                "symbol": leg.occ_symbol,
                "ratio_qty": str(quantity // spreads),
                "side": leg.side,
                "position_intent": _position_intent(leg.side, closing=closing),
            }
            for leg, quantity in zip(request.legs, quantities, strict=True)
        ]
        arguments = {
            "qty": str(spreads),
            "order_class": "mleg",
            "legs": legs,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": request.client_order_id,
        }
        net_limit = _net_limit_price(request, spreads)
        if net_limit is not None:
            arguments["type"] = "limit"
            arguments["limit_price"] = dstr(net_limit)
        return self._submitted("place_option_order", arguments)

    def _submitted(self, tool: str, arguments: Mapping[str, Any]) -> BrokerOrder:
        result = self._call(tool, arguments)
        if result.is_error:
            raise BrokerError(
                "The broker rejected the order.",
                reason_codes=[ReasonCode.BROKER_REJECTED],
                detail=f"{tool}: {result.text[:300]}",
            )
        payload = result.json()
        if not isinstance(payload, Mapping):
            raise BrokerError(
                "The broker returned no order object.",
                reason_codes=[ReasonCode.BROKER_REJECTED],
                detail=f"{tool} returned {type(payload).__name__}",
            )
        return _order_of(payload)

    # -- transport ---------------------------------------------------------------------------------
    def _account(self) -> dict[str, Any]:
        """The account, with the paper signal proven on the way past. Every account read, every time."""
        result = self._call("get_account_info", {})
        if result.is_error:
            raise _broker_unavailable(result.text)
        payload = result.json()
        if not isinstance(payload, Mapping):
            raise _broker_unavailable("get_account_info returned no account object")
        account = dict(payload)
        _assert_paper_account(account)
        return account

    def _stock_quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        wanted = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not wanted:
            return {}
        result = self._call("get_stock_latest_quote", {"symbols": ",".join(wanted)})
        if result.is_error:
            raise _broker_unavailable(result.text)
        payload = result.json()
        if isinstance(payload, Mapping):
            quotes = payload.get("quotes")
            if isinstance(quotes, Mapping):
                return dict(quotes)
            return {k: v for k, v in payload.items() if k in wanted}
        self.deltas.append(f"get_stock_latest_quote returned {type(payload).__name__}, not an object")
        return {}

    def _option_snapshots(self, occ_symbols: Sequence[str]) -> dict[str, Any]:
        """Option marks WITH greeks. ``get_option_snapshot`` carries them; the latest-quote tool does not."""
        wanted = sorted({str(symbol).upper() for symbol in occ_symbols if str(symbol).strip()})
        if not wanted:
            return {}
        result = self._call("get_option_snapshot", {"symbols": ",".join(wanted)})
        if result.is_error:
            raise _broker_unavailable(result.text)
        payload = result.json()
        if isinstance(payload, Mapping):
            snapshots = payload.get("snapshots")
            if isinstance(snapshots, Mapping):
                return dict(snapshots)
            return {k: v for k, v in payload.items() if k in wanted}
        self.deltas.append(f"get_option_snapshot returned {type(payload).__name__}, not an object")
        return {}

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> MCPToolResult:
        try:
            return self._client.call_tool(tool, arguments)
        except MCPError as failure:
            raise BrokerError(
                "The broker could not be reached.",
                reason_codes=[ReasonCode.BROKER_UNAVAILABLE],
                detail=type(failure).__name__,
            ) from failure

    def _required_decimal(self, value: Any, field: str) -> str:
        result = _decimal_str(value)
        if result is None:
            self.deltas.append(f"alpaca mcp response is missing {field}")
            raise BrokerError(
                "The broker did not report a value the engine requires.",
                reason_codes=[ReasonCode.MARKET_DATA_MISSING],
                detail=f"missing {field}",
            )
        return result


# ---------------------------------------------------------------------------------------------------
# JSON -> contract mapping. Everything below turns a vendor document into a Mizan object exactly once.
# ---------------------------------------------------------------------------------------------------
def _assert_paper_account(account: Mapping[str, Any]) -> None:
    """The account itself must say it is a paper account. Silence is not permission.

    Forcing ``ALPACA_PAPER_TRADE=true`` in the child proves where the request is going. It cannot
    prove what is waiting there. Alpaca prefixes every paper account number with ``PA``, so the
    account is asked to identify itself and the two signals must AGREE. Never proceed on one alone.
    """
    raw = account.get("account_number")
    number = "" if raw is None else str(raw).strip()
    if not number.startswith(PAPER_ACCOUNT_PREFIX):
        raise LiveTradingForbidden(
            message="The broker account does not identify itself as a paper account.",
            detail=(
                "account_number is absent or does not carry the paper prefix; the forced paper "
                "environment and the account must agree and this build proceeds on neither alone"
            ),
        )


def _position_intent(side: str, *, closing: bool) -> str:
    """Open vs close is the PROPOSAL's intent. The legs inherit it rather than guessing."""
    if closing:
        return "buy_to_close" if side == "buy" else "sell_to_close"
    return "buy_to_open" if side == "buy" else "sell_to_open"


def _net_limit_price(request: OrderRequest, spreads: int) -> Decimal | None:
    """The spread's NET limit PER SPREAD: debits paid minus credits received, at the leg RATIOS.

    Per spread, not per order. Alpaca prices an mleg order per spread and multiplies by ``qty``, so
    summing at absolute quantities would submit N times the intended debit. ``None`` when any leg
    lacks a limit: a spread priced on only some of its legs is not a limit order, and treating it as
    one would let the unpriced side fill at any price.
    """
    total = Decimal(0)
    for leg in request.legs:
        if leg.limit_price is None or leg.order_type != "limit":
            return None
        ratio = DECIMAL_CONTEXT.divide(dec(leg.quantity), Decimal(spreads))
        signed = dec(leg.limit_price) if leg.side == "buy" else -dec(leg.limit_price)
        total = DECIMAL_CONTEXT.add(total, DECIMAL_CONTEXT.multiply(signed, ratio))
    return total


def _as_list(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("positions", "data", "results"):
            nested = payload.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, str | bytes):
                return [item for item in nested if isinstance(item, Mapping)]
    return []


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_or_none(value: Any) -> bool | None:
    """A tri-state read: True, False, or "the broker did not say". Never coerce absence to False.

    ``bool(None)`` is False, and False here means "not blocked" - which would turn a missing field into
    a grant of permission. That is the exact shape of an ESC-4 defect, so absence stays absent.
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


def _decimal_str(value: Any) -> str | None:
    """Any JSON number into a canonical DecimalStr, or None when the API said nothing.

    ``Decimal(str(value))`` is deliberate: it goes through the *text* the API produced rather than a
    binary approximation of it, so a price never acquires digits nobody quoted.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dstr(Decimal(text))
    except (ArithmeticError, ValueError):
        return None


def _timestamp(value: Any, *, fallback: datetime) -> str:
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


def _pick(raw: Mapping[str, Any], *names: str) -> Any:
    """The first key the API actually sent. Alpaca's market-data JSON abbreviates (``bp``/``ap``/``t``)
    while its trading JSON spells fields out; both are read, and neither is invented."""
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def _position_of(raw: Mapping[str, Any]) -> Position:
    raw_symbol = str(raw.get("symbol") or "")
    raw_class = str(raw.get("asset_class") or "us_equity")
    asset_class = _ASSET_CLASSES.get(raw_class, "equity")
    is_option = asset_class == "equity_option"
    quantity = _decimal_str(_pick(raw, "qty", "quantity"))
    market_value = _decimal_str(raw.get("market_value"))
    if quantity is None or market_value is None:
        raise BrokerError(
            "The broker did not report a value the engine requires.",
            reason_codes=[ReasonCode.MARKET_DATA_MISSING],
            detail=f"position {raw_symbol} is missing qty or market_value",
        )
    return Position(
        symbol=_underlying_of(raw_symbol) if is_option else raw_symbol,
        asset_class=asset_class,  # type: ignore[arg-type]
        quantity=quantity,
        market_value=market_value,
        sector=None,  # Alpaca exposes no sector classification on any endpoint. Recorded as a delta.
        occ_symbol=raw_symbol if is_option else None,
        delta=None,
        gamma=None,
        vega=None,
    )


def _quote_of(symbol: str, raw: Any, *, as_of: datetime) -> Quote | None:
    """A quote exists only when a usable price exists. The midpoint, never the caller's number (F-1)."""
    if not isinstance(raw, Mapping):
        return None
    bid = _decimal_str(_pick(raw, "bid_price", "bp"))
    ask = _decimal_str(_pick(raw, "ask_price", "ap"))
    price = _midpoint(bid, ask) or _decimal_str(_pick(raw, "price", "p"))
    if price is None or Decimal(price) <= 0:
        return None
    return Quote(
        symbol=symbol,
        price=price,
        bid=bid if bid is not None and Decimal(bid) > 0 else None,
        ask=ask if ask is not None and Decimal(ask) > 0 else None,
        as_of=_timestamp(_pick(raw, "timestamp", "t"), fallback=as_of),
        source="alpaca:mcp:paper:quotes",
    )


def _option_quote_of(occ_symbol: str, raw: Any, *, as_of: datetime) -> OptionQuote | None:
    """An option snapshot into an OptionQuote. Greeks that were not sent stay ``None``."""
    if not isinstance(raw, Mapping):
        return None
    nested = raw.get("latestQuote")
    quote: Mapping[str, Any] = nested if isinstance(nested, Mapping) else raw
    bid = _decimal_str(_pick(quote, "bid_price", "bp"))
    ask = _decimal_str(_pick(quote, "ask_price", "ap"))
    mark = _midpoint(bid, ask)
    if mark is None or Decimal(mark) <= 0:
        return None
    reported = raw.get("greeks")
    greeks: Mapping[str, Any] = reported if isinstance(reported, Mapping) else {}
    return OptionQuote(
        occ_symbol=occ_symbol,
        mark=mark,
        delta=_decimal_str(greeks.get("delta")),
        gamma=_decimal_str(greeks.get("gamma")),
        vega=_decimal_str(greeks.get("vega")),
        theta=_decimal_str(greeks.get("theta")),
        as_of=_timestamp(_pick(quote, "timestamp", "t"), fallback=as_of),
        source="alpaca:mcp:paper:options",
    )


def _order_of(raw: Mapping[str, Any]) -> BrokerOrder:
    """Map an order document. Its identifiers become strings here and stay strings everywhere after."""
    broker_order_id = str(raw.get("id") or "")
    client_order_id = str(raw.get("client_order_id") or "")
    if not broker_order_id or not client_order_id:
        raise BrokerError(
            "The broker returned an order without identifiers.",
            reason_codes=[ReasonCode.BROKER_REJECTED],
        )
    submitted = _pick(raw, "submitted_at", "created_at")
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        status=str(raw.get("status") or "unknown"),
        submitted_at=_timestamp(submitted, fallback=datetime.now(UTC)),
        filled_quantity=_decimal_str(raw.get("filled_qty")) or "0",
        avg_price=_decimal_str(raw.get("filled_avg_price")),
    )


def _looks_like_not_found(text: str) -> bool:
    lowered = text.casefold()
    return "404" in lowered or "not found" in lowered


def _broker_unavailable(detail: str) -> BrokerError:
    """One machine code and one generic sentence. The vendor's text goes to ``detail``, for logs only."""
    return BrokerError(
        "The broker could not be reached.",
        reason_codes=[ReasonCode.BROKER_UNAVAILABLE],
        detail=detail[:300],
    )
