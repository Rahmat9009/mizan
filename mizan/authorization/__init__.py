"""L2 — issuing and validating short-lived, single-use, state-bound execution authorizations.

An authorization is the only thing the execution gate will act on. It expires (E6), it is bound to the
state that justified it (Addendum 1 B.4), and it can be consumed exactly once — the registry is the
mechanism that makes "single use" true under concurrency rather than merely documented.

The scope is built from the *decision*, never from the proposal's requested quantities: the proposal says
what the agent wanted, the decision says what it may have. Everything the gate compares later — the
idempotency key, the leg quantities, the bound state hashes — is derived here, so an authorization that
has drifted from the decision that justified it cannot validate.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from mizan.contracts import (
    ENGINE_VERSION,
    AuthorizationScope,
    AuthorizedLeg,
    BoundState,
    ExecutionAuthorization,
    GovernorDecision,
    Policy,
    ReasonCode,
    RiskContext,
    TradeProposal,
    authorization_hash_for,
    dec,
    format_ts,
    idempotency_key_for,
    object_hash,
    parse_ts,
    uuid7,
)
from mizan.contracts.errors import AuthorizationError
from mizan.contracts.execution_authorization import TTL_MAX_SECONDS, TTL_MIN_SECONDS

__all__ = [
    "AuthorizationRegistry",
    "InMemoryAuthorizationRegistry",
    "SqliteAuthorizationRegistry",
    "issue",
    "validate",
]


def _refuse(code: ReasonCode, message: str, detail: str = "") -> AuthorizationError:
    """Every refusal in this module is one machine code plus one safe, generic sentence."""
    return AuthorizationError(message, reason_codes=[code], detail=detail)


def issue(
    decision: GovernorDecision,
    proposal: TradeProposal,
    policy: Policy,
    *,
    now: datetime,
    context: RiskContext,
) -> ExecutionAuthorization:
    """Mint an authorization for a non-REJECT decision, bound to the state in ``context``.

    Raises ``AuthorizationError`` when the decision is a REJECT: there is no path that authorizes a
    rejected proposal, and refusing here rather than later keeps that true by construction.
    """
    if decision.verdict == "REJECT":
        raise _refuse(
            ReasonCode.AUTHORIZATION_INVALID,
            "A rejected decision cannot be authorized.",
            detail=f"decision {decision.decision_id} is a REJECT",
        )
    if not decision.authorized.legs or dec(decision.authorized.total_quantity) <= 0:
        raise _refuse(
            ReasonCode.AUTHORIZATION_INVALID, "The decision authorizes no quantity to execute."
        )
    if decision.proposal_id != proposal.proposal_id:
        raise _refuse(
            ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, "The decision does not belong to this proposal."
        )
    if decision.tenant_id != policy.tenant_id or decision.tenant_id != context.tenant_id:
        raise _refuse(ReasonCode.TENANT_MISMATCH, "The decision, policy and context differ in tenant.")
    if decision.policy.hash != policy.policy_hash or context.policy.hash != policy.policy_hash:
        raise _refuse(
            ReasonCode.STATE_BINDING_MISMATCH,
            "The decision, policy and context refer to different policy versions.",
        )

    portfolio = context.portfolio_snapshot
    market = context.market_snapshot
    if portfolio is None:
        raise _refuse(ReasonCode.PORTFOLIO_STATE_MISSING, "No portfolio state to bind the authorization to.")
    if market is None:
        raise _refuse(ReasonCode.MARKET_DATA_MISSING, "No market state to bind the authorization to.")

    scope = AuthorizationScope(
        symbol=proposal.symbol,
        asset_class=proposal.asset_class,
        intent=proposal.intent,
        legs=_scope_legs(decision, proposal),
        total_quantity=decision.authorized.total_quantity,
        max_notional=decision.authorized.total_notional,
    )
    bound_state = BoundState(
        policy_hash=policy.policy_hash,
        portfolio_snapshot_id=portfolio.snapshot_id,
        # sha256_hex(canonical_json(snapshot)) — the gate re-derives these from fresh state and compares.
        portfolio_state_hash=object_hash(portfolio),
        market_snapshot_id=market.snapshot_id,
        response_level=context.response_level,
        path_state_hash=None if context.path_state is None else object_hash(context.path_state),
        aggregate_state_hash=(
            None if context.aggregate_state is None else object_hash(context.aggregate_state)
        ),
    )
    return ExecutionAuthorization.build(
        auth_id=uuid7(),
        decision_id=decision.decision_id,
        proposal_id=decision.proposal_id,
        tenant_id=decision.tenant_id,
        agent_id=decision.agent_id,
        policy=policy.ref,
        engine_version=ENGINE_VERSION,
        issued_at=format_ts(now),
        # 5..30 seconds, policy-configurable, default 15 (E6). expires_at and the idempotency key are
        # derived by the contract from these fields, so they cannot disagree with them.
        ttl_seconds=policy.authorization.ttl_seconds,
        scope=scope,
        bound_state=bound_state,
    )


def _scope_legs(decision: GovernorDecision, proposal: TradeProposal) -> list[AuthorizedLeg]:
    """One authorized leg per leg of the DECISION, described by the proposal, sized by the decision."""
    by_index = {leg.leg_index: leg for leg in proposal.legs}
    legs: list[AuthorizedLeg] = []
    for authorized in decision.authorized.legs:
        source = by_index.get(authorized.leg_index)
        if source is None:
            raise _refuse(
                ReasonCode.AUTHORIZATION_SCOPE_MISMATCH,
                "The decision authorizes a leg the proposal does not contain.",
            )
        if dec(authorized.quantity) > dec(source.quantity):
            raise _refuse(
                ReasonCode.AUTHORIZATION_SCOPE_MISMATCH,
                "The decision authorizes more of a leg than the proposal requested.",
            )
        legs.append(
            AuthorizedLeg(
                leg_index=source.leg_index,
                side=source.side,
                symbol=proposal.symbol,
                occ_symbol=source.occ_symbol(proposal.symbol) if source.is_option else None,
                contract_type=source.contract_type,
                strike=source.strike,
                expiry=source.expiry,
                quantity=authorized.quantity,
                limit_price=source.limit_price,
                order_type=source.order_type,
            )
        )
    return legs


def validate(
    auth: ExecutionAuthorization,
    *,
    now: datetime,
    decision: GovernorDecision | None = None,
    proposal: TradeProposal | None = None,
) -> None:
    """Raise ``AuthorizationError`` unless ``auth`` is valid at ``now`` and matches the given objects.

    Reason codes: AUTHORIZATION_EXPIRED, AUTHORIZATION_NOT_YET_VALID, AUTHORIZATION_INVALID,
    AUTHORIZATION_SCOPE_MISMATCH. Returns ``None`` on success so a caller cannot mistake a truthy
    return value for permission.
    """
    _check_self_consistent(auth)
    _check_window(auth, now)
    if decision is not None:
        _check_decision(auth, decision)
    if proposal is not None:
        _check_proposal(auth, proposal)
    return None


def _check_self_consistent(auth: ExecutionAuthorization) -> None:
    """Recompute everything the contract derived. A forged or mutated authorization dies here.

    The contract's own validators run at construction; these run at *use*, which is the moment that
    matters — an object can reach the gate without ever passing through a constructor again.
    """
    invalid = "The authorization is not internally consistent."
    if not TTL_MIN_SECONDS <= auth.ttl_seconds <= TTL_MAX_SECONDS:
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="ttl out of bounds")
    if parse_ts(auth.expires_at) - parse_ts(auth.issued_at) != timedelta(seconds=auth.ttl_seconds):
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="lifetime does not match ttl")
    if auth.environment != "paper" or auth.single_use is not True:
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="environment or single_use")
    if auth.idempotency_key != idempotency_key_for(auth.tenant_id, auth.proposal_id, auth.scope.legs):
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="idempotency key mismatch")
    if auth.authorization_hash != authorization_hash_for(auth):
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="authorization hash mismatch")
    if auth.bound_state.policy_hash != auth.policy.hash:
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="bound policy hash mismatch")
    total = sum((dec(leg.quantity) for leg in auth.scope.legs), start=dec("0"))
    if total != dec(auth.scope.total_quantity):
        raise _refuse(ReasonCode.AUTHORIZATION_INVALID, invalid, detail="leg quantities do not sum")


def _check_window(auth: ExecutionAuthorization, now: datetime) -> None:
    """The authorization is valid on ``[issued_at, expires_at)``. The upper boundary is exact: at
    ``now == expires_at`` it is EXPIRED, because an authorization that is valid *at* its expiry is an
    authorization with a longer TTL than the policy granted."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise _refuse(
            ReasonCode.AUTHORIZATION_INVALID,
            "The authorization cannot be checked without an absolute time.",
            detail="now must be a timezone-aware datetime",
        )
    if now < parse_ts(auth.issued_at):
        raise _refuse(ReasonCode.AUTHORIZATION_NOT_YET_VALID, "The authorization is not valid yet.")
    if now >= parse_ts(auth.expires_at):
        raise _refuse(ReasonCode.AUTHORIZATION_EXPIRED, "The authorization has expired.")


