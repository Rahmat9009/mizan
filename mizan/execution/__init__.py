"""L3 — the execution gate: the last thing between an authorization and a broker mutation.

``ExecutionGate.execute`` runs :data:`CHECK_ORDER` and nothing else can reach the broker: there is exactly
one ``submit_order`` call in this package and it is reachable only by falling off the end of every check.
The order is the specification, and invariants 06, 07 and 14 pin it.

The kill switch is deliberately **last**. Checking it at request entry would leave a window — the whole
TOCTOU re-validation and the consume — in which an operator has thrown the switch and an order still goes
out. Read immediately before the mutation, after the final broker read, that window does not exist.

:mod:`mizan.execution.reconciliation` sits beside the gate and is strictly read-only: it reports how the
broker's view differs from Mizan's and has no way to act on the answer (B4).

Hard Rules: E3 (no bypass), E4 (kill switch immediately before the mutation), E5 (no silent resizing),
E6 (authorization expires and is re-validated before submission), E9 (TOCTOU re-check), B1 (paper is a
deployment boundary, not a flag).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ConfigDict

from mizan import authorization as _authorization
from mizan import risk as _risk
from mizan.contracts import (
    BoundState,
    BrokerRef,
    ContractModel,
    ExecutionAuthorization,
    ExecutionResult,
    Fill,
    GovernorDecision,
    Policy,
    ReasonCode,
    RevalidationReport,
    RiskContext,
    StrictTrue,
    TradeProposal,
    dec,
    format_ts,
    object_hash,
    sorted_reason_codes,
    uuid7,
)
from mizan.contracts.errors import (
    AuthorizationError,
    ConfigurationError,
    LiveTradingForbidden,
    MizanError,
)
from mizan.execution.reconciliation import (
    DISCREPANCY_STATUSES,
    Reconciler,
    ReconciliationItem,
    ReconciliationReport,
    ReconciliationStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mizan.adapters import BrokerAdapter, ContextProvider
    from mizan.authorization import AuthorizationRegistry

__all__ = [
    "CHECK_ORDER",
    "WORKER_COUNT_VARIABLES",
    "assert_kill_switch_covers_every_worker",
    "configured_worker_count",
    "DISCREPANCY_STATUSES",
    "EnvKillSwitch",
    "ExecutionConfig",
    "ExecutionGate",
    "InMemoryKillSwitch",
    "KillSwitch",
    "ReconciliationItem",
    "ReconciliationReport",
    "ReconciliationStatus",
    "Reconciler",
]

# The only accepted spelling of "yes". Everything else - including an unset variable - fails closed.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})

#: The order ``ExecutionGate.execute`` must follow. The kill switch is deliberately last (E4).
CHECK_ORDER = (
    "execution_enabled",
    "authorization_valid",
    "idempotency",
    "toctou_revalidation",
    "authorization_consumed",
    "authorization_fresh",
    "kill_switch",
    "submit",
)


#: Every spelling of "how many workers" this build knows. Checked at application construction.
WORKER_COUNT_VARIABLES = (
    "WEB_CONCURRENCY",      # uvicorn / gunicorn / many PaaS
    "UVICORN_WORKERS",
    "GUNICORN_WORKERS",
    "MIZAN_WORKERS",
)


def configured_worker_count() -> int:
    """The largest worker count any recognised variable asks for. Unparseable means 'more than one'.

    Fails toward refusing to boot: a variable we cannot read is not evidence of a single worker.
    """
    highest = 1
    for name in WORKER_COUNT_VARIABLES:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            highest = max(highest, int(raw.strip()))
        except ValueError:
            return 2
    return highest


def assert_kill_switch_covers_every_worker(kill_switch: KillSwitch) -> None:
    """Refuse to boot a multi-worker deployment behind a process-local kill switch (F-28 class).

    The kill switch is the control an operator reaches for when everything else has already failed, and
    it is the one this product demonstrates. Behind N workers with a process-local switch, tripping it
    returns 200 and ``active: true``, stops the single worker that served the request, and leaves the
    other N-1 trading - the worst possible failure for a safety control, because it reports success.

    Refusing at construction is deliberate: the alternative is discovering it during the incident the
    switch exists for. Set one worker, or supply a kill switch whose state is shared across processes
    (``shared_state = True``).
    """
    workers = configured_worker_count()
    if workers <= 1:
        return
    if getattr(kill_switch, "shared_state", False):
        return
    named = ", ".join(f"{n}={os.getenv(n)!r}" for n in WORKER_COUNT_VARIABLES if os.getenv(n))
    # The variable name, the count and the remedy go in `message`, not `detail`: this error aborts
    # startup, so its only audience is the operator reading the crash, and MizanError.__str__ renders
    # `message` alone. None of it is sensitive - a worker count is not a credential.
    raise ConfigurationError(
        message=(
            "refusing to start: this deployment asks for multiple worker processes but the kill switch "
            "is process-local, so tripping it would stop one worker and leave the others trading. "
            f"{named or 'a worker variable'} requests {workers} workers and "
            f"{type(kill_switch).__name__}.shared_state is False. Run a single worker, or supply a kill "
            "switch whose state is shared across processes (shared_state = True)."
        ),
        detail=f"worker variables: {named or 'none set'}",
    )


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise LiveTradingForbidden(message=f"{name} must be a true/false value.")


@runtime_checkable
class KillSwitch(Protocol):
    """Consulted immediately before every broker mutation. Must not depend on the policy engine."""

    #: True only for a switch whose state is shared across PROCESSES. A process-local switch stops the
    #: worker that read it and no other, so an operator who trips it gets a success response while the
    #: remaining workers keep trading. Implementations default to False by omission, which fails safe.
    shared_state: bool

    def is_active(self) -> bool: ...


class InMemoryKillSwitch:
    """Process-local kill switch. Thread-safe; the state is a single boolean.

    NOT safe under multiple worker processes: each worker gets its own instance, so tripping it stops
    one worker and leaves the rest trading. ``assert_kill_switch_covers_every_worker`` refuses to boot
    that configuration rather than letting the operator discover it during an incident.
    """

    shared_state = False

    def __init__(self, *, active: bool = False) -> None:
        self._lock = threading.Lock()
        self._active = bool(active)

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def activate(self) -> None:
        with self._lock:
            self._active = True

    def deactivate(self) -> None:
        """De-escalation is a human action (R-GRAD-1); callers must record a ControlEvent."""
        with self._lock:
            self._active = False


class EnvKillSwitch:
    """Reads ``MIZAN_KILL_SWITCH`` on EVERY call, so an operator can trip it without a redeploy.

    Re-reading each time is the point: a cached value would let the process keep trading after the
    switch was thrown. An unparseable value is treated as active - the switch fails safe.
    """

    variable = "MIZAN_KILL_SWITCH"

    #: Also False. The value is re-read on every call, but a running worker cannot see an edit made to
    #: the parent's environment after it forked, so this is a deploy-time control, not a runtime one.
    shared_state = False

    def is_active(self) -> bool:
        raw = os.getenv(self.variable)
        if raw is None:
            return False
        normalized = raw.strip().casefold()
        if normalized in _TRUE:
            return True
        if normalized in _FALSE:
            return False
        return True


class ExecutionConfig(ContractModel):
    """Execution configuration. There is no representable live configuration (B1).

    ``paper`` is ``Literal[True]``: constructing this object with anything else is a validation error,
    so no code path - not a debug flag, not a test helper, not an admin route - can express live trading.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    paper: StrictTrue = True
    enabled: bool = False
    dry_run: bool = True

    @classmethod
    def from_environment(cls) -> ExecutionConfig:
        """Build from the environment, refusing anything that is not an explicit paper account.

        ``ALPACA_PAPER`` must be present and true. Absent, empty or false all raise: an unset variable
        is not permission, and this is the one decision where a permissive default is unacceptable.
        """
        raw = os.getenv("ALPACA_PAPER")
        if raw is None or raw.strip().casefold() not in _TRUE:
            raise LiveTradingForbidden(
                message="ALPACA_PAPER must be explicitly true; this build has no live trading path."
            )
        return cls(
            enabled=_env_flag("MIZAN_EXECUTION_ENABLED", default=False),
            dry_run=_env_flag("MIZAN_EXECUTION_DRY_RUN", default=True),
        )


