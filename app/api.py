from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.models import MarketRiskSnapshot, TradeProposal
from app.services import BackendServices, ServiceError


DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://localhost:3000"]


class ProposalEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: TradeProposal
    market_risk: MarketRiskSnapshot


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return DEFAULT_CORS_ORIGINS
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("CORS_ORIGINS must be a comma list or JSON array.") from exc
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise RuntimeError("CORS_ORIGINS JSON must be an array of strings.")
        origins = [value.strip() for value in values if value.strip()]
    else:
        origins = [value.strip() for value in raw.split(",") if value.strip()]
    if "*" in origins:
        raise RuntimeError("Wildcard CORS origins are not supported.")
    return origins


def create_app(services: BackendServices | None = None) -> FastAPI:
    load_dotenv()
    backend = services

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if not hasattr(application.state, "services"):
            application.state.services = backend or BackendServices()
        yield

    application = FastAPI(
        title="Portfolio Governor PAPER API",
        version="1.0.0",
        lifespan=lifespan,
    )
    if backend is not None:
        application.state.services = backend
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

    def service(request: Request) -> BackendServices:
        return request.app.state.services

    @application.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value."),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": details,
                }
            },
        )

    @application.get("/health")
    def health(request: Request) -> dict[str, Any]:
        current = service(request)
        config = current.execution_config
        return {
            "status": "ok" if current.database.healthy() else "degraded",
            "paper_only": True,
            "execution_enabled": config.enabled,
            "dry_run": config.dry_run,
            "kill_switch": config.kill_switch,
            "ai_provider": current.ai_provider_name,
            "database": {
                "status": "ok" if current.database.healthy() else "unavailable",
                "technology": "SQLite",
            },
        }

    @application.get("/portfolio")
    def portfolio(request: Request) -> Any:
        return service(request).portfolio()

    @application.post("/proposals/evaluate")
    def evaluate(payload: ProposalEvaluationRequest, request: Request) -> Any:
        return service(request).evaluate(payload.proposal, payload.market_risk)

    @application.post("/proposals/{proposal_id}/execute")
    def execute(proposal_id: str, request: Request) -> Any:
        return service(request).execute_proposal(proposal_id)

    @application.get("/proposals/{proposal_id}")
    def lifecycle(proposal_id: str, request: Request) -> Any:
        return service(request).lifecycle(proposal_id)

    @application.get("/proposals/{proposal_id}/audit")
    def audit(proposal_id: str, request: Request) -> Any:
        return service(request).audit_events(proposal_id)

    @application.get("/orders/{client_order_id}")
    def get_order(client_order_id: str, request: Request) -> Any:
        try:
            return service(request).order(client_order_id, reconcile=True)
        except ServiceError as exc:
            if exc.code == "BROKER_UNAVAILABLE":
                return service(request).order(client_order_id, reconcile=False)
            raise

    @application.post("/orders/{client_order_id}/reconcile")
    def reconcile(client_order_id: str, request: Request) -> Any:
        return service(request).order(client_order_id, reconcile=True)

    @application.get("/recent")
    def recent(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> Any:
        return service(request).recent(limit)

    return application


app = create_app()
