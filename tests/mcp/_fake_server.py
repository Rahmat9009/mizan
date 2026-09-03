"""A minimal, deliberately awkward MCP server, spawned as a subprocess by tests/mcp/test_client.py.

It is not a test module and is never imported: the client's whole job is to talk to a *separate
process* over pipes, so testing it against an in-process double would test nothing that matters. It is
run as a script - ``python tests/mcp/_fake_server.py <behaviour>`` - and each behaviour reproduces one
way a real server misbehaves.
"""

from __future__ import annotations

import json
import sys
import time

PROTOCOL_VERSION = "2025-06-18"


def write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(behaviour: str) -> int:
    if behaviour == "noise":
        # Real servers print banners. A client that cannot survive one deadlocks on a full pipe.
        print("=== a banner nobody asked for ===", file=sys.stderr, flush=True)
        sys.stdout.write("not json at all\n")
        sys.stdout.flush()
    if behaviour == "die-on-start":
        return 1

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        message = json.loads(text)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake", "version": "9.9.9"},
                    },
                }
            )
            continue
        if request_id is None:
            continue  # a notification: say nothing

        if method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            if behaviour == "paged" and cursor is None:
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "tools": [{"name": "first", "inputSchema": {"type": "object"}}],
                            "nextCursor": "page-2",
                        },
                    }
                )
            else:
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"tools": [{"name": "second", "inputSchema": {"type": "object"}}]},
                    }
                )
            continue

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name == "structured":
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [], "structuredContent": {"from": "structuredContent"}},
                    }
                )
            elif name == "explodes":
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": "it went wrong"}],
                            "isError": True,
                        },
                    }
                )
            elif name == "slow":
                time.sleep(30)
            elif name == "chatty":
                # A notification arriving before the answer must not be mistaken for the answer.
                write({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": '{"ok": true}'}]},
                    }
                )
            else:
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {"type": "image", "data": "ignored"},
                                {"type": "text", "text": json.dumps(params.get("arguments") or {})},
                            ]
                        },
                    }
                )
            continue

        write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "plain"))