class ExecutionGate:
    """The single path to a broker mutation.

    ``execute`` runs :data:`CHECK_ORDER` and returns an :class:`ExecutionResult`; every failure returns
    ``BLOCKED`` with reason codes and performs no mutation. It never resizes an order (E5): fresh risk
    that supports less is ``REAUTHORIZATION_REQUIRED``, not a quiet cut.
    """

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        kill_switch: KillSwitch,
        registry: AuthorizationRegistry,
        context_provider: ContextProvider,
        policy: Policy,
        config: ExecutionConfig,
        clock: Callable[[], datetime],
    ) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.registry = registry
        self.context_provider = context_provider
        self.policy = policy
        self.config = config
        self.clock = clock

    # -- the gate -------------------------------------------------------------------------------
    def execute(
        self,
        auth: ExecutionAuthorization,
        proposal: TradeProposal,
        decision: GovernorDecision,
    ) -> ExecutionResult:
        """Run :data:`CHECK_ORDER` and return what happened. The only path to a broker mutation.

        Every step that refuses returns early with ``BLOCKED`` (or the documented status) and reason
        codes, having performed no mutation. Nothing here can be skipped by a flag, a caller argument
        or an exception handler: there is one ``submit_order`` call in this class and it is reachable
        only by falling off the end of every check (E3).
        """
        checked_at = format_ts(self.clock())
        unperformed = _unperformed()

        # 1 - is execution switched on at all?
        if not self.config.enabled:
            return self._blocked(
                auth,
                [ReasonCode.EXECUTION_DISABLED],
                revalidation=unperformed,
                checked_at=checked_at,
                message="Execution is disabled for this deployment.",
            )

        # 2 - is this authorization valid, and does it still describe this decision and proposal?
        entry_time = self.clock()
        try:
            _authorization.validate(auth, now=entry_time, decision=decision, proposal=proposal)
        except AuthorizationError as refusal:
            return self._blocked(
                auth,
                refusal.reason_codes or [ReasonCode.AUTHORIZATION_INVALID],
                revalidation=unperformed,
                checked_at=checked_at,
                message="The authorization is not valid for this execution.",
            )
        validated_at = format_ts(entry_time)

        # 3 - has this exact order already been placed? (E7: the key is derived, never chosen)
        try:
            existing = self.broker.find_order(auth.idempotency_key)
        except MizanError as broker_failure:
            return self._failed(auth, broker_failure, checked_at=checked_at, validated_at=validated_at)
        if existing is not None:
            return self._result(
                auth,
                status="RECONCILED_EXISTING",
                reason_codes=[ReasonCode.IDEMPOTENT_ORDER_EXISTS],
                revalidation=unperformed,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                client_order_id=existing.client_order_id,
                broker_order_id=existing.broker_order_id,
                broker_status=existing.status,
                message="An order already exists for this authorization; nothing was submitted.",
            )

        # 4 - TOCTOU: re-derive the state and re-run the engine against it (E9). Never resize (E5).
        fresh_context = self.context_provider.build(
            tenant_id=auth.tenant_id,
            agent_id=auth.agent_id,
            proposal=proposal,
            policy=self.policy,
            now=self.clock(),
        )
        fresh_evaluation = _risk.evaluate(proposal, fresh_context, self.policy)
        scope_quantity = dec(auth.scope.total_quantity)
        fresh_quantity = dec(fresh_evaluation.recommended_quantity)
        escalated = fresh_context.response_level > auth.bound_state.response_level
        rebound = self.policy.policy_hash != auth.bound_state.policy_hash
        unsupported = fresh_evaluation.verdict == "REJECT" or fresh_quantity < scope_quantity
        state_changed = self._state_changed(auth.bound_state, fresh_context)
        revalidation = RevalidationReport(
            performed=True,
            fresh_context_id=fresh_context.context_id,
            fresh_evaluation_id=fresh_evaluation.evaluation_id,
            fresh_recommended_quantity=fresh_evaluation.recommended_quantity,
            supported=not (unsupported or escalated or rebound),
            state_changed=state_changed,
            response_level_at_execution=fresh_context.response_level,
        )
        if unsupported or escalated or rebound:
            codes = [ReasonCode.REAUTHORIZATION_REQUIRED]
            if unsupported or state_changed:
                codes.append(ReasonCode.TOCTOU_STATE_CHANGED)
            if escalated:
                codes.append(ReasonCode.RESPONSE_LEVEL_ESCALATED)
            if rebound:
                codes.append(ReasonCode.STATE_BINDING_MISMATCH)
            # E5: a size the fresh state supports is not a smaller order to place, it is a refusal.
            # The agent must come back through the whole decision plane for a new authorization.
            return self._blocked(
                auth,
                codes,
                revalidation=revalidation,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                message="Fresh risk no longer supports this authorization; re-authorization is required.",
            )

        # 5 - single use, atomically. This is what turns two racing callers into one submission.
        if not self.registry.consume(auth.auth_id):
            return self._blocked(
                auth,
                [ReasonCode.AUTHORIZATION_ALREADY_USED],
                revalidation=revalidation,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                message="This authorization has already been used.",
            )

        # 6 - freshness immediately before submission (E6). Steps 3-5 took time; the TTL may have run out.
        submit_time = self.clock()
        try:
            _authorization.validate(auth, now=submit_time)
        except AuthorizationError as refusal:
            return self._blocked(
                auth,
                refusal.reason_codes or [ReasonCode.AUTHORIZATION_INVALID],
                revalidation=revalidation,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                message="The authorization expired before it could be used.",
            )
        validated_at = format_ts(submit_time)

        # 7 - the LAST check before the mutation (E4). Read live, on every execution: a value cached
        # here, or taken from configuration loaded at start-up, is a switch that cannot be thrown.
        kill_switch_checked_at = format_ts(self.clock())
        if self.kill_switch.is_active():
            return self._blocked(
                auth,
                [ReasonCode.KILL_SWITCH_ACTIVE],
                revalidation=revalidation,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                kill_switch_checked_at=kill_switch_checked_at,
                message="The kill switch is active; no order was submitted.",
            )

        # 8 - the mutation.
        if self.config.dry_run:
            return self._result(
                auth,
                status="WOULD_SUBMIT",
                reason_codes=[],
                revalidation=revalidation,
                checked_at=checked_at,
                authorization_validated_at=validated_at,
                kill_switch_checked_at=kill_switch_checked_at,
                client_order_id=auth.idempotency_key,
                message="Dry run: every check passed and no order was submitted.",
            )
        return self._submit(
            auth,
            revalidation=revalidation,
            checked_at=checked_at,
            validated_at=validated_at,
            kill_switch_checked_at=kill_switch_checked_at,
        )

    # -- helpers --------------------------------------------------------------------------------
    def _submit(
        self,
        auth: ExecutionAuthorization,
        *,
        revalidation: RevalidationReport,
        checked_at: str,
        validated_at: str,
        kill_switch_checked_at: str,
    ) -> ExecutionResult:
        """The single mutation site. Everything above has already agreed that it may happen."""
        from mizan.adapters import OrderRequest

        request = OrderRequest(
            client_order_id=auth.idempotency_key,
            symbol=auth.scope.symbol,
            asset_class=auth.scope.asset_class,
            intent=auth.scope.intent,
            legs=list(auth.scope.legs),
        )
        try:
            order = self.broker.submit_order(request)
        except MizanError as broker_failure:
            return self._failed(
                auth,
                broker_failure,
                checked_at=checked_at,
                validated_at=validated_at,
                revalidation=revalidation,
                kill_switch_checked_at=kill_switch_checked_at,
            )
        return self._result(
            auth,
            status="SUBMITTED",
            reason_codes=[],
            revalidation=revalidation,
            checked_at=checked_at,
            authorization_validated_at=validated_at,
            kill_switch_checked_at=kill_switch_checked_at,
            submitted_at=order.submitted_at,
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            fills=_fills_of(order),
            message="Submitted to the paper broker.",
        )

    def _state_changed(self, bound: BoundState, fresh: RiskContext) -> bool:
        """Did anything the authorization was bound to move between the decision and now?

        Compared by hash, not by identity: a snapshot that kept its id but changed its contents is a
        changed state, and that is precisely the case an attacker would arrange.
        """
        portfolio, market = fresh.portfolio_snapshot, fresh.market_snapshot
        if portfolio is None or market is None:
            return True
        return (
            bound.policy_hash != self.policy.policy_hash
            or bound.portfolio_snapshot_id != portfolio.snapshot_id
            or bound.portfolio_state_hash != object_hash(portfolio)
            or bound.market_snapshot_id != market.snapshot_id
            or bound.response_level != fresh.response_level
            or bound.path_state_hash != _optional_hash(fresh.path_state)
            or bound.aggregate_state_hash != _optional_hash(fresh.aggregate_state)
        )

    def _blocked(
        self,
        auth: ExecutionAuthorization,
        reason_codes: Iterable[ReasonCode],
        *,
        revalidation: RevalidationReport,
        checked_at: str,
        authorization_validated_at: str | None = None,
        kill_switch_checked_at: str | None = None,
        message: str,
    ) -> ExecutionResult:
        return self._result(
            auth,
            status="BLOCKED",
            reason_codes=reason_codes,
            revalidation=revalidation,
            checked_at=checked_at,
            authorization_validated_at=authorization_validated_at,
            kill_switch_checked_at=kill_switch_checked_at,
            message=message,
        )

    def _failed(
        self,
        auth: ExecutionAuthorization,
        failure: MizanError,
        *,
        checked_at: str,
        validated_at: str | None = None,
        revalidation: RevalidationReport | None = None,
        kill_switch_checked_at: str | None = None,
    ) -> ExecutionResult:
        """A broker that could not be reached is a FAILED execution, never an assumed success.

        The broker's own words go nowhere near the result: only the machine codes they map to. Whether
        the order reached the venue is unknown, and the answer to an unknown is the idempotency key -
        a retry finds the existing order at step 3 rather than placing a second one.
        """
        codes = failure.reason_codes or [ReasonCode.BROKER_UNAVAILABLE]
        return self._result(
            auth,
            status="FAILED",
            reason_codes=codes,
            revalidation=revalidation if revalidation is not None else _unperformed(),
            checked_at=checked_at,
            authorization_validated_at=validated_at,
            kill_switch_checked_at=kill_switch_checked_at,
            message="The broker could not be reached; the order state is unknown.",
        )

    def _result(
        self,
        auth: ExecutionAuthorization,
        *,
        status: Any,
        reason_codes: Iterable[ReasonCode],
        revalidation: RevalidationReport,
        checked_at: str,
        authorization_validated_at: str | None = None,
        kill_switch_checked_at: str | None = None,
        submitted_at: str | None = None,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        broker_status: str | None = None,
        fills: Sequence[Fill] = (),
        message: str = "",
    ) -> ExecutionResult:
        return ExecutionResult(
            schema_version="1.0.0",
            result_id=uuid7(),
            auth_id=auth.auth_id,
            decision_id=auth.decision_id,
            proposal_id=auth.proposal_id,
            tenant_id=auth.tenant_id,
            status=status,
            # Sorted and de-duplicated here, once, so no caller can build a result the contract refuses.
            reason_codes=sorted_reason_codes(reason_codes),
            broker=BrokerRef(name=self.broker.name, environment=self.broker.environment),
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            checked_at=checked_at,
            authorization_validated_at=authorization_validated_at,
            kill_switch_checked_at=kill_switch_checked_at,
            submitted_at=submitted_at,
            revalidation=revalidation,
            fills=list(fills),
            broker_status=broker_status,
            message=message,
        )


def _unperformed() -> RevalidationReport:
    """The re-validation did not run. ``supported`` is False: unknown is never treated as safe (E2)."""
    return RevalidationReport(
        performed=False,
        fresh_context_id=None,
        fresh_evaluation_id=None,
        fresh_recommended_quantity=None,
        supported=False,
    )


def _optional_hash(state: Any) -> str | None:
    return None if state is None else object_hash(state)


def _fills_of(order: Any) -> list[Fill]:
    """Fills the broker reported at submission. Absent or zero means no fill, not a zero fill."""
    quantity = getattr(order, "filled_quantity", "0")
    price = getattr(order, "avg_price", None)
    if price is None or dec(quantity) <= 0:
        return []
    return [Fill(filled_quantity=quantity, avg_price=price, filled_at=order.submitted_at)]
