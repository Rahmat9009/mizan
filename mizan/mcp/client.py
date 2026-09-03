"""A Model Context Protocol client over stdio, in the standard library and nothing else.

Why hand-rolled rather than ``pip install mcp``: Mizan pins ``pydantic``, ``jsonschema`` and ``PyYAML``
EXACTLY, because their versions are recorded in every ``DecisionRecord.library_versions`` and an
unpinned upgrade would change the recorded provenance of every decision ever made (Master Plan C6).
The reference MCP SDK and ``fastmcp`` both float those pins. Installing them into this environment
would silently move the decision path, so the protocol - newline-delimited JSON-RPC 2.0, and small -
is implemented here instead, and the *server* runs in its own interpreter.

That isolation is the design, not a workaround. An MCP server is a separate process by definition, so
Alpaca's official server can hold whatever library versions it likes and Mizan's determinism
fingerprint does not move.

The one rule this module adds to the protocol is :class:`StdioMCPClient`'s ``allowed_tools``. A tool
that is not on that set is refused *before a byte is written to the pipe* - see ``mizan.mcp.alpaca``
for why that matters when the server on the other end can close every position in the account.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any

__all__ = [
    "DEFAULT_TIMEOUT",
    "MCP_CLIENT_INFO",
    "PROTOCOL_VERSION",
    "MCPError",
    "MCPToolDenied",
    "MCPToolResult",
    "StdioMCPClient",
]

#: The revision of the MCP specification this client speaks. A server that answers with a different
#: one is not refused: version negotiation is the server's to decide and the transport is unchanged.
PROTOCOL_VERSION = "2025-06-18"

MCP_CLIENT_INFO = {"name": "mizan", "version": "0.1.0"}

#: How long any single request may take. A broker read that hangs must become an error the execution
#: gate can refuse on, never a stalled decision.
DEFAULT_TIMEOUT = 60.0


class MCPError(RuntimeError):
    """The server failed, answered nothing, or answered something that is not this protocol."""


class MCPToolDenied(MCPError):
    """The tool is not on this client's allowlist. Raised before anything is sent."""


def _unwrap_envelope(payload: Any) -> Any:
    """Strip Alpaca's ``{_alpaca_mcp_security, data}`` wrapper, leaving the data it carries.

    The official server wraps every response in a security envelope that labels its own output
    ``untrusted_tool_output`` and tells the reader to treat it as data rather than instructions - a
    genuinely good move, and one this project agrees with, since ``reasoning`` reaching enforcement is
    what INV-17 exists to prevent.

    It has to be stripped centrally rather than per call site. Unwrapped in only some places, a field
    like ``account_number`` reads as ABSENT rather than wrong, and absent means "the broker did not
    say" - which is exactly the shape that turns a missing field into a silent grant of permission
    elsewhere. Here it fails closed and refuses, which is correct but for the wrong reason; better
    that the field simply arrives.

    Only unwrapped when the envelope marker is present, so a plain payload is passed through untouched.
    """
    if isinstance(payload, Mapping) and "_alpaca_mcp_security" in payload and "data" in payload:
        return payload["data"]
    return payload


class MCPToolResult:
    """One ``tools/call`` result: the text blocks, the structured payload, and whether it failed.

    ``is_error`` is the protocol's *in-band* failure flag. It is deliberately not raised on
    automatically: a caller reading an option chain for a symbol with no contracts wants an empty
    answer, and a caller reading an account wants an exception. ``raise_for_error`` lets each decide.
    """

    __slots__ = ("content", "is_error", "structured")

    def __init__(
        self,
        *,
        content: Sequence[Mapping[str, Any]] = (),
        structured: Any = None,
        is_error: bool = False,
    ) -> None:
        self.content = list(content)
        self.structured = structured
        self.is_error = bool(is_error)

    @property
    def text(self) -> str:
        """Every text block, joined. Non-text blocks (images, resources) are not text and are skipped."""
        return "\n".join(
            str(block.get("text", ""))
            for block in self.content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )

    def json(self) -> Any:
        """The payload as data: ``structuredContent`` when the server sent it, else the text parsed.

        Servers built on an OpenAPI bridge - Alpaca's is one - return a JSON document inside a text
        block, so both shapes have to be understood. Text that is not JSON comes back as the string it
        is rather than being coerced into something: inventing structure the server did not send is how
        a contract boundary stops meaning anything.
        """
        if self.structured is not None:
            return _unwrap_envelope(self.structured)
        text = self.text.strip()
        if not text:
            return None
        try:
            return _unwrap_envelope(json.loads(text))
        except ValueError:
            return text

    def raise_for_error(self, tool: str) -> MCPToolResult:
        if self.is_error:
            raise MCPError(f"MCP tool {tool!r} failed: {self.text[:500]}")
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MCPToolResult(is_error={self.is_error}, text={self.text[:80]!r})"


