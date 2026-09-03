"""The stdio MCP client, tested against a real subprocess rather than an in-process double.

The client's entire purpose is to survive another process: a banner on stderr, non-protocol bytes on
stdout, a notification arriving before the answer, a server that never replies. None of those can be
reproduced by a mock, so ``tests/mcp/_fake_server.py`` is spawned for real and made to misbehave.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mizan.mcp.client import (
    PROTOCOL_VERSION,
    MCPError,
    MCPToolDenied,
    MCPToolResult,
    StdioMCPClient,
)

FAKE_SERVER = Path(__file__).resolve().parent / "_fake_server.py"


def client(behaviour: str = "plain", **kwargs) -> StdioMCPClient:
    return StdioMCPClient([sys.executable, str(FAKE_SERVER), behaviour], **kwargs)


def test_handshake_records_what_answered_and_on_which_protocol() -> None:
    with client() as connection:
        assert connection.server_info == {"name": "fake", "version": "9.9.9"}
        assert connection.negotiated_version == PROTOCOL_VERSION
        assert connection.running


def test_a_tool_call_round_trips_its_arguments() -> None:
    with client() as connection:
        result = connection.call_tool("echo", {"symbol": "SPY", "limit": 3})
        assert result.is_error is False
        assert result.json() == {"symbol": "SPY", "limit": 3}


def test_non_text_content_blocks_are_not_text() -> None:
    """An image block has no ``text``. Concatenating one in would corrupt the JSON beside it."""
    with client() as connection:
        result = connection.call_tool("echo", {"a": 1})
        assert result.text == '{"a": 1}'


def test_structured_content_wins_over_text_when_the_server_sends_it() -> None:
    with client() as connection:
        assert connection.call_tool("structured").json() == {"from": "structuredContent"}


def test_tools_list_follows_next_cursor_to_the_end() -> None:
    with client("paged") as connection:
        assert [tool["name"] for tool in connection.list_tools()] == ["first", "second"]


def test_a_notification_arriving_first_is_not_mistaken_for_the_answer() -> None:
    with client() as connection:
        assert connection.call_tool("chatty").json() == {"ok": True}


def test_an_in_band_tool_failure_is_a_result_not_an_exception() -> None:
    """``isError`` means "your request failed", which is an ANSWER. Raising would lose the reason."""
    with client() as connection:
        result = connection.call_tool("explodes")
        assert result.is_error is True
        assert "it went wrong" in result.text
        with pytest.raises(MCPError, match="it went wrong"):
            result.raise_for_error("explodes")


def test_a_banner_on_stderr_and_junk_on_stdout_do_not_deadlock_or_derail() -> None:
    with client("noise") as connection:
        assert connection.call_tool("echo", {"ok": 1}).json() == {"ok": 1}
        assert "a banner nobody asked for" in connection.stderr_tail


def test_a_server_that_never_answers_becomes_an_error_not_a_hang() -> None:
    with client(timeout=1.0) as connection:
        with pytest.raises(MCPError, match="did not answer"):
            connection.call_tool("slow")


def test_a_server_that_dies_at_startup_is_an_error() -> None:
    with pytest.raises(MCPError, match="stopped"), client("die-on-start"):
        pass  # pragma: no cover - the context manager raises on entry


def test_an_unknown_method_surfaces_the_servers_error_object() -> None:
    with client() as connection:
        with pytest.raises(MCPError, match="unknown method"):
            connection.request("resources/list", {})


class TestAllowlist:
    """The allowlist is the mechanism Hard Rule B4 is re-imposed with. It has to bite before the pipe."""

    def test_a_tool_outside_the_allowlist_is_refused(self) -> None:
        with client(allowed_tools={"echo"}) as connection:
            with pytest.raises(MCPToolDenied, match="close_all_positions"):
                connection.call_tool("close_all_positions")

    def test_an_allowed_tool_still_works(self) -> None:
        with client(allowed_tools={"echo"}) as connection:
            assert connection.call_tool("echo", {"x": 1}).json() == {"x": 1}

    def test_an_empty_allowlist_permits_nothing_and_is_not_the_same_as_no_allowlist(self) -> None:
        """``frozenset()`` is a real configuration. Conflating it with ``None`` would open everything."""
        with client(allowed_tools=set()) as connection:
            assert connection.allowed_tools == frozenset()
            with pytest.raises(MCPToolDenied):
                connection.call_tool("echo")

    def test_no_allowlist_means_no_restriction(self) -> None:
        with client() as connection:
            assert connection.allowed_tools is None
            connection.assert_allowed("anything at all")

    def test_the_denial_happens_before_anything_is_sent(self) -> None:
        """Proven by the request counter: a denied call must not consume a JSON-RPC id."""
        with client(allowed_tools={"echo"}) as connection:
            connection.call_tool("echo", {})
            before = connection._next_id
            with pytest.raises(MCPToolDenied):
                connection.call_tool("close_position")
            assert connection._next_id == before


class TestResultShaping:
    def test_text_that_is_not_json_comes_back_as_the_string_it_is(self) -> None:
        result = MCPToolResult(content=[{"type": "text", "text": "plain words"}])
        assert result.json() == "plain words"

    def test_empty_content_is_none_rather_than_an_invented_object(self) -> None:
        assert MCPToolResult(content=[]).json() is None


def test_close_is_idempotent_and_leaves_nothing_running() -> None:
    connection = client().start()
    connection.close()
    connection.close()
    assert connection.running is False


def test_an_empty_command_is_refused_rather_than_spawning_a_shell() -> None:
    with pytest.raises(MCPError, match="command is required"):
        StdioMCPClient([])
