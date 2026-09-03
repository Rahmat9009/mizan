"""L3 — the developer-facing SDK. The ten lines that put an agent behind the gate.

The target developer experience from Master Plan section 7:

    mizan = Mizan(tenant_id=..., agent=..., policy=..., broker=...)

    @mizan.protected
    def submit_trade(order):
        broker.submit(order)

``evaluate`` runs the whole decision plane — context, deterministic risk, advisory, governor,
authorization, ledger append — and returns the DecisionRecord. It never executes. ``execute`` is a
separate call because approving and acting are separate authorities (Barings, R-BLOW-3).

Two boundaries this class holds and never relaxes:

* **the market data is the broker's, not the caller's** (F-1/F-2). ``evaluate`` takes a proposal and
  nothing else; prices, positions and buying power are read through the context provider. A caller
  cannot hand in the numbers their order will be judged against;
* **the agent identity is the instance's, not the payload's**. A proposal claiming another agent is
  refused rather than governed under that agent's budget.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mizan import advisory as _advisory
from mizan import authorization as _authorization
from mizan import governor as _governor
from mizan import replay as _replay
from mizan import risk as _risk
from mizan.adapters import BrokerContextProvider
from mizan.audit import InMemoryLedger
from mizan.contracts import (
    AgentIdentity,
    DecisionRecord,
    ExecutionResult,
    Policy,
    ReasonCode,
    TradeProposal,
)
from mizan.contracts.errors import (
    ConfigurationError,
    ExecutionBlocked,
    NotFound,
    ValidationFailed,
)
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch
from mizan.policy import load_policy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mizan.adapters import BrokerAdapter, ContextProvider
    from mizan.advisory import AdvisoryProvider
    from mizan.audit import ChainVerification, Ledger, TenantLedger
    from mizan.execution import KillSwitch
    from mizan.replay import ReplayResult

__all__ = ["EXECUTABLE_STATUSES", "Mizan", "authorized_proposal"]

#: Statuses that mean "the gate agreed". Everything else is a refusal, including a broker failure:
#: an unknown outcome is never treated as a success.
EXECUTABLE_STATUSES = frozenset({"SUBMITTED", "WOULD_SUBMIT", "RECONCILED_EXISTING"})


class Mizan:
    """One tenant's governed pipeline, assembled from the lane components."""

    def __init__(
        self,
        *,
        tenant_id: str,
        agent: AgentIdentity,
        policy: Policy | str,
        broker: BrokerAdapter | None = None,
        ledger: Ledger | None = None,
        advisory: AdvisoryProvider | None = None,
        kill_switch: KillSwitch | None = None,
        config: ExecutionConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.agent = agent
        self.policy = policy if isinstance(policy, Policy) else load_policy(policy)
        if self.policy.tenant_id != tenant_id:
            raise ConfigurationError(
                message="The policy does not belong to this tenant.",
                reason_codes=[ReasonCode.TENANT_MISMATCH],
                detail=f"policy tenant {self.policy.tenant_id!r} != {tenant_id!r}",
            )
        self.broker = broker
        self.ledger: Ledger = ledger if ledger is not None else InMemoryLedger()
        self.advisory = advisory
        self.kill_switch: KillSwitch = kill_switch if kill_switch is not None else InMemoryKillSwitch()
        self.config = config if config is not None else ExecutionConfig()
        self.clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self.registry = _authorization.InMemoryAuthorizationRegistry()
        self.context_provider: ContextProvider | None = (
            BrokerContextProvider(broker) if broker is not None else None
        )
        # Execution results are not part of the hash chain: a DecisionRecord is immutable by
        # construction (A2), so an outcome learned after the append cannot be written back into it.
        # They are indexed here and surfaced by get_execution. See ledger/requests.md REQ-9.
        self._executions: dict[str, ExecutionResult] = {}
        self._lock = threading.Lock()

    # -- time -------------------------------------------------------------------------------------
    def now(self) -> datetime:
        """The instance's clock. Injectable so that a demo, a test and a replay all agree on 'now'."""
        return self.clock()

    # -- the decision plane -------------------------------------------------------------------------
    def evaluate(self, proposal: TradeProposal) -> DecisionRecord:
        """Govern a proposal and record the decision. Never executes.

        Context, deterministic risk, advisory, governor, authorization, ledger append - in that order,
        with the advisory able only to reduce or reject (E1) and the authorization minted only for a
        decision that survived. The returned record is the whole story, and it is already chained.
        """
        self._require_agent(proposal)
        provider = self._require_context_provider()
        now = self.now()
        context = provider.build(
            tenant_id=self.tenant_id,
            agent_id=self.agent.agent_id,
            proposal=proposal,
            policy=self.policy,
            now=now,
        )
        evaluation = _risk.evaluate(proposal, context, self.policy)
        opinion = _advisory.get_advisory(self.advisory, proposal, evaluation, context, self.policy)
        decision = _governor.govern(proposal, evaluation, self.policy, opinion, context=context)
        authorization = (
            None
            if decision.verdict == "REJECT"
            else _authorization.issue(decision, proposal, self.policy, now=now, context=context)
        )
        return self._tenant_ledger().append(
            proposal=proposal,
            risk_context=context,
            risk_evaluation=evaluation,
            governor_decision=decision,
            policy_snapshot=self.policy,
            authorization=authorization,
            recorded_at=now,
        )

    def execute(self, decision_id: str) -> ExecutionResult:
        """Run the recorded authorization through the gate. The only way to reach the broker (E3)."""
        record = self.get_decision(decision_id)
        if record.authorization is None:
            raise ExecutionBlocked(
                message="This decision carries no authorization to execute.",
                reason_codes=[ReasonCode.AUTHORIZATION_INVALID],
                detail=f"decision {decision_id} verdict {record.verdict}",
            )
        gate = ExecutionGate(
            broker=self._require_broker(),
            kill_switch=self.kill_switch,
            registry=self.registry,
            context_provider=self._require_context_provider(),
            policy=self.policy,
            config=self.config,
            clock=self.clock,
        )
        result = gate.execute(record.authorization, record.proposal, record.governor_decision)
        with self._lock:
            self._executions[decision_id] = result
        return result

    def protected(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        on_decision: Callable[[DecisionRecord], None] | None = None,
    ) -> Callable[..., Any]:
        """Wrap a submit function so it runs only behind an approved, authorized decision.

            @mizan.protected
            def submit_trade(proposal):
                broker.submit(proposal)

        The wrapped function receives whatever the caller passed; its first argument must be the
        ``TradeProposal``. Mizan evaluates it, executes the authorization through the gate, and calls
        the function **only** if the gate reached an executable status - otherwise it raises
        ``ExecutionBlocked`` carrying the reason codes and never calls the function at all.

        Requires ``ExecutionConfig(dry_run=True)`` (the default), because in this mode the wrapped
        function is the thing that places the order: the gate runs every check and stops one step short
        of the mutation, and the caller's code performs it. With ``dry_run=False`` the gate submits
        through Mizan's own broker, so calling the function as well would place the order **twice** —
        that combination is refused rather than documented.

        The function receives the **authorized** proposal, not the one the agent asked for. When the
        governor reduced the size, the object handed down carries the reduced leg quantities, so an
        agent cannot get its original quantity submitted by ignoring the verdict; the reduction is
        applied to the thing that reaches the broker, not merely reported alongside it.

        ``on_decision`` is called with the DecisionRecord for every evaluation, approved or refused, so
        a caller can log or ship the decision id without changing its own function's signature.
        """
        if fn is None:
            return functools.partial(self.protected, on_decision=on_decision)

        @functools.wraps(fn)
        def guarded(proposal: TradeProposal, *args: Any, **kwargs: Any) -> Any:
            record = self.evaluate(proposal)
            if on_decision is not None:
                on_decision(record)
            if record.verdict == "REJECT" or record.authorization is None:
                raise ExecutionBlocked(
                    message="The proposal was rejected by policy; nothing was submitted.",
                    reason_codes=list(record.reason_codes),
                    detail=f"decision {record.decision_id} verdict {record.verdict}",
                )
            result = self.execute(record.decision_id)
            if result.status == "SUBMITTED":
                # The gate already placed this order through Mizan's broker. Running the caller's
                # submit function too would send a second, unauthorized order for the same decision.
                raise ConfigurationError(
                    message="@protected requires a dry-run execution config; the gate already submitted.",
                    detail="ExecutionConfig(dry_run=False) with @protected would double-submit",
                )
            if result.status != "WOULD_SUBMIT":
                raise ExecutionBlocked(
                    message="The execution gate refused this authorization; nothing was submitted.",
                    reason_codes=list(result.reason_codes),
                    detail=f"decision {record.decision_id} status {result.status}",
                )
            return fn(authorized_proposal(record), *args, **kwargs)

        return guarded

    # -- reads ---------------------------------------------------------------------------------------
    def replay(self, decision_id: str, **kwargs: Any) -> ReplayResult:
        return _replay.replay(self.get_decision(decision_id), **kwargs)

    def verify_chain(self) -> ChainVerification:
        return self._tenant_ledger().verify_chain()

    def get_decision(self, decision_id: str) -> DecisionRecord:
        """The record, or ``NotFound``. Another tenant's id is NotFound too, never Forbidden (B3)."""
        return self._tenant_ledger().get(decision_id)

    def list_decisions(self, *, limit: int = 50, before_sequence: int | None = None) -> list[DecisionRecord]:
        """Newest first, strictly before ``before_sequence`` (ledger REQ-4 cursor paging)."""
        return self._tenant_ledger().list(limit=limit, before_sequence=before_sequence)

    def get_execution(self, decision_id: str) -> ExecutionResult:
        """The last execution attempt for a decision this instance executed."""
        self.get_decision(decision_id)  # tenant scoping first: an unknown id is NotFound, not empty
        with self._lock:
            result = self._executions.get(decision_id)
        if result is None:
            raise NotFound(detail="no execution has been attempted for this decision")
        return result

    # -- wiring ----------------------------------------------------------------------------------------
    def _tenant_ledger(self) -> TenantLedger:
        return self.ledger.for_tenant(self.tenant_id)

    def _require_broker(self) -> BrokerAdapter:
        if self.broker is None:
            raise ConfigurationError(
                message="No broker is configured for this tenant.",
                detail="Mizan(broker=...) is required to execute",
            )
        return self.broker

    def _require_context_provider(self) -> ContextProvider:
        if self.context_provider is None:
            raise ConfigurationError(
                message="No market data source is configured for this tenant.",
                reason_codes=[ReasonCode.MARKET_DATA_MISSING],
                detail="Mizan(broker=...) is required: prices are never taken from the caller (F-1)",
            )
        return self.context_provider

    def _require_agent(self, proposal: TradeProposal) -> None:
        """A proposal may not claim another agent's identity. Impersonation is refused, not governed."""
        if proposal.agent.agent_id != self.agent.agent_id:
            raise ValidationFailed(
                message="The proposal does not belong to this agent.",
                detail=f"proposal agent {proposal.agent.agent_id!r} != {self.agent.agent_id!r}",
            )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def authorized_proposal(record: DecisionRecord) -> TradeProposal:
    """The proposal as the governor authorized it: the original when nothing was cut, a resized copy
    when it was.

    A REDUCE that is only *reported* is not a reduction. Whatever a caller does with the object it is
    handed, the object itself must already carry the authorized quantities, so that the smallest
    possible mistake - passing the proposal straight through to a broker, which is exactly what the
    ``@protected`` example does - still submits the allowed size.

    The rebuilt proposal necessarily has a different ``proposal_id``: it is a different order, and
    ``proposal_id`` is a hash of the order's content. The decision that authorized it names the
    original id, which is what ties the two together in the ledger.
    """
    proposal = record.proposal
    if record.governor_decision.verdict == "APPROVE":
        return proposal
    quantities = {leg.leg_index: leg.quantity for leg in record.authorized.legs}
    if set(quantities) != {leg.leg_index for leg in proposal.legs}:
        raise ExecutionBlocked(
            message="The authorized order does not describe this proposal's legs.",
            reason_codes=[ReasonCode.AUTHORIZATION_SCOPE_MISMATCH],
            detail=f"decision {record.decision_id} authorized legs {sorted(quantities)}",
        )
    payload = proposal.model_dump(mode="json")
    payload.pop("proposal_id")
    payload["legs"] = [
        {**leg.model_dump(mode="json"), "quantity": quantities[leg.leg_index]} for leg in proposal.legs
    ]
    return TradeProposal.build(**payload)
