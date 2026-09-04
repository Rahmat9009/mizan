"""Run the authoritative Mizan REST API for the local operator frontend.

This is assembly only: it creates the existing ``Mizan`` SDK object and passes it to
``mizan.api.create_app``. Broker choice is explicit and there is no mock fallback.
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mizan.adapters import AlpacaPaperBroker
from mizan.api import ApiConfig, Principal, StaticTokenStore, create_app
from mizan.audit import SqliteLedger
from mizan.contracts import AgentIdentity
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.policy import load_policy
from mizan.sdk import Mizan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Mizan's authenticated paper-only REST API.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--agent", default="mizan-operator-api")
    parser.add_argument("--policy", type=Path, default=Path("policies/options-conservative.yaml"))
    parser.add_argument("--ledger", type=Path, default=Path("data/ledger"))
    parser.add_argument("--broker", choices=("none", "alpaca-py"), default="none")
    parser.add_argument("--cors-origin", action="append", default=["http://localhost:5173"])
    parser.add_argument(
        "--serve-frontend",
        type=Path,
        help="Serve a built Vite directory and expose its read-only same-origin /api gateway.",
    )
    return parser


def _required_token(*, generate_if_missing: bool = False) -> str:
    token = (os.getenv("MIZAN_API_TOKEN") or "").strip()
    if not token and generate_if_missing:
        # The production browser gateway and API share this process, so an
        # unlogged, per-process credential is stronger than a deployed static
        # value and never has to leave the service.
        return secrets.token_urlsafe(32)
    if len(token) < 16:
        raise SystemExit("MIZAN_API_TOKEN must be set to at least 16 characters.")
    return token


def _kill_switch_active() -> bool:
    raw = (os.getenv("MIZAN_KILL_SWITCH") or "").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


class _SameOriginApiGateway:
    """Rewrite /api to /v1 and attach the process-local bearer server-side."""

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self.authorization = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return

        forwarded = dict(scope)
        forwarded_path = path.removeprefix("/api") or "/"
        forwarded["path"] = forwarded_path
        forwarded["raw_path"] = forwarded_path.encode("utf-8")
        headers = [
            (key, value)
            for key, value in scope.get("headers", ())
            if key.lower() != b"authorization"
        ]
        headers.append((b"authorization", self.authorization))
        forwarded["headers"] = headers
        await self.app(forwarded, receive, send)


def _serve_frontend(app: Any, directory: Path, *, token: str) -> None:
    frontend = directory.resolve()
    index = frontend / "index.html"
    if not index.is_file():
        raise SystemExit(f"Frontend build not found: {index}")

    app.add_middleware(_SameOriginApiGateway, token=token)
    assets = frontend / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def spa_root() -> Any:
        return FileResponse(index)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str) -> Any:
        # Unknown API URLs must remain API 404s, never successful HTML.
        if path == "api" or path.startswith(("api/", "v1/")):
            return JSONResponse(status_code=404, content={"error": {"code": "not_found"}})
        try:
            candidate = (frontend / path).resolve()
            if candidate.is_file() and (candidate == frontend or frontend in candidate.parents):
                return FileResponse(candidate)
        except Exception:
            pass
        return FileResponse(index)


def _build_app(args: argparse.Namespace, *, token: str | None = None) -> Any:
    token = token or _required_token(generate_if_missing=args.serve_frontend is not None)
    # ExecutionConfig itself proves that ALPACA_PAPER=true was explicit and fails closed otherwise.
    execution = ExecutionConfig.from_environment()
    broker = AlpacaPaperBroker.from_environment() if args.broker == "alpaca-py" else None
    policy = load_policy(args.policy.read_text(encoding="utf-8"))
    agent = AgentIdentity(
        agent_id=args.agent,
        agent_type="portfolio_manager",
        agent_version="1.0.0",
        framework="custom",
    )
    pipeline = Mizan(
        tenant_id=args.tenant,
        agent=agent,
        policy=policy,
        broker=broker,
        ledger=SqliteLedger(args.ledger),
        kill_switch=InMemoryKillSwitch(active=_kill_switch_active()),
        config=execution,
    )
    principal = Principal(
        token_id="operator-ui",
        tenant_id=args.tenant,
        agent=agent,
        # A public production demo is deliberately read-only. Local API-only
        # operation retains the existing in-memory control surface.
        scopes=frozenset({"read"}) if args.serve_frontend else frozenset({"read", "control"}),
    )
    app = create_app(
        pipeline,
        tokens=StaticTokenStore({token: principal}),
        config=ApiConfig(cors_origins=tuple(dict.fromkeys(args.cors_origin))),
    )
    if args.serve_frontend:
        _serve_frontend(app, args.serve_frontend, token=token)
    return app


def main() -> int:
    args = _parser().parse_args()
    app = _build_app(args)
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
