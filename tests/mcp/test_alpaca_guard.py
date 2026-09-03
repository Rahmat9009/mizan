"""The safety properties of routing a broker through Alpaca's official MCP server.

That server offers tools which cancel every order and close every position. Mizan's ``BrokerAdapter``
Protocol has no vocabulary for any of them (Hard Rule B4), and adopting a new transport must not
quietly hand those capabilities back. These tests assert the three independent guards, the two paper
signals, and that a credential never leaves the process boundary it arrived at.

Nothing here starts the real server or opens a socket. The rules being tested are decided before a
byte is sent, which is exactly why they can be tested hermetically.
"""

from __future__ import annotations

import pytest

from mizan.adapters.base import PAPER_HOST
from mizan.contracts.errors import ConfigurationError, LiveTradingForbidden
from mizan.mcp.alpaca import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    READ_TOOLS,
    REQUIRED_TOOLSETS,
    UNAUTHENTICATED_PROBE,
    WRITE_TOOLS,
    AlpacaMCPBroker,
    _assert_paper_account,
    _require_paper_environment,
    alpaca_mcp_environment,
    resolve_alpaca_mcp_command,
)
from mizan.mcp.client import StdioMCPClient

#: Every tool on Alpaca's server that mutates or destroys state without a Mizan decision existing.
DESTRUCTIVE = (
    "cancel_all_orders",
    "cancel_order_by_id",
    "replace_order_by_id",
    "close_all_positions",
    "close_position",
    "exercise_options_position",
    "do_not_exercise_options_position",
)


class TestTheAllowlistReimposesB4:
    def test_no_destructive_tool_is_reachable(self) -> None:
        for tool in DESTRUCTIVE:
            assert tool not in ALLOWED_TOOLS, f"{tool} would let an agent bypass every decision"

    def test_every_destructive_tool_is_named_explicitly_rather_than_merely_absent(self) -> None:
        """A ban by omission is not testable. Naming them is what makes a future edit fail here."""
        for tool in DESTRUCTIVE:
            assert tool in FORBIDDEN_TOOLS

    def test_the_two_sets_cannot_overlap(self) -> None:
        assert not (ALLOWED_TOOLS & FORBIDDEN_TOOLS)

    def test_exactly_two_order_placing_tools_and_they_are_options_and_equities(self) -> None:
        assert WRITE_TOOLS == {"place_stock_order", "place_option_order"}

    def test_crypto_cannot_be_ordered_through_this_adapter(self) -> None:
        """The policy language cannot describe a crypto position, so the venue must be unreachable."""
        assert "place_crypto_order" in FORBIDDEN_TOOLS
        assert "place_crypto_order" not in ALLOWED_TOOLS

    def test_the_reads_cover_the_four_the_protocol_requires(self) -> None:
        for tool in ("get_account_info", "get_all_positions", "get_stock_latest_quote"):
            assert tool in READ_TOOLS
        assert "get_order_by_client_id" in READ_TOOLS  # the E7 idempotency read

    def test_the_toolset_filter_excludes_the_venues_mizan_does_not_govern(self) -> None:
        toolsets = set(REQUIRED_TOOLSETS.split(","))
        assert "crypto-data" not in toolsets
        assert "watchlists" not in toolsets
        assert "locates" not in toolsets

    def test_a_broker_cannot_be_built_on_a_client_with_no_allowlist(self) -> None:
        unrestricted = StdioMCPClient(["python", "-c", "pass"])
        with pytest.raises(ConfigurationError, match="capabilities Mizan does not permit"):
            AlpacaMCPBroker(unrestricted)

    def test_a_broker_cannot_be_built_on_a_client_that_allows_a_forbidden_tool(self) -> None:
        loosened = StdioMCPClient(
            ["python", "-c", "pass"], allowed_tools=ALLOWED_TOOLS | {"close_all_positions"}
        )
        with pytest.raises(ConfigurationError, match="capabilities Mizan does not permit"):
            AlpacaMCPBroker(loosened)


