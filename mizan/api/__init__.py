"""L3 — the REST surface.

Every route is tenant-scoped and every error is a stable code plus a correlation id. A traceback, a
database path, a broker account id or another tenant's data must never appear in a response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mizan.sdk import Mizan

__all__ = ["ROUTES", "create_app"]

#: The v1 surface. Listed here so the console and the SDK have one place to agree with.
ROUTES = (
    ("POST", "/v1/proposals/evaluate"),
    ("POST", "/v1/decisions/{decision_id}/execute"),
    ("GET", "/v1/decisions/{decision_id}"),
    ("GET", "/v1/decisions"),
    ("POST", "/v1/decisions/{decision_id}/replay"),
    ("GET", "/v1/audit/verify"),
    ("GET", "/v1/policy"),
    ("POST", "/v1/control/kill-switch"),
    ("GET", "/v1/health"),
)


def create_app(mizan: "Mizan | Callable[[str], Mizan]") -> Any:
    """Build the FastAPI application.

    ``mizan`` may be a single instance or a resolver from tenant id to instance; the resolver form is
    what multi-tenant deployments use, so that a request can never be served by another tenant's
    pipeline.
    """
    raise NotImplementedError("L3 implements this in Sprint 2")
