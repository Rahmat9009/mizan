"""L3 — the Model Context Protocol surface, in both directions.

Two servers meet in this package, and which is which matters:

**Outbound** (``alpaca.py``, ``client.py``) — Mizan is an MCP *client* of Alpaca's official
``alpaca-mcp-server``. Account, positions, quotes, option contracts, order status and the one order
submission all travel as ``tools/call`` messages over stdio. Alpaca's own server is the broker
transport; ``alpaca-py`` is no longer on the path when this adapter is selected.

**Inbound** (``server.py``) — Mizan is an MCP *server*, and its tools are governed operations rather
than broker endpoints. An agent connected to it can propose, be refused with reasons, verify the hash
chain and replay a decision. It cannot reach a venue except through ``submit_governed_order``, and it
has no vocabulary at all for cancelling, replacing or closing (Hard Rule B4).

Together::

    agent  --MCP-->  MIZAN  --MCP-->  Alpaca  -->  paper venue
                       |
                       +-- risk engine, governor, authorization, hash-chained ledger

Nothing in this package is imported by the decision path, and nothing in it adds a dependency: the
protocol is newline-delimited JSON-RPC 2.0 and is implemented in the standard library, so Mizan's
exact pins on ``pydantic``/``jsonschema``/``PyYAML`` - which are recorded in every decision - cannot
be moved by an MCP install. See ``docs/MCP-INTERFACE.md``.
"""

from __future__ import annotations

from mizan.mcp.client import (
    PROTOCOL_VERSION,
    MCPError,
    MCPToolDenied,
    MCPToolResult,
    StdioMCPClient,
)
from mizan.mcp.server import SERVER_INFO, TOOLS, MizanMCPServer, serve_stdio
from mizan.mcp.session import MizanSession, SessionConfig, build_session

__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "TOOLS",
    "MCPError",
    "MCPToolDenied",
    "MCPToolResult",
    "MizanMCPServer",
    "MizanSession",
    "SessionConfig",
    "StdioMCPClient",
    "build_session",
    "serve_stdio",
]
