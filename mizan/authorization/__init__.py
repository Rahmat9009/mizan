"""L2 — issuing and validating short-lived, single-use, state-bound execution authorizations.

An authorization is the only thing the execution gate will act on. It expires (E6), it is bound to the
state that justified it (Addendum 1 B.4), and it can be consumed exactly once — the registry is the
mechanism that makes "single use" true under concurrency rather than merely documented.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mizan.contracts import (
    ExecutionAuthorization,
    GovernorDecision,
    Policy,
    RiskContext,
    TradeProposal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

__all__ = [
    "AuthorizationRegistry",
    "InMemoryAuthorizationRegistry",
    "issue",
    "validate",
]


def issue(
    decision: GovernorDecision,
    proposal: TradeProposal,
    policy: Policy,
    *,
    now: "datetime",
    context: RiskContext,
) -> ExecutionAuthorization:
    """Mint an authorization for a non-REJECT decision, bound to the state in ``context``.

    Raises ``AuthorizationError`` when the decision is a REJECT: there is no path that authorizes a
    rejected proposal, and refusing here rather than later keeps that true by construction.
    """
    raise NotImplementedError("L2 implements this in Sprint 2")


def validate(
    auth: ExecutionAuthorization,
    *,
    now: "datetime",
    decision: GovernorDecision | None = None,
    proposal: TradeProposal | None = None,
) -> None:
    """Raise ``AuthorizationError`` unless ``auth`` is valid at ``now`` and matches the given objects.

    Reason codes: AUTHORIZATION_EXPIRED, AUTHORIZATION_NOT_YET_VALID, AUTHORIZATION_INVALID,
    AUTHORIZATION_SCOPE_MISMATCH. Returns ``None`` on success so a caller cannot mistake a truthy
    return value for permission.
    """
    raise NotImplementedError("L2 implements this in Sprint 2")


@runtime_checkable
class AuthorizationRegistry(Protocol):
    """Single-use enforcement. ``consume`` returns True for exactly one caller per ``auth_id``."""

    def consume(self, auth_id: str) -> bool: ...


class InMemoryAuthorizationRegistry:
    """Thread-safe single-use registry.

    The lock is what makes this an enforcement mechanism rather than a bookkeeping convenience: two
    threads racing the same authorization must not both be told to proceed, so the test-and-set is
    atomic.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consumed: set[str] = set()

    def consume(self, auth_id: str) -> bool:
        with self._lock:
            if auth_id in self._consumed:
                return False
            self._consumed.add(auth_id)
            return True

    def was_consumed(self, auth_id: str) -> bool:
        with self._lock:
            return auth_id in self._consumed
