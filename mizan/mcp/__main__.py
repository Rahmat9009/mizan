"""``python -m mizan.mcp`` - serve Mizan's governed operations as MCP tools over stdio."""

from __future__ import annotations

from mizan.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
