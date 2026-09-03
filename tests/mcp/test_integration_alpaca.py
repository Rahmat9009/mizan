"""The real thing: Mizan's client against Alpaca's real MCP server, over a real pipe.

Skipped by default, because the rest of the suite must stay hermetic - it downloads a package, starts
a subprocess, and reaches ``paper-api.alpaca.markets``. Turn it on deliberately::

    MIZAN_MCP_INTEGRATION=1 python -m pytest -q tests/mcp/test_integration_alpaca.py

The first two tests need no Alpaca credentials at all: what they assert is that the transport works
and that the allowlist holds, and a 401 from Alpaca is itself proof that the request left the machine
and arrived. The rest need working PAPER credentials and are skipped without them.

Nothing here submits an order. The write path is exercised against a stub in test_alpaca_mapping.py;
placing a real order from a test suite is not something a test should decide to do.
"""

from __future__ import annotations

import os
import shutil

import pytest

from mizan.mcp.alpaca import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    AlpacaMCPBroker,
    alpaca_mcp_environment,
    resolve_alpaca_mcp_command,
)
from mizan.mcp.client import MCPToolDenied, StdioMCPClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MIZAN_MCP_INTEGRATION", "") != "1",
        reason="set MIZAN_MCP_INTEGRATION=1 to talk to Alpaca's real MCP server",
    ),
    pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx is needed to run the official server"),
]

HAS_CREDENTIALS = bool(
    (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"))
    and (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY"))
)
needs_credentials = pytest.mark.skipif(
    not HAS_CREDENTIALS, reason="paper credentials are not set in this environment"
)


@pytest.fixture
def official():
    client = StdioMCPClient(
        resolve_alpaca_mcp_command(),
        env=alpaca_mcp_environment(require_credentials=False),
        allowed_tools=ALLOWED_TOOLS,
        timeout=120.0,
    )
    with client:
        yield client


class TestTheTransport:
    def test_the_official_server_starts_and_negotiates_the_protocol(self, official) -> None:
        assert "alpaca" in str(official.server_info.get("name", "")).casefold()
        assert official.negotiated_version

    def test_it_offers_the_tools_mizan_reads_through(self, official) -> None:
        offered = {tool["name"] for tool in official.list_tools()}
        missing = {t for t in ALLOWED_TOOLS if t not in offered}
        assert not missing, f"the official server no longer offers: {sorted(missing)} - DELTA"

    def test_it_still_offers_the_destructive_tools_mizan_refuses_to_send(self, official) -> None:
        """The reason the allowlist exists. If this ever stops being true, say so - do not relax it."""
        offered = {tool["name"] for tool in official.list_tools()}
        assert offered & FORBIDDEN_TOOLS, "expected the official server to expose destructive tools"

    def test_no_forbidden_tool_can_be_sent_even_though_the_server_would_accept_it(
        self, official
    ) -> None:
        for tool in sorted(FORBIDDEN_TOOLS):
            with pytest.raises(MCPToolDenied):
                official.call_tool(tool, {})

    def test_an_allowed_read_reaches_alpaca(self, official) -> None:
        """With no credential this is a 401, which is exactly the proof wanted: the request arrived."""
        result = official.call_tool("get_account_info", {})
        if HAS_CREDENTIALS:
            assert result.is_error is False
        else:
            assert result.is_error is True
            assert "401" in result.text or "unauthorized" in result.text.casefold()


@needs_credentials
class TestAgainstTheRealPaperAccount:
    @pytest.fixture
    def broker(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ALPACA_PAPER", "true")
        adapter = AlpacaMCPBroker.connect(timeout=120.0)
        yield adapter
        adapter.close()

    def test_the_account_identifies_itself_as_paper(self, broker) -> None:
        from datetime import UTC, datetime

        state = broker.get_account_state(as_of=datetime.now(UTC))
        assert state.status
        # AlpacaMCPBroker.connect already refused anything without the PA prefix; this is the record.
        assert state.source == "alpaca:mcp:paper:account"

    def test_the_portfolio_maps_into_the_contract(self, broker) -> None:
        from datetime import UTC, datetime

        snapshot = broker.get_portfolio_snapshot(as_of=datetime.now(UTC))
        assert snapshot.equity and snapshot.cash
        assert broker.deltas == [], f"contract deltas against the real API: {broker.deltas}"

    def test_a_live_quote_maps_into_the_contract(self, broker) -> None:
        from datetime import UTC, datetime

        snapshot = broker.get_market_snapshot(symbols=["SPY"], as_of=datetime.now(UTC))
        # Outside market hours there may be no two-sided quote, and absent is the correct answer.
        if "SPY" in snapshot.quotes:
            assert snapshot.quotes["SPY"].price

    def test_the_option_chain_can_be_read(self, broker) -> None:
        contracts = broker.get_option_chain("SPY", limit=5)
        assert isinstance(contracts, list)

    def test_an_unknown_client_order_id_is_absent_rather_than_an_error(self, broker) -> None:
        assert broker.find_order("mizan-no-such-order-0000") is None