def _check_decision(auth: ExecutionAuthorization, decision: GovernorDecision) -> None:
    """The authorization must still describe exactly what the decision authorized — no more, no less."""
    mismatch = "The authorization does not match the decision."
    if decision.verdict == "REJECT":
        raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="decision is a REJECT")
    for field, expected, actual in (
        ("decision_id", decision.decision_id, auth.decision_id),
        ("proposal_id", decision.proposal_id, auth.proposal_id),
        ("tenant_id", decision.tenant_id, auth.tenant_id),
        ("agent_id", decision.agent_id, auth.agent_id),
        ("policy_hash", decision.policy.hash, auth.policy.hash),
    ):
        if expected != actual:
            raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail=field)
    if dec(auth.scope.total_quantity) != dec(decision.authorized.total_quantity):
        raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="total quantity")
    authorized = {leg.leg_index: dec(leg.quantity) for leg in decision.authorized.legs}
    scoped = {leg.leg_index: dec(leg.quantity) for leg in auth.scope.legs}
    if authorized != scoped:
        raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="leg quantities")


def _check_proposal(auth: ExecutionAuthorization, proposal: TradeProposal) -> None:
    """The authorization must describe this proposal's structure, at a size it never exceeds."""
    mismatch = "The authorization does not match the proposal."
    for field, expected, actual in (
        ("proposal_id", proposal.proposal_id, auth.proposal_id),
        ("symbol", proposal.symbol, auth.scope.symbol),
        ("asset_class", proposal.asset_class, auth.scope.asset_class),
        ("intent", proposal.intent, auth.scope.intent),
    ):
        if expected != actual:
            raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail=field)
    if dec(auth.scope.total_quantity) > proposal.total_quantity:
        raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="total quantity")
    by_index = {leg.leg_index: leg for leg in proposal.legs}
    for leg in auth.scope.legs:
        source = by_index.get(leg.leg_index)
        if source is None:
            raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="unknown leg")
        expected_occ = source.occ_symbol(proposal.symbol) if source.is_option else None
        comparisons: tuple[tuple[str, str | None, str | None], ...] = (
            ("side", source.side, leg.side),
            ("symbol", proposal.symbol, leg.symbol),
            ("order_type", source.order_type, leg.order_type),
            ("contract_type", source.contract_type, leg.contract_type),
            ("strike", source.strike, leg.strike),
            ("expiry", source.expiry, leg.expiry),
            ("occ_symbol", expected_occ, leg.occ_symbol),
            ("limit_price", source.limit_price, leg.limit_price),
        )
        for leg_field, wanted, got in comparisons:
            if wanted != got:
                raise _refuse(
                    ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail=f"leg {leg_field}"
                )
        if dec(leg.quantity) > dec(source.quantity):
            raise _refuse(ReasonCode.AUTHORIZATION_SCOPE_MISMATCH, mismatch, detail="leg quantity")


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