class TestThePaperProof:
    def test_the_child_environment_is_forced_to_paper_rather_than_inheriting_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The official server picks its endpoint from this variable. Inheriting it would be a path."""
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
        assert alpaca_mcp_environment()["ALPACA_PAPER_TRADE"] == "true"

    def test_a_non_paper_parent_value_is_refused_loudly_not_silently_corrected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overwriting it quietly would leave an operator believing their environment was honoured."""
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
        with pytest.raises(LiveTradingForbidden):
            alpaca_mcp_environment()

    def test_the_refusal_applies_to_an_explicitly_passed_mapping_too(self) -> None:
        """It is a property of the environment being BUILT, not an ambient switch on our behaviour."""
        with pytest.raises(LiveTradingForbidden):
            alpaca_mcp_environment(
                {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER_TRADE": "0"}
            )

    def test_alpaca_paper_is_the_only_ambient_variable_this_module_gates_on(self) -> None:
        """Everything else is an argument. An inherited value does not appear in what anyone typed."""
        import ast
        from pathlib import Path

        import mizan.mcp.alpaca as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        read = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert read == {"ALPACA_PAPER"}

    def test_absent_alpaca_paper_is_not_permission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALPACA_PAPER", raising=False)
        with pytest.raises(LiveTradingForbidden):
            _require_paper_environment()

    def test_alpaca_paper_must_be_explicitly_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_PAPER", "maybe")
        with pytest.raises(LiveTradingForbidden):
            _require_paper_environment()

    @pytest.mark.parametrize("number", ["", "  ", "U12345", "1PA234", None])
    def test_an_account_that_does_not_identify_as_paper_is_refused(self, number: object) -> None:
        with pytest.raises(LiveTradingForbidden):
            _assert_paper_account({} if number is None else {"account_number": number})

    def test_a_pa_prefixed_account_is_accepted(self) -> None:
        _assert_paper_account({"account_number": "PA3ABCDEFGH"})

    def test_this_module_spells_no_broker_host_at_all(self) -> None:
        """It imports :data:`PAPER_HOST` and writes no hostname of its own, in code or in prose.

        The stronger form of "there is no live host constant": there is no host LITERAL here, so
        neither a typo nor a helpful edit can introduce a second endpoint, and a grep for a non-paper
        Alpaca host across ``mizan/`` returns nothing (INV-16 asserts that repository-wide).
        """
        import re
        from pathlib import Path

        import mizan.mcp.alpaca as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "PAPER_HOST" in source, "the module should use the one shared constant"
        assert not re.search(r"[a-z-]*\.?alpaca\.markets", source), "no hostname literal belongs here"
        assert PAPER_HOST == "paper-api.alpaca.markets"


class TestCredentials:
    def test_credentials_are_read_from_the_environment_and_passed_through_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key-from-parent")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret-from-parent")
        env = alpaca_mcp_environment()
        assert env["ALPACA_API_KEY"] == "key-from-parent"
        assert env["ALPACA_SECRET_KEY"] == "secret-from-parent"

    def test_the_apca_spellings_are_accepted_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        monkeypatch.setenv("APCA_API_KEY_ID", "apca-key")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "apca-secret")
        env = alpaca_mcp_environment()
        assert env["ALPACA_API_KEY"] == "apca-key"

    def test_missing_credentials_are_a_configuration_error_not_a_silent_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ConfigurationError):
            alpaca_mcp_environment()

    def test_the_surface_probe_placeholder_could_never_be_mistaken_for_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            monkeypatch.delenv(name, raising=False)
        env = alpaca_mcp_environment(require_credentials=False)
        assert env["ALPACA_API_KEY"] == UNAUTHENTICATED_PROBE
        assert " " not in UNAUTHENTICATED_PROBE and "mizan" in UNAUTHENTICATED_PROBE
        # Not key-shaped: Alpaca keys are short upper-case alphanumerics, and this is neither.
        assert not UNAUTHENTICATED_PROBE.isalnum()


class TestServerResolution:
    def test_an_explicit_command_wins_and_it_is_an_argument_not_a_variable(self) -> None:
        """Which executable becomes the broker transport is at least as consequential as the broker."""
        assert resolve_alpaca_mcp_command(["my-server", "--transport", "stdio"]) == [
            "my-server",
            "--transport",
            "stdio",
        ]

    def test_no_environment_variable_can_redirect_the_server_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIZAN_ALPACA_MCP_CMD", "attacker-server")
        assert "attacker-server" not in resolve_alpaca_mcp_command()

    def test_the_default_pins_an_exact_version(self) -> None:
        argv = resolve_alpaca_mcp_command()
        assert any("alpaca-mcp-server==" in part for part in argv), argv
        assert "stdio" in argv
