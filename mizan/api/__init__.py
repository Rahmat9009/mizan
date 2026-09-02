"""L3 — the REST surface.

Every route is tenant-scoped and every error is a stable code plus a correlation id. A traceback, a
database path, a broker account id or another tenant's data must never appear in a response.

Four legacy findings shaped this module and are answered structurally rather than route by route:

* **F-3 (no authentication anywhere).** Every ``/v1`` route depends on :func:`_principal`; there is no
  route function that can be reached without one. ``/v1/health`` is the single anonymous endpoint and
  it answers with liveness and nothing else.
* **F-3 (agent impersonation).** The agent identity is taken from the token's principal. A proposal
  body naming a different agent is refused; a body naming none is completed from the token.
* **F-14 (internal exception text forwarded to clients).** Every response body is
  ``MizanError.to_payload()``: a machine code, a generic sentence, a correlation id and reason codes.
  ``str(exc)`` from a lower layer goes to the server log beside the same correlation id, and nowhere else.
* **F-15 (health disclosing control-plane state).** Execution flags, the broker's name and the
  environment are returned only to an authenticated tenant.

CORS is configuration, and ``*`` is refused outright: a wildcard origin on an API that mutates a
brokerage account is not a convenience.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from mizan.api.auth import Principal, StaticTokenStore, TokenStore, token_digest
from mizan.api.ratelimit import FixedWindowRateLimiter, RateLimit
from mizan.contracts import ReasonCode, TradeProposal
from mizan.contracts.errors import (
    ConfigurationError,
    MizanError,
    NotFound,
    RateLimited,
    TenantForbidden,
    ValidationFailed,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mizan.sdk import Mizan

__all__ = [
    "ROUTES",
    "ApiConfig",
    "Principal",
    "StaticTokenStore",
    "TokenStore",
    "create_app",
    "token_digest",
]

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

#: Scopes. ``control`` is separate from ``execute`` on purpose: the authority to trade and the
#: authority to stop trading are different authorities and are not granted by the same token.
SCOPE_READ = "read"
SCOPE_EVALUATE = "evaluate"
SCOPE_EXECUTE = "execute"
SCOPE_CONTROL = "control"

_LOGGER = logging.getLogger("mizan.api")


@dataclass(frozen=True)
class ApiConfig:
    """Deployment-time API configuration. Nothing here can widen the tenant boundary."""

    cors_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = False
    evaluate_rate_limit: RateLimit = field(default_factory=RateLimit)

    def __post_init__(self) -> None:
        for origin in self.cors_origins:
            if origin.strip() in {"*", "null"} or not origin.strip():
                raise ConfigurationError(
                    message="A wildcard CORS origin is not permitted.",
                    detail=f"refused origin {origin!r}",
                )


def create_app(
    mizan: "Mizan | Callable[[str], Mizan]",
    *,
    tokens: TokenStore | None = None,
    config: ApiConfig | None = None,
) -> Any:
    """Build the FastAPI application.

    ``mizan`` may be a single instance or a resolver from tenant id to instance; the resolver form is
    what multi-tenant deployments use, so that a request can never be served by another tenant's
    pipeline. ``tokens`` is the bearer-token store; without one the app still starts and every route
    refuses every request, which is the correct behaviour for a misconfigured deployment (fail closed).
    """
    from fastapi import Depends, FastAPI, Request, Response
    from fastapi.responses import JSONResponse

    settings = config if config is not None else ApiConfig()
    store: TokenStore = tokens if tokens is not None else StaticTokenStore()
    resolve = mizan if callable(mizan) else (lambda _tenant_id: mizan)
    clock = _clock_of(mizan)
    limiter = FixedWindowRateLimiter(settings.evaluate_rate_limit, clock=clock)

    app = FastAPI(title="Mizan", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    # -- authentication -------------------------------------------------------------------------
    def principal_of(request: Request) -> Principal:
        """Resolve the bearer token, or refuse. Every /v1 route depends on this (F-3)."""
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.strip().lower() != "bearer" or not token.strip():
            raise TenantForbidden(
                message="A bearer token is required.", detail="missing or malformed Authorization header"
            )
        found = store.resolve(token.strip())
        if found is None:
            raise TenantForbidden(message="The credential is not valid.", detail="unknown token")
        if found.is_expired(clock()):
            raise TenantForbidden(message="The credential has expired.", detail="expired token")
        return found

    def require(scope: str) -> Callable[..., Principal]:
        def dependency(principal: Principal = Depends(principal_of)) -> Principal:
            if not principal.has(scope):
                raise TenantForbidden(
                    message="This credential may not perform that operation.",
                    detail=f"missing scope {scope}",
                )
            return principal

        return dependency

    def pipeline_for(principal: Principal) -> "Mizan":
        pipeline = resolve(principal.tenant_id)
        if pipeline is None or pipeline.tenant_id != principal.tenant_id:
            # A resolver that hands back another tenant's pipeline is a bug that would silently
            # cross the boundary; it is refused here rather than served.
            raise TenantForbidden(message="No pipeline is available for this tenant.")
        return pipeline

    # -- errors ---------------------------------------------------------------------------------
    @app.exception_handler(MizanError)
    async def _mizan_error(_request: Request, exc: MizanError) -> Response:
        _LOGGER.warning(
            "mizan error", extra={"correlation_id": exc.correlation_id, "code": exc.code.value,
                                  "detail": exc.detail}
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> Response:
        # F-14: the type name and the message go to the log; the client gets a code and an id.
        safe = MizanError(detail=f"{type(exc).__name__}")
        _LOGGER.exception("unhandled error", extra={"correlation_id": safe.correlation_id})
        return JSONResponse(status_code=safe.http_status, content=safe.to_payload())

    # -- routes ---------------------------------------------------------------------------------
    @app.post("/v1/proposals/evaluate")
    async def evaluate(
        request: Request, principal: Principal = Depends(require(SCOPE_EVALUATE))
    ) -> Response:
        if not limiter.allow(f"{principal.tenant_id}:{principal.agent_id}"):
            raise RateLimited(message="Too many evaluations; slow down.")
        payload = await _json_object(request)
        proposal = _proposal_for(principal, payload)
        record = pipeline_for(principal).evaluate(proposal)
        return JSONResponse(status_code=200, content=_decision_view(record))

    @app.post("/v1/decisions/{decision_id}/execute")
    async def execute(
        decision_id: str, principal: Principal = Depends(require(SCOPE_EXECUTE))
    ) -> Response:
        result = pipeline_for(principal).execute(decision_id)
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))

    @app.get("/v1/decisions/{decision_id}")
    async def get_decision(
        decision_id: str, principal: Principal = Depends(require(SCOPE_READ))
    ) -> Response:
        record = pipeline_for(principal).get_decision(decision_id)
        return JSONResponse(status_code=200, content=record.model_dump(mode="json"))

    @app.get("/v1/decisions")
    async def list_decisions(
        limit: int = 50,
        before_sequence: int | None = None,
        principal: Principal = Depends(require(SCOPE_READ)),
    ) -> Response:
        if limit < 1 or limit > 200:
            raise ValidationFailed(message="limit must be between 1 and 200.")
        records = pipeline_for(principal).list_decisions(limit=limit, before_sequence=before_sequence)
        return JSONResponse(
            status_code=200,
            content={"decisions": [_decision_view(record) for record in records]},
        )

    @app.post("/v1/decisions/{decision_id}/replay")
    async def replay_decision(
        decision_id: str, principal: Principal = Depends(require(SCOPE_READ))
    ) -> Response:
        result = pipeline_for(principal).replay(decision_id)
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))

    @app.get("/v1/audit/verify")
    async def verify(principal: Principal = Depends(require(SCOPE_READ))) -> Response:
        verification = pipeline_for(principal).verify_chain()
        return JSONResponse(status_code=200, content=verification.model_dump(mode="json"))

    @app.get("/v1/policy")
    async def policy(principal: Principal = Depends(require(SCOPE_READ))) -> Response:
        return JSONResponse(
            status_code=200, content=pipeline_for(principal).policy.model_dump(mode="json")
        )

    @app.post("/v1/control/kill-switch")
    async def kill_switch(
        request: Request, principal: Principal = Depends(require(SCOPE_CONTROL))
    ) -> Response:
        payload = await _json_object(request)
        active = payload.get("active")
        if not isinstance(active, bool):
            raise ValidationFailed(message="The request body must set 'active' to true or false.")
        pipeline = pipeline_for(principal)
        _flip(pipeline, active=active, principal=principal)
        return JSONResponse(
            status_code=200,
            content={"kill_switch": {"active": pipeline.kill_switch.is_active()}},
        )

    @app.get("/v1/health")
    async def health(request: Request) -> Response:
        """Liveness to anyone; control-plane state only to an authenticated tenant (F-15)."""
        body: dict[str, Any] = {"status": "ok"}
        try:
            principal = principal_of(request)
        except MizanError:
            return JSONResponse(status_code=200, content=body)
        pipeline = pipeline_for(principal)
        body["tenant_id"] = principal.tenant_id
        body["environment"] = "paper"
        body["execution"] = {
            "enabled": pipeline.config.enabled,
            "dry_run": pipeline.config.dry_run,
            "kill_switch_active": pipeline.kill_switch.is_active(),
        }
        body["broker"] = None if pipeline.broker is None else pipeline.broker.name
        return JSONResponse(status_code=200, content=body)

    app.state.routes = ROUTES
    return app


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _clock_of(mizan: Any) -> Callable[[], Any]:
    from datetime import UTC, datetime

    clock = getattr(mizan, "clock", None)
    return clock if callable(clock) else (lambda: datetime.now(UTC))


async def _json_object(request: Any) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as failure:  # noqa: BLE001 - a parser message is not for the client (F-14)
        raise ValidationFailed(
            message="The request body must be a JSON object.", detail=type(failure).__name__
        ) from failure
    if not isinstance(payload, dict):
        raise ValidationFailed(message="The request body must be a JSON object.")
    return payload


def _proposal_for(principal: Principal, payload: dict[str, Any]) -> TradeProposal:
    """Build the proposal with the token's agent identity. The body never chooses who it is.

    A body that names the principal's own agent is accepted (clients like to be explicit); a body that
    names any other agent is refused outright rather than quietly corrected, because a silent
    correction hides an attempt.
    """
    body = dict(payload)
    claimed = body.pop("agent", None)
    if isinstance(claimed, dict) and claimed.get("agent_id") not in (None, principal.agent_id):
        raise TenantForbidden(
            message="This credential may not propose as another agent.",
            reason_codes=[ReasonCode.TENANT_ACCESS_DENIED],
            detail="agent_id in body does not match the token",
        )
    body.pop("proposal_id", None)
    try:
        return TradeProposal.build(agent=principal.agent, **body)
    except MizanError:
        raise
    except Exception as failure:  # noqa: BLE001 - pydantic's message can echo the input (F-14)
        raise ValidationFailed(
            message="The proposal is not valid.", detail=type(failure).__name__
        ) from failure


def _decision_view(record: Any) -> dict[str, Any]:
    """The decision summary a client needs, without re-serving the whole recorded state."""
    return {
        "decision_id": record.decision_id,
        "sequence": record.sequence,
        "proposal_id": record.proposal_id,
        "verdict": record.verdict,
        "reason_codes": [str(getattr(code, "value", code)) for code in record.reason_codes],
        "original": record.original.model_dump(mode="json"),
        "authorized": record.authorized.model_dump(mode="json"),
        "policy": record.policy.model_dump(mode="json"),
        "decision_timestamp": record.decision_timestamp,
        "audit_hash": record.audit_hash,
        "authorization": (
            None
            if record.authorization is None
            else {
                "auth_id": record.authorization.auth_id,
                "expires_at": record.authorization.expires_at,
                "ttl_seconds": record.authorization.ttl_seconds,
                "environment": record.authorization.environment,
            }
        ),
    }


def _flip(pipeline: Any, *, active: bool, principal: Principal) -> None:
    """Throw the switch and record the flip in the tenant's hash chain (Addendum 1 B.6, R-GRAD-2)."""
    switch = pipeline.kill_switch
    action = getattr(switch, "activate" if active else "deactivate", None)
    if action is None:
        raise ConfigurationError(
            message="This deployment's kill switch is not controlled through the API.",
            detail=f"{type(switch).__name__} has no activate/deactivate",
        )
    action()
    ledger = pipeline.ledger.for_tenant(pipeline.tenant_id)
    recorder = getattr(ledger, "append_control_event", None)
    if recorder is None:  # pragma: no cover - every shipped ledger has one
        return
    now = pipeline.now()
    recorder(
        event_type="kill_switch_activated" if active else "kill_switch_deactivated",
        # De-escalation is a human action (R-GRAD-1); the actor is the token's holder, not "system".
        actor={"type": "human", "id": principal.agent_id},
        occurred_at=now,
        recorded_at=now,
        trigger_reason_codes=[ReasonCode.KILL_SWITCH_ACTIVE] if active else [],
        policy=pipeline.policy,
    )


def _not_found() -> NotFound:
    return NotFound()