SQLITE_BUSY_TIMEOUT_SECONDS = 30


class SqliteAuthorizationRegistry:
    """Single use across PROCESSES, not merely across threads.

    The in-memory registry above is atomic within one interpreter and says nothing at all about a
    second one. Two workers over the same ledger and the same broker - the ordinary way anything is
    deployed - each held their own set, each was the first to consume the authorization, and each
    submitted. Single use was a property of a process rather than of the authorization (F-28), and the
    only thing standing between that and a duplicate order was the broker remembering a client order
    id, which is exactly the posture this build faulted the legacy one for.

    So the test-and-set moves to durable storage, where a PRIMARY KEY does the work: the second INSERT
    of an auth_id raises IntegrityError no matter which process attempts it, or how simultaneously.
    The database enforces it, so there is no window between checking and writing for a racing caller
    to slip through - that window is what a check-then-write in application code always leaves open.

    Only the auth_id is stored. Recording a consumption TIME here would put a wall clock in the
    authorization module, and when a decision happened is the ledger's job, not this table's.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS consumed_authorizations (auth_id TEXT PRIMARY KEY)"
            )

    def _connect(self) -> sqlite3.Connection:
        # A generous timeout because the contended case is the whole point: a second worker arriving
        # mid-write should WAIT for the answer, not fail and be retried into a second submission.
        # Integer seconds: a float literal anywhere in this module is a Hard Rule A6 violation, and
        # INV-15 walks the AST of exactly this path. It caught the first version of this line.
        connection = sqlite3.connect(self.path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def consume(self, auth_id: str) -> bool:
        """True for exactly one caller per ``auth_id``, across every process sharing this file."""
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO consumed_authorizations (auth_id) VALUES (?)", (auth_id,)
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def was_consumed(self, auth_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM consumed_authorizations WHERE auth_id = ?", (auth_id,)
            ).fetchone()
        return row is not None
