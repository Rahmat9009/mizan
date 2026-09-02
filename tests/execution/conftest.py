"""Shared wiring for the execution-gate tests.

Everything here goes through the public API: the real risk engine, the real governor, the real
authorization module. Nothing stubs an enforcement decision - a gate test that mocks the engine proves
only that the mock was called.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pytest

from mizan import authorization, governor, risk
from mizan.adapters import BrokerContextProvider, MockBroker
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts import (
    ExecutionAuthorization,
    GovernorDecision,
    Policy,
    RiskContext,
    TradeProposal,
)
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch

from tests.fixtures import FIXED_NOW, make_context, make_policy, make_proposal


@dataclass
class Chain:
    """A complete, engine-produced object chain: what a real evaluation would have left behind."""

    policy: Policy
    context: RiskContext
    proposal: TradeProposal
    decision: GovernorDecision
    auth: ExecutionAuthorization


def build_chain(
    *,
    policy: Policy | None = None,
    context: RiskContext | None = None,
    proposal: TradeProposal | None = None,
    now: datetime = FIXED_NOW,
) -> Chain:
    policy = policy if policy is not None else make_policy()
    context = context if context is not None else make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = proposal if proposal is not None else make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", evaluation.reason_codes
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization.issue(decision, proposal, policy, now=now, context=context)
    return Chain(policy=policy, context=context, proposal=proposal, decision=decision, auth=auth)


class LoggingContextProvider:
    """The real BrokerContextProvider plus an event-log entry and a hook, for ordering assertions."""

    def __init__(
        self,
        broker: MockBroker,
        *,
        log: list[str] | None = None,
        on_build: Callable[[RiskContext], Any] | None = None,
        response_level: int = 0,
    ) -> None:
        self._inner = BrokerContextProvider(broker, response_level=response_level)
        self.log = log if log is not None else broker.log
        self.on_build = on_build
        self.built: list[RiskContext] = []

    def build(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        proposal: TradeProposal,
        policy: Policy,
        now: datetime,
        recent_orders: Sequence[Any] = (),
    ) -> RiskContext:
        context = self._inner.build(
            tenant_id=tenant_id,
            agent_id=agent_id,
            proposal=proposal,
            policy=policy,
            now=now,
            recent_orders=recent_orders,
        )
        self.built.append(context)
        self.log.append("context.build")
        if self.on_build is not None:
            self.on_build(context)
        return context


class LoggingKillSwitch:
    """A real InMemoryKillSwitch that writes every consultation to the shared event log.

    The log entry is what makes E4 testable: an outcome alone cannot tell a switch read at the
    mutation boundary from one read at request entry.
    """

    def __init__(self, *, active: bool = False, log: list[str] | None = None) -> None:
        self._inner = InMemoryKillSwitch(active=active)
        self.log = log if log is not None else []
        self.calls = 0

    def is_active(self) -> bool:
        self.calls += 1
        self.log.append("kill_switch")
        return self._inner.is_active()

    def activate(self) -> None:
        self._inner.activate()

    def deactivate(self) -> None:
        self._inner.deactivate()


@dataclass
class Wiring:
    """A gate and everything it was built from, so a test can inspect and script each part."""

    chain: Chain
    broker: MockBroker
    provider: LoggingContextProvider
    kill_switch: LoggingKillSwitch
    registry: InMemoryAuthorizationRegistry
    gate: ExecutionGate
    log: list[str] = field(default_factory=list)


def wire(
    chain: Chain,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
    on_build: Callable[[RiskContext], Any] | None = None,
    response_level: int = 0,
    broker: MockBroker | None = None,
    registry: InMemoryAuthorizationRegistry | None = None,
    kill_switch: LoggingKillSwitch | None = None,
) -> Wiring:
    log: list[str] = []
    if broker is None:
        broker = MockBroker(
            portfolio_snapshot=chain.context.portfolio_snapshot,
            market_snapshot=chain.context.market_snapshot,
            log=log,
        )
    else:
        broker.log = log
    provider = LoggingContextProvider(broker, log=log, on_build=on_build, response_level=response_level)
    switch = kill_switch if kill_switch is not None else LoggingKillSwitch(log=log)
    store = registry if registry is not None else InMemoryAuthorizationRegistry()
    gate = ExecutionGate(
        broker=broker,
        kill_switch=switch,
        registry=store,
        context_provider=provider,
        policy=chain.policy,
        config=ExecutionConfig(enabled=enabled, dry_run=dry_run),
        clock=clock if clock is not None else (lambda: FIXED_NOW),
    )
    return Wiring(
        chain=chain,
        broker=broker,
        provider=provider,
        kill_switch=switch,
        registry=store,
        gate=gate,
        log=log,
    )


def run(wiring: Wiring) -> Any:
    return wiring.gate.execute(wiring.chain.auth, wiring.chain.proposal, wiring.chain.decision)


def codes(result: Any) -> set[str]:
    return {str(getattr(code, "value", code)) for code in result.reason_codes}


@pytest.fixture
def chain() -> Chain:
    return build_chain()


# ---------------------------------------------------------------------------------------------
# The helpers above are reached through fixtures, never by importing this module: pytest runs with
# --import-mode=importlib and test modules must not import one another (pyproject.toml).
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def make_chain() -> Callable[..., Chain]:
    return build_chain


@pytest.fixture
def make_wiring() -> Callable[..., Wiring]:
    return wire


@pytest.fixture
def run_gate() -> Callable[[Wiring], Any]:
    return run


@pytest.fixture
def reason_codes() -> Callable[[Any], set[str]]:
    return codes


@pytest.fixture
def mock_broker() -> Callable[..., MockBroker]:
    return MockBroker
