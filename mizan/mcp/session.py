"""One place that assembles a governed :class:`~mizan.sdk.Mizan`, shared by the MCP server and the CLI.

Both surfaces must govern identically or the demonstration proves nothing: if the CLI's ``submit``
ran a different policy, a different ledger or a different broker from the MCP tool of the same name,
then whichever one a judge did not run would be untested. So there is exactly one builder, and the two
entry points differ only in how they are *spoken to*.

Three brokers, chosen explicitly and never inferred:

``mock``        in-memory, credential-free, fixed snapshots. The whole governance path - risk,
                governor, authorization, ledger, chain, replay - runs with no Alpaca key at all, which
                is what makes the reproducibility claim checkable by a third party.
``alpaca-mcp``  Alpaca's OFFICIAL MCP server, over stdio. The headline route.
``alpaca-py``   the pre-existing SDK adapter, kept so the two transports can be compared field by
                field against the same account.

There is no automatic fallback between them. A run that asked for ``alpaca-mcp`` and silently got
``mock`` would print a governance story about data that came from nowhere.

**Every field of :class:`SessionConfig` is explicit.** Nothing here is read from the ambient
environment - not the policy file, not the broker, not the ledger, not the server command. The reason
is narrow: the policy file decides WHICH CHECKS RUN, and the broker decides which venue is reached. An
inherited variable can set either without appearing in the command anyone typed, so a parent process
could change what a run enforces and leave nothing in shell history. An explicit ``--policy`` is
visible in the invocation; a ``MIZAN_POLICY_PATH`` is not, and that difference is the shape of finding
F-19.

Two variables remain anywhere in this package, and both can only make the system MORE restrictive:
``ALPACA_PAPER``, which must be explicitly true before any broker touching Alpaca is built, and
``MIZAN_KILL_SWITCH``, which boots the session with the switch already down. Credentials are read from
the environment where they are needed and are never stored (Hard Rule B2).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts import (
    AgentIdentity,
    MarketSnapshot,
    PortfolioSnapshot,
    format_ts,
)
from mizan.contracts.errors import ConfigurationError
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.policy import load_policy
from mizan.sdk import Mizan

__all__ = [
    "BROKER_CHOICES",
    "DEFAULT_AGENT",
    "BrokerChoice",
    "MizanSession",
    "SessionConfig",
    "build_session",
    "demo_market_snapshot",
    "demo_portfolio_snapshot",
]

BrokerChoice = Literal["mock", "alpaca-mcp", "alpaca-py"]
BROKER_CHOICES: tuple[BrokerChoice, ...] = ("mock", "alpaca-mcp", "alpaca-py")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policies" / "options-conservative.yaml"

DEFAULT_AGENT = AgentIdentity(
    agent_id="mizan-mcp-agent",
    agent_type="trader",
    agent_version="0.1.0",
    framework="custom",
)

#: A fixed instant for the credential-free broker, so the same command prints the same decision hash
#: on every machine. Determinism is the product, and a demo that drifts with the wall clock hides it.
DEMO_NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)
DEMO_AS_OF = format_ts(DEMO_NOW - timedelta(seconds=5))


@dataclass(frozen=True)
class SessionConfig:
    """Everything that decides what a governed call will do. Read from flags, then from environment."""

    tenant_id: str = "tenant-a"
    agent_id: str = DEFAULT_AGENT.agent_id
    policy_path: Path = DEFAULT_POLICY
    ledger_dir: Path | None = None
    broker: BrokerChoice = "mock"
    #: ``dry_run`` runs every gate and stops one step short of the mutation (WOULD_SUBMIT).
    dry_run: bool = True
    execution_enabled: bool = True
    timeout: float = 60.0
    #: argv for the Alpaca MCP server. ``None`` means "resolve it from what is installed".
    alpaca_mcp_command: tuple[str, ...] | None = None

    @classmethod
    def resolve(cls, **given: Any) -> SessionConfig:
        """Build a config from EXPLICIT values, letting the defaults stand where nothing was given.

        ``None`` means "the caller did not say", never "look it up somewhere". An unknown key is an
        error rather than silently ignored: a typo in a setting that decides which checks run must be
        loud, not absorbed.
        """
        known = {
            "tenant_id", "agent_id", "policy_path", "ledger_dir", "broker",
            "dry_run", "execution_enabled", "timeout", "alpaca_mcp_command",
        }
        unknown = sorted(set(given) - known)
        if unknown:
            raise ConfigurationError(message="Unknown session setting.", detail=f"unexpected: {unknown}")
        broker = given.get("broker") or "mock"
        if broker not in BROKER_CHOICES:
            raise ConfigurationError(
                message="Unknown broker.",
                detail=f"{broker!r} is not one of {list(BROKER_CHOICES)}",
            )
        policy_path = given.get("policy_path")
        ledger_dir = given.get("ledger_dir")
        command = given.get("alpaca_mcp_command")
        dry_run = given.get("dry_run")
        return cls(
            tenant_id=str(given.get("tenant_id") or "tenant-a"),
            agent_id=str(given.get("agent_id") or DEFAULT_AGENT.agent_id),
            policy_path=Path(policy_path) if policy_path else DEFAULT_POLICY,
            ledger_dir=Path(ledger_dir) if ledger_dir else None,
            broker=broker,
            dry_run=True if dry_run is None else bool(dry_run),
            execution_enabled=bool(given.get("execution_enabled", True)),
            timeout=float(given.get("timeout") or 60.0),
            alpaca_mcp_command=tuple(command) if command else None,
        )


@dataclass
class MizanSession:
    """A built pipeline plus whatever has to be shut down afterwards."""

    mizan: Mizan
    config: SessionConfig
    broker_name: str
    broker_environment: str
    closers: list[Callable[[], None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def close(self) -> None:
        for closer in self.closers:
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must not mask the caller's own failure
                pass
        self.closers.clear()

    def __enter__(self) -> MizanSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        return {
            "tenant_id": self.config.tenant_id,
            "agent_id": self.config.agent_id,
            "policy": {
                "policy_id": self.mizan.policy.policy_id,
                "version": self.mizan.policy.policy_version,
                "path": str(self.config.policy_path),
            },
            "broker": {"name": self.broker_name, "environment": self.broker_environment},
            "ledger": "sqlite" if self.config.ledger_dir else "memory",
            "ledger_dir": str(self.config.ledger_dir) if self.config.ledger_dir else None,
            "execution": {"enabled": self.config.execution_enabled, "dry_run": self.config.dry_run},
            "notes": list(self.notes),
        }


def build_session(config: SessionConfig | None = None) -> MizanSession:
    """Assemble the pipeline. The broker is whatever ``config.broker`` names, and never a fallback."""
    config = config or SessionConfig()
    policy = load_policy(_read_policy(config.policy_path))
    broker, closers, notes, clock = _build_broker(config)
    agent = AgentIdentity(
        agent_id=config.agent_id,
        agent_type=DEFAULT_AGENT.agent_type,
        agent_version=DEFAULT_AGENT.agent_version,
        framework=DEFAULT_AGENT.framework,
    )
    ledger = SqliteLedger(config.ledger_dir) if config.ledger_dir else InMemoryLedger()
    if config.ledger_dir:
        config.ledger_dir.mkdir(parents=True, exist_ok=True)
    # The one ambient variable this module honours, and it can only STOP trading. A session booted
    # into a tripped switch still runs every check and then refuses at the mutation boundary (E4),
    # which is what an operator who set it expects on every path - the MCP one included.
    kill_switch = InMemoryKillSwitch(active=_kill_switch_down())
    if kill_switch.is_active():
        notes.append("MIZAN_KILL_SWITCH is set: this session refuses at the mutation boundary")
    mizan = Mizan(
        tenant_id=config.tenant_id,
        agent=agent,
        policy=policy,
        broker=broker,
        ledger=ledger,
        kill_switch=kill_switch,
        config=ExecutionConfig(enabled=config.execution_enabled, dry_run=config.dry_run),
        clock=clock,
    )
    return MizanSession(
        mizan=mizan,
        config=config,
        broker_name=getattr(broker, "name", "unknown"),
        broker_environment=str(getattr(broker, "environment", "unknown")),
        closers=closers,
        notes=notes,
    )


def _build_broker(
    config: SessionConfig,
) -> tuple[Any, list[Callable[[], None]], list[str], Callable[[], datetime]]:
    if config.broker == "mock":
        from mizan.adapters import MockBroker

        broker = MockBroker(
            portfolio_snapshot=demo_portfolio_snapshot(),
            market_snapshot=demo_market_snapshot(),
        )
        note = "credential-free in-memory broker; market data is fixture data, not a live quote"
        return broker, [], [note], lambda: DEMO_NOW

    if config.broker == "alpaca-mcp":
        from mizan.mcp.alpaca import AlpacaMCPBroker

        mcp_broker = AlpacaMCPBroker.connect(timeout=config.timeout, command=config.alpaca_mcp_command)
        note = "reads and the one write go through Alpaca's official MCP server over stdio"
        return mcp_broker, [mcp_broker.close], [note], _utc_now

    from mizan.adapters import AlpacaPaperBroker

    sdk_broker = AlpacaPaperBroker.from_environment()
    return sdk_broker, [], ["direct alpaca-py SDK adapter (comparison path)"], _utc_now


def _read_policy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as failure:
        raise ConfigurationError(
            message="The policy file could not be read.",
            detail=f"{path}: {failure}",
        ) from failure


def _kill_switch_down() -> bool:
    """``MIZAN_KILL_SWITCH`` - the project's stop-everything variable. Absent means "not tripped"."""
    raw = os.getenv("MIZAN_KILL_SWITCH")
    return raw is not None and raw.strip().casefold() in {"1", "true", "yes", "on"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------------
# The credential-free world. Illustrative test data for a governance demonstration; nothing here is a
# recommendation and no return figure appears anywhere (Hard Rules B5/B6).
# ---------------------------------------------------------------------------------------------------
def demo_market_snapshot() -> MarketSnapshot:
    return MarketSnapshot.build(
        as_of=DEMO_AS_OF,
        quotes={
            "AAPL": {
                "symbol": "AAPL",
                "price": "228.5",
                "bid": "228.45",
                "ask": "228.55",
                "as_of": DEMO_AS_OF,
                "source": "demo:quotes",
                "adv": "55000000",
                "spread_pct": "0.0004",
            },
            "SPY": {
                "symbol": "SPY",
                "price": "560.1",
                "bid": "560.05",
                "ask": "560.15",
                "as_of": DEMO_AS_OF,
                "source": "demo:quotes",
                "adv": "70000000",
                "spread_pct": "0.0002",
            },
        },
        option_quotes={
            "AAPL260925C00230000": {
                "occ_symbol": "AAPL260925C00230000",
                "mark": "1.85",
                "delta": "0.168",
                "gamma": "0.021",
                "vega": "0.142",
                "theta": "-0.061",
                "as_of": DEMO_AS_OF,
                "source": "demo:options",
                "open_interest": 4200,
                "spread_pct": "0.027",
                "iv": "0.284",
            },
            "AAPL260925C00235000": {
                "occ_symbol": "AAPL260925C00235000",
                "mark": "0.95",
                "delta": "0.101",
                "gamma": "0.016",
                "vega": "0.118",
                "theta": "-0.048",
                "as_of": DEMO_AS_OF,
                "source": "demo:options",
                "open_interest": 3100,
                "spread_pct": "0.031",
                "iv": "0.276",
            },
        },
        sectors={"AAPL": "Technology", "SPY": "Index"},
        source="demo",
    )


def demo_portfolio_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot.model_validate(
        {
            "snapshot_id": "demo-pf-1",
            "as_of": DEMO_AS_OF,
            "equity": "100000",
            "cash": "79395",
            "buying_power": "158790",
            "peak_equity": "105000",
            "daily_pnl": "-250",
            "positions": [],
            "greeks": {"delta": "0", "gamma": "0", "vega": "0"},
            "source": "demo:account",
        }
    )
