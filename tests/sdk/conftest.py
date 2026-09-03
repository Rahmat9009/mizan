"""Shared doubles and builders for the SDK suite.

Two rules the doubles here follow, so that a green test means something:

* nothing bypasses a contract. Every object handed to ``Mizan`` is built through ``tests.fixtures``,
  which builds through the frozen models, so a test cannot accidentally prove a property of a shape
  the engine would never see;
* the execution gate is faked, not stubbed out. ``FakeGate`` has the same three-argument ``execute``
  as ``mizan.execution.ExecutionGate`` and records what it was handed, so the SDK's delegation - which
  authorization, which proposal, which decision, under which policy - is asserted rather than assumed.
  It is a **mock, to be swapped for the real gate at the next checkpoint**: L3a was implementing
  ``ExecutionGate.execute`` while this suite was written, and a test that waits on another lane is a
  test that does not run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from mizan.adapters import MockBroker
from mizan.audit import InMemoryLedger
from mizan.contracts import ExecutionResult
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    make_execution_result,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

__all__ = [
    "FakeGate",
    "StepClock",
    "reducing_policy",
]


class StepClock:
    """A clock that walks a scripted list of instants and then stays at the last one.

    Time is an input everywhere in Mizan, so "an authorization expired between the decision and the
    submission" is expressed by advancing this, never by sleeping.
    """

    def __init__(self, *instants: datetime) -> None:
        self.instants = list(instants) or [FIXED_NOW]
        self.calls = 0

    def __call__(self) -> datetime:
        index = min(self.calls, len(self.instants) - 1)
        self.calls += 1
        return self.instants[index]


class FakeGate:
    """A stand-in for ``ExecutionGate`` that records its arguments and returns a scripted status."""

    def __init__(self, status: str = "WOULD_SUBMIT", reason_codes: list[Any] | None = None) -> None:
        self.status = status
        self.reason_codes = list(reason_codes or [])
        self.calls: list[tuple[Any, Any, Any]] = []

    def execute(self, auth: Any, proposal: Any, decision: Any) -> ExecutionResult:
        self.calls.append((auth, proposal, decision))
        return make_execution_result(
            authorization=auth,
            status=self.status,
            reason_codes=self.reason_codes,
            broker={"name": "fake", "environment": "paper"},
        )


def reducing_policy(*, max_quantity: str = "4", **overrides: Any):
    """A policy whose position limit is a WARNING, so breaching it REDUCEs instead of REJECTing.

    Severity decides shape (ledger REQ-10): the same limit configured ``blocking`` would produce a
    REJECT with recommended_quantity "0". This is the only way to exercise the reduce path end to end
    without hand-building a decision, which would prove nothing about the engine.
    """
    base = make_policy()
    checks = {key: value.model_dump(mode="json") for key, value in base.checks.items()}
    checks["position_limit"] = {"enabled": True, "severity": "warning"}
    fields: dict[str, Any] = {
        "order": {"max_notional": "10000.00", "max_quantity": max_quantity, "max_legs": 4},
        "checks": checks,
    }
    fields.update(overrides)
    return make_policy(**fields)


@pytest.fixture
def broker() -> MockBroker:
    return MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )


@pytest.fixture
def proposal():
    return make_proposal()


@pytest.fixture
def policy():
    return make_policy()


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def build_pipeline(broker, proposal, ledger):
    """Factory for a ``Mizan`` bound to tenant-a, with every collaborator overridable."""

    def build(**overrides: Any) -> Mizan:
        fields: dict[str, Any] = {
            "tenant_id": TENANT_A,
            "agent": proposal.agent,
            "policy": make_policy(),
            "broker": broker,
            "ledger": ledger,
            "advisory": None,
            "kill_switch": InMemoryKillSwitch(),
            "config": ExecutionConfig(enabled=True, dry_run=True),
            "clock": lambda: FIXED_NOW,
        }
        fields.update(overrides)
        return Mizan(**fields)

    return build


@pytest.fixture
def pipeline(build_pipeline) -> Mizan:
    return build_pipeline()


@pytest.fixture
def much_later() -> datetime:
    """Well past any authorization TTL (which the contract caps at 30 seconds)."""
    return FIXED_NOW + timedelta(hours=1)
