"""Bearer tokens, principals and the tenant binding every route is checked against.

Finding F-3 was an API with no authentication at all: anyone who could reach the port could trigger a
paper order submission and read the whole account. The lesson taken here is not "add a check to the
dangerous routes" but "there is no route without one" - reads included, because a decision record
carries positions, buying power and an agent's reasoning.

Two rules this module exists to make structural:

* **identity comes from the token.** A :class:`Principal` carries the tenant, the agent identity and
  the scopes; the request body contributes none of them. An agent cannot propose as another agent by
  saying so in JSON.
* **the token itself is never stored.** :class:`StaticTokenStore` keeps SHA-256 digests and compares
  with :func:`hmac.compare_digest`, so neither a heap dump nor a log line yields a usable credential,
  and lookup time does not leak which prefix was right.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from mizan.contracts import AgentIdentity

__all__ = [
    "Principal",
    "StaticTokenStore",
    "TokenStore",
    "token_digest",
]


def token_digest(token: str) -> str:
    """The stored form of a token. The token itself is never persisted, logged or compared directly."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Who is calling, resolved from a token and from nothing else."""

    token_id: str
    tenant_id: str
    agent: AgentIdentity
    scopes: frozenset[str] = field(default_factory=frozenset)
    expires_at: datetime | None = None

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@runtime_checkable
class TokenStore(Protocol):
    def resolve(self, token: str) -> Principal | None: ...


class StaticTokenStore:
    """A token store built from a mapping of raw token to principal, kept as digests.

    Deployments hand tokens in from their own secret manager; this class is the shape the API needs,
    not a place to keep secrets. Nothing here writes a token to disk.
    """

    def __init__(self, tokens: Mapping[str, Principal] | Iterable[tuple[str, Principal]] = ()) -> None:
        items = tokens.items() if isinstance(tokens, Mapping) else tokens
        self._by_digest: dict[str, Principal] = {}
        for token, principal in items:
            self.add(token, principal)

    def add(self, token: str, principal: Principal) -> None:
        if not token or len(token) < 16:
            raise ValueError("a bearer token must be at least 16 characters")
        self._by_digest[token_digest(token)] = principal

    def resolve(self, token: str) -> Principal | None:
        """Constant-time lookup: every candidate is compared, so timing does not reveal a near miss."""
        digest = token_digest(token)
        found: Principal | None = None
        for candidate, principal in self._by_digest.items():
            if hmac.compare_digest(candidate, digest):
                found = principal
        return found