class StdioMCPClient:
    """A client for one MCP server subprocess, spoken over its stdin and stdout.

    Use it as a context manager; ``__exit__`` terminates the child. The child's stderr is drained by a
    daemon thread and kept as a bounded tail, because a server that logs a banner to stderr - and
    Alpaca's does - deadlocks any client that forgets to read it.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        allowed_tools: Iterable[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        stderr_lines: int = 40,
    ) -> None:
        if not argv:
            raise MCPError("an MCP server command is required")
        self.argv = list(argv)
        self._env = dict(os.environ if env is None else env)
        self._cwd = cwd
        #: ``None`` means "no allowlist". An empty set means "nothing is allowed", which is a real and
        #: useful configuration, so the two are never conflated.
        self.allowed_tools: frozenset[str] | None = (
            None if allowed_tools is None else frozenset(allowed_tools)
        )
        self.timeout = timeout
        self._stderr_lines = stderr_lines
        self._process: subprocess.Popen[str] | None = None
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 0
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.negotiated_version: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------------
    def __enter__(self) -> StdioMCPClient:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> StdioMCPClient:
        """Spawn the server and complete the initialize handshake."""
        if self._process is not None:
            return self
        try:
            self._process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as failure:
            raise MCPError(f"could not start the MCP server {self.argv[0]!r}: {failure}") from failure
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._initialize()
        return self

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - the OS refused to reap it
                pass

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def stderr_tail(self) -> str:
        """The last few stderr lines, for a diagnostic. Never parsed, never part of a decision."""
        return "\n".join(self._stderr)

    # -- protocol ----------------------------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server offers, following ``nextCursor`` to the end."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self.request("tools/list", params)
            tools.extend(result.get("tools", []) or [])
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen:
                return tools
            seen.add(cursor)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> MCPToolResult:
        """Invoke a tool. A name outside ``allowed_tools`` never reaches the pipe.

        The check lives here, at the transport, rather than in each caller: a capability that is merely
        "not called" is still reachable by the next bug or the next helpful refactor, and the point of
        the allowlist is that the forbidden tools are *unreachable* rather than unused (Hard Rule B4).
        """
        self.assert_allowed(name)
        payload = self.request("tools/call", {"name": name, "arguments": dict(arguments or {})})
        return MCPToolResult(
            content=payload.get("content", []) or [],
            structured=payload.get("structuredContent"),
            is_error=bool(payload.get("isError", False)),
        )

    def assert_allowed(self, name: str) -> None:
        if self.allowed_tools is not None and name not in self.allowed_tools:
            raise MCPToolDenied(
                f"MCP tool {name!r} is not on this client's allowlist; "
                f"allowed: {sorted(self.allowed_tools)}"
            )

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One JSON-RPC request and its response. Raises on an error object or on a timeout."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)
        payload = self._await(request_id)
        if "error" in payload:
            error = payload["error"] or {}
            raise MCPError(
                f"MCP {method} failed: {error.get('message', 'unknown error')} "
                f"(code {error.get('code', 'none')})"
            )
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

    # -- internals ---------------------------------------------------------------------------------
    def _initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(MCP_CLIENT_INFO),
            },
        )
        self.negotiated_version = result.get("protocolVersion")
        self.server_info = dict(result.get("serverInfo") or {})
        self.server_capabilities = dict(result.get("capabilities") or {})
        self.notify("notifications/initialized")

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPError("the MCP server is not running")
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if "\n" in line:  # pragma: no cover - json.dumps never emits a raw newline
            raise MCPError("a stdio MCP message may not contain a newline")
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except OSError as failure:
            raise MCPError(f"the MCP server closed its input: {failure}\n{self.stderr_tail}") from failure

    def _await(self, request_id: int) -> dict[str, Any]:
        """Wait for the response carrying this id, discarding anything else.

        Notifications and server-initiated requests are dropped rather than answered: this client
        advertises no capabilities, so a server has nothing legitimate to ask it for, and replying to
        an unexpected request would be inventing a capability we do not have.
        """
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                payload = self._inbox.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(payload, BaseException):
                raise MCPError(f"the MCP server stopped: {payload}\n{self.stderr_tail}")
            if isinstance(payload, Mapping) and payload.get("id") == request_id:
                return dict(payload)
        raise MCPError(
            f"the MCP server did not answer request {request_id} within {self.timeout}s\n"
            f"{self.stderr_tail}"
        )

    def _pump_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    self._inbox.put(json.loads(text))
                except ValueError:
                    # A server that writes non-JSON to stdout is out of protocol. Keep it as a
                    # diagnostic rather than crashing the reader thread.
                    self._remember(f"non-JSON on stdout: {text[:200]}")
        except (OSError, ValueError) as failure:  # pragma: no cover - pipe teardown
            self._inbox.put(failure)
        finally:
            self._inbox.put(EOFError("the MCP server closed its output"))

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._remember(line.rstrip())
        except OSError:  # pragma: no cover - pipe teardown
            pass

    def _remember(self, line: str) -> None:
        self._stderr.append(line)
        if len(self._stderr) > self._stderr_lines:
            del self._stderr[: -self._stderr_lines]
