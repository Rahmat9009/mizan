"""L3 — the execution gate: the last thing between an authorization and a broker mutation.

Sprint 1 ships the safety-critical configuration and kill-switch machinery for real, because those are the
pieces that must be impossible to misconfigure even before the gate itself exists. ``ExecutionGate.execute``
is the stub L3 fills in Sprint 2; its documented check order is reproduced below and is what invariants 06,
07 and 14 pin.

Hard Rules: E3 (no bypass), E4 (kill switch immediately before the mutation), E5 (no silent resizing),
E6 (authorization expires and is re-validated before submission), E9 (TOCTOU re-check), B1 (paper is a
deployment boundary, not a flag).
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from pydantic import ConfigDict

from mizan.contracts import (
    ContractModel,
    ExecutionAuthorization,
    ExecutionResult,
    GovernorDecision,
    Policy,
    StrictTrue,
    TradeProposal,
)
from mizan.contracts.errors import LiveTradingForbidden

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mizan.adapters import BrokerAdapter, ContextProvider
    from mizan.authorization import AuthorizationRegistry

__all__ = [
    "CHECK_ORDER",
    "EnvKillSwitch",
    "ExecutionConfig",
    "ExecutionGate",
    "InMemoryKillSwitch",
    "KillSwitch",
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

    def is_active(self) -> bool: ...


class InMemoryKillSwitch:
    """Process-local kill switch. Thread-safe; the state is a single boolean."""

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
    def from_environment(cls) -> "ExecutionConfig":
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
        broker: "BrokerAdapter",
        kill_switch: KillSwitch,
        registry: "AuthorizationRegistry",
        context_provider: "ContextProvider",
        policy: Policy,
        config: ExecutionConfig,
        clock: Callable[[], "datetime"],
    ) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.registry = registry
        self.context_provider = context_provider
        self.policy = policy
        self.config = config
        self.clock = clock

    def execute(
        self,
        auth: ExecutionAuthorization,
        proposal: TradeProposal,
        decision: GovernorDecision,
    ) -> ExecutionResult:
        raise NotImplementedError("L3 implements this in Sprint 2")
