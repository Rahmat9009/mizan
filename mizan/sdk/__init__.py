"""L3 — the developer-facing SDK. The ten lines that put an agent behind the gate.

The target developer experience from Master Plan section 7:

    mizan = Mizan(tenant_id=..., agent=..., policy=..., broker=...)

    @mizan.protected
    def submit_trade(order):
        broker.submit(order)

``evaluate`` runs the whole decision plane — context, deterministic risk, advisory, governor,
authorization, ledger append — and returns the DecisionRecord. It never executes. ``execute`` is a
separate call because approving and acting are separate authorities (Barings, R-BLOW-3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from mizan.contracts import (
    AgentIdentity,
    DecisionRecord,
    ExecutionResult,
    Policy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mizan.adapters import BrokerAdapter
    from mizan.advisory import AdvisoryProvider
    from mizan.audit import ChainVerification, Ledger
    from mizan.execution import ExecutionConfig, KillSwitch
    from mizan.replay import ReplayResult

__all__ = ["Mizan"]


class Mizan:
    """One tenant's governed pipeline, assembled from the lane components."""

    def __init__(
        self,
        *,
        tenant_id: str,
        agent: AgentIdentity,
        policy: Policy | str,
        broker: "BrokerAdapter | None" = None,
        ledger: "Ledger | None" = None,
        advisory: "AdvisoryProvider | None" = None,
        kill_switch: "KillSwitch | None" = None,
        config: "ExecutionConfig | None" = None,
        clock: Callable[[], "datetime"] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.agent = agent
        self.policy = policy
        self.broker = broker
        self.ledger = ledger
        self.advisory = advisory
        self.kill_switch = kill_switch
        self.config = config
        self.clock = clock

    def evaluate(self, proposal: Any) -> DecisionRecord:
        """Govern a proposal and record the decision. Never executes."""
        raise NotImplementedError("L3 implements this in Sprint 2")

    def execute(self, decision_id: str) -> ExecutionResult:
        raise NotImplementedError("L3 implements this in Sprint 2")

    def protected(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a submit function so it runs only behind an approved, authorized decision."""
        raise NotImplementedError("L3 implements this in Sprint 2")

    def replay(self, decision_id: str, **kwargs: Any) -> "ReplayResult":
        raise NotImplementedError("L3 implements this in Sprint 2")

    def verify_chain(self) -> "ChainVerification":
        raise NotImplementedError("L3 implements this in Sprint 2")

    def get_decision(self, decision_id: str) -> DecisionRecord:
        raise NotImplementedError("L3 implements this in Sprint 2")
