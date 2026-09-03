"""One assembled Mizan deployment, built from real components, for the integration suite.

WHAT IS REAL HERE (everything except one thing):

* the policy is a **YAML document** run through ``mizan.policy.load_policy`` and
  ``mizan.policy.validate_policy`` — the same path a deployment's policy file takes, not a fixture
  object handed straight to the engine, so the loader and the hash are under test too;
* the risk engine, the advisory layer, the governor, the authorization mint, the ledger, the
  execution gate, decision replay and chain verification are the shipped implementations, reached
  through ``mizan.sdk.Mizan`` — the public entry point a developer uses;
* the ledger is a **real** ``SqliteLedger`` on disk wherever a test asks for one, so the append-only
  triggers and the per-tenant file boundary are exercised rather than assumed.

THE ONE MOCK, AND WHY (§ "MOCKS IN USE" of the lane report):

* :class:`mizan.adapters.MockBroker` stands in for the venue. A test process has no brokerage to
  integrate against; the alternative is a network call to Alpaca with credentials, which the build
  forbids. ``MockBroker`` is a **shipped adapter** implementing the same ``BrokerAdapter`` protocol
  as ``AlpacaPaperBroker``, not a stub invented in this suite, and it logs every call so a test can
  assert the *order* of the gate's reads and not merely their outcome.

Nothing here patches, monkeypatches or subclasses a Mizan internal in order to reach a branch. The
only subclass is :class:`RecordingKillSwitch`, and it exists solely to write its reads into the same
log the broker writes to — the read itself is ``InMemoryKillSwitch``'s, unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mizan.adapters import MockBroker
from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts import (
    AgentIdentity,
    MarketSnapshot,
    ModelIdentity,
    Policy,
    PortfolioSnapshot,
    TradeProposal,
    format_ts,
)
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.policy import load_policy, validate_policy
from mizan.sdk import Mizan

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

#: A fixed instant. Determinism is the product, so nothing in this suite reads a wall clock.
NOW = datetime(2026, 9, 2, 17, 40, tzinfo=UTC)
AS_OF = format_ts(NOW - timedelta(seconds=5))

AGENT_A = AgentIdentity(
    agent_id="integration-trader-01",
    agent_type="trader",
    agent_version="1.0.0",
    framework="tradingagents",
)
AGENT_B = AgentIdentity(
    agent_id="integration-trader-02",
    agent_type="trader",
    agent_version="1.0.0",
    framework="tradingagents",
)
MODEL = ModelIdentity(provider="featherless", model="qwen3-32b", version="2026-06", prompt_hash="0" * 64)

#: The baseline policy document. ``position_limit`` is a WARNING, which is what makes a REDUCE
#: reachable deterministically: a breached warning caps the order, a breached blocking rejects it
#: (see ledger/requests.md REQ-10). max_quantity 20 is the cap every REDUCE test lands on.
POLICY_YAML = """
schema_version: "1.0.0"
policy_id: integration-equity
policy_version: "1.0.0"
tenant_id: {tenant_id}
order: {{max_notional: "10000", max_quantity: "20", max_legs: 1}}
portfolio:
  max_single_symbol_pct: "0.15"
  max_sector_concentration_pct: "0.40"
  max_drawdown_pct: "0.20"
  max_buying_power_utilization: "0.80"
options: null
restricted: {{symbols: ["GME"], strategies: []}}
checks:
  position_limit: {{enabled: true, severity: warning}}
advisory: {{enabled: false, profile: "offline", authority_ceiling: "reduce_or_reject"}}
authorization: {{ttl_seconds: {ttl_seconds}}}
fail_closed: {{on_advisory_unavailable: false}}
"""

#: The same document with ``position_limit`` BLOCKING: the identical proposal now REJECTs. Used to
#: prove that the refusal is the policy's doing and not a property of the proposal.
STRICT_POLICY_YAML = POLICY_YAML.replace("severity: warning", "severity: blocking")


def policy_for(tenant_id: str = TENANT_A, *, ttl_seconds: int = 15, strict: bool = False) -> Policy:
    """Load and validate a policy document exactly as a deployment would."""
    source = STRICT_POLICY_YAML if strict else POLICY_YAML
    return validate_policy(load_policy(source.format(tenant_id=tenant_id, ttl_seconds=ttl_seconds)))


def market_snapshot(**overrides: Any) -> MarketSnapshot:
    """What the broker's data feed returns. AAPL and MSFT are both Technology, deliberately."""
    base: dict[str, Any] = {
        "snapshot_id": "mkt-integration-1",
        "as_of": AS_OF,
        "quotes": {
            "AAPL": {
                "symbol": "AAPL", "price": "228.5", "bid": "228.45", "ask": "228.55",
                "as_of": AS_OF, "source": "integration:quotes",
                "adv": "55000000", "spread_pct": "0.0004",
            },
            "MSFT": {
                "symbol": "MSFT", "price": "412.1", "bid": "412", "ask": "412.2",
                "as_of": AS_OF, "source": "integration:quotes",
                "adv": "21000000", "spread_pct": "0.0005",
            },
        },
        "option_quotes": {},
        "sectors": {"AAPL": "Technology", "MSFT": "Technology"},
        "source": "integration",
    }
    return MarketSnapshot.model_validate({**base, **overrides})


