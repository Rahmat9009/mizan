"""How the console reaches data, and the one thing it is allowed to assume about the caller.

The console never opens a ledger file, a database or a broker connection. It is handed a *client* - the SDK
facade, an API read-model wrapper, or in tests a fake - and reads through it. That keeps L4 unblocked by L3:
the protocol below is structural, so anything with the right method names satisfies it.

Two spellings are accepted for each read, because the ledger (`list`, `get`) and the SDK (`list_decisions`,
`get_decision`) name the same operations differently and the console should not care which one it was given.
A client that cannot answer a question is not an error: :data:`UNAVAILABLE` comes back and the view renders an
explicit "not available from this client" state rather than inventing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from mizan.contracts import ControlEvent, DecisionRecord, Policy
from mizan.contracts.errors import NotFound

__all__ = [
    "UNAVAILABLE",
    "ConsoleClient",
    "Unavailable",
    "chain_entries",
    "get_decision",
    "get_policy",
    "list_control_events",
    "list_decisions",
    "read",
    "replay_decision",
    "verify_chain",
]


class Unavailable:
    """Sentinel: the client cannot answer this question. Distinct from ``None``, which is a real answer."""

    _instance: Unavailable | None = None

    def __new__(cls) -> Unavailable:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNAVAILABLE"


UNAVAILABLE = Unavailable()


@runtime_checkable
class ConsoleClient(Protocol):
    """The read surface the console uses. Every member is optional in practice; see :func:`read`."""

    def list_decisions(
        self, *, limit: int = 50, before_sequence: int | None = None
    ) -> Sequence[DecisionRecord]: ...

    def get_decision(self, decision_id: str) -> DecisionRecord: ...

    def list_control_events(
        self, *, limit: int = 50, before_sequence: int | None = None
    ) -> Sequence[ControlEvent]: ...

    def chain_entries(self) -> Sequence[DecisionRecord | ControlEvent]: ...

    def verify_chain(self) -> Any: ...


def read(client: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    """Call the first method of ``client`` named in ``names``; return :data:`UNAVAILABLE` when none exists."""
    for name in names:
        method = getattr(client, name, None)
        if callable(method):
            return method(*args, **kwargs)
    return UNAVAILABLE


def list_decisions(
    client: Any, *, limit: int = 50, before_sequence: int | None = None
) -> list[DecisionRecord]:
    """Newest first, strictly before ``before_sequence`` (REQ-4 cursor paging). Empty when unavailable."""
    result = read(client, ("list_decisions", "list"), limit=limit, before_sequence=before_sequence)
    return [] if isinstance(result, Unavailable) or result is None else list(result)


def get_decision(client: Any, decision_id: str) -> DecisionRecord | None:
    """One record, or ``None`` for an id this tenant cannot see.

    REQ-4: ``NotFound`` is raised both for an id that does not exist and for another tenant's id, and the two
    must never be distinguished. This function collapses them into the same ``None`` before any view sees them,
    so no caller downstream is able to tell them apart even by accident.
    """
    try:
        result = read(client, ("get_decision", "get"), decision_id)
    except NotFound:
        return None
    if isinstance(result, Unavailable) or result is None:
        return None
    return result


def list_control_events(
    client: Any, *, limit: int = 50, before_sequence: int | None = None
) -> list[ControlEvent]:
    """Control events, newest first. Empty when the client does not expose them."""
    result = read(
        client, ("list_control_events",), limit=limit, before_sequence=before_sequence
    )
    return [] if isinstance(result, Unavailable) or result is None else list(result)


def chain_entries(client: Any) -> list[Any]:
    """The merged decision + control-event chain in sequence order, or an empty list."""
    result = read(client, ("chain_entries",))
    return [] if isinstance(result, Unavailable) or result is None else list(result)


def verify_chain(client: Any) -> Any:
    """The ``ChainVerification`` the client reports, or :data:`UNAVAILABLE`."""
    return read(client, ("verify_chain",))


def replay_decision(client: Any, decision_id: str) -> Any:
    """The ``ReplayResult`` for a decision replay, or :data:`UNAVAILABLE`.

    Named for the vocabulary the project uses in user-facing text: decision replay, never bare "replay".
    """
    try:
        return read(client, ("replay", "decision_replay", "replay_decision"), decision_id)
    except NotFound:
        return UNAVAILABLE


def get_policy(client: Any, policy_id: str, version: str) -> Policy | None:
    """One policy version, or ``None`` when the client cannot supply it."""
    try:
        result = read(client, ("get_policy", "policy_version"), policy_id, version)
    except NotFound:
        return None
    if isinstance(result, Unavailable) or result is None:
        return None
    return result
