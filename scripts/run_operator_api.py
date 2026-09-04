"""Run the authoritative Mizan REST API for the local operator frontend.

This is assembly only: it creates the existing ``Mizan`` SDK object and passes it to
``mizan.api.create_app``. Broker choice is explicit and there is no mock fallback.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from mizan.adapters import AlpacaPaperBroker
from mizan.api import ApiConfig, Principal, StaticTokenStore, create_app
from mizan.audit import SqliteLedger
from mizan.contracts import AgentIdentity
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.policy import load_policy
from mizan.sdk import Mizan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Mizan's authenticated paper-only REST API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--agent", default="mizan-operator-api")
    parser.add_argument("--policy", type=Path, default=Path("policies/options-conservative.yaml"))
    parser.add_argument("--ledger", type=Path, default=Path("data/ledger"))
    parser.add_argument("--broker", choices=("none", "alpaca-py"), default="none")
    parser.add_argument("--cors-origin", action="append", default=["http://localhost:5173"])
    return parser


def _required_token() -> str:
    token = (os.getenv("MIZAN_API_TOKEN") or "").strip()
    if len(token) < 16:
        raise SystemExit("MIZAN_API_TOKEN must be set to at least 16 characters.")
    return token


def _kill_switch_active() -> bool:
    raw = (os.getenv("MIZAN_KILL_SWITCH") or "").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    args = _parser().parse_args()
    token = _required_token()
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
        scopes=frozenset({"read", "control"}),
    )
    app = create_app(
        pipeline,
        tokens=StaticTokenStore({token: principal}),
        config=ApiConfig(cors_origins=tuple(dict.fromkeys(args.cors_origin))),
    )
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