def portfolio_snapshot(**overrides: Any) -> PortfolioSnapshot:
    """What the broker's account endpoint returns."""
    base: dict[str, Any] = {
        "snapshot_id": "pf-integration-1",
        "as_of": AS_OF,
        "equity": "100000",
        "cash": "79395",
        "buying_power": "158790",
        "peak_equity": "105000",
        "daily_pnl": "-250",
        "positions": [
            {
                "symbol": "MSFT", "asset_class": "equity", "quantity": "50",
                "market_value": "20605", "sector": "Technology", "occ_symbol": None,
                "delta": "50", "gamma": "0", "vega": "0",
            }
        ],
        "greeks": {"delta": "50", "gamma": "0", "vega": "0"},
        "source": "integration:account",
    }
    return PortfolioSnapshot.model_validate({**base, **overrides})


def starved_portfolio(buying_power: str = "1000") -> PortfolioSnapshot:
    """The same account after its buying power collapsed. Used for the TOCTOU race."""
    return portfolio_snapshot(snapshot_id="pf-integration-2", buying_power=buying_power, cash=buying_power)


def proposal(
    quantity: str = "10",
    *,
    agent: AgentIdentity | None = None,
    symbol: str = "AAPL",
    reasoning: str = "",
    intent: str = "open",
) -> TradeProposal:
    """BUY <quantity> <symbol> shares, limit 228.50, expiring five minutes from ``NOW``."""
    return TradeProposal.build(
        agent=agent if agent is not None else AGENT_A,
        model=MODEL,
        created_at=format_ts(NOW),
        expires_at=format_ts(NOW + timedelta(minutes=5)),
        intent=intent,
        symbol=symbol,
        asset_class="equity",
        strategy="long_equity",
        legs=[
            {
                "leg_index": 0, "side": "buy", "contract_type": None, "strike": None,
                "expiry": None, "quantity": quantity, "limit_price": "228.50",
                "order_type": "limit",
            }
        ],
        reasoning=reasoning,
        market_snapshot_ref="mkt-integration-1",
        portfolio_snapshot_ref="pf-integration-1",
    )


class Clock:
    """A hand-wound clock. ``advance`` is how a test makes a TTL run out without sleeping."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class RecordingKillSwitch(InMemoryKillSwitch):
    """``InMemoryKillSwitch`` that writes each read into the broker's call log.

    The behaviour under test is unchanged — ``is_active`` still calls up to the real implementation.
    Sharing one log with the broker is the only way to assert Hard Rule E4 as an ORDERING fact: the
    switch must be read AFTER the gate's last broker read and immediately BEFORE the mutation.
    """

    def __init__(self, log: list[str], *, active: bool = False) -> None:
        super().__init__(active=active)
        self.log = log

    def is_active(self) -> bool:
        self.log.append("kill_switch.is_active")
        return super().is_active()


@dataclass
class World:
    """One tenant's assembled deployment plus the handles a test needs to perturb it."""

    mizan: Mizan
    broker: MockBroker
    kill_switch: RecordingKillSwitch
    clock: Clock
    log: list[str] = field(default_factory=list)

    @property
    def submitted(self) -> list[Any]:
        return self.broker.submitted

    def codes(self, obj: Any) -> list[str]:
        return [str(getattr(code, "value", code)) for code in obj.reason_codes]


def build_world(
    *,
    tenant_id: str = TENANT_A,
    agent: AgentIdentity | None = None,
    policy: Policy | None = None,
    ledger: Any | None = None,
    ledger_dir: Path | None = None,
    dry_run: bool = False,
    enabled: bool = True,
    ttl_seconds: int = 15,
    strict: bool = False,
    kill_switch_active: bool = False,
    portfolio: PortfolioSnapshot | None = None,
    market: MarketSnapshot | None = None,
    clock: Clock | None = None,
    hooks: dict[str, Callable[[], Any]] | None = None,
) -> World:
    """Assemble a complete, real pipeline. ``ledger_dir`` selects the on-disk SQLite ledger."""
    log: list[str] = []
    broker = MockBroker(
        portfolio_snapshot=portfolio if portfolio is not None else portfolio_snapshot(),
        market_snapshot=market if market is not None else market_snapshot(),
        log=log,
        **(hooks or {}),
    )
    switch = RecordingKillSwitch(log, active=kill_switch_active)
    the_clock = clock if clock is not None else Clock()
    if ledger is None:
        ledger = SqliteLedger(root_dir=ledger_dir) if ledger_dir is not None else InMemoryLedger()
    pipeline = Mizan(
        tenant_id=tenant_id,
        agent=agent if agent is not None else AGENT_A,
        policy=(
            policy
            if policy is not None
            else policy_for(tenant_id, ttl_seconds=ttl_seconds, strict=strict)
        ),
        broker=broker,
        ledger=ledger,
        advisory=None,
        kill_switch=switch,
        config=ExecutionConfig(enabled=enabled, dry_run=dry_run),
        clock=the_clock,
    )
    return World(mizan=pipeline, broker=broker, kill_switch=switch, clock=the_clock, log=log)
