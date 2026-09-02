from __future__ import annotations

import re
from typing import Any, Protocol

from app.models import AIRiskAnalysis, AuditEvent, GovernorDecision, RiskReport
from app.persistence.repositories import LifecycleRepository


class AuditLog(Protocol):
    def append_risk(self, report: RiskReport) -> AuditEvent: ...

    def append_ai_risk(self, analysis: AIRiskAnalysis) -> AuditEvent: ...

    def append_governor(self, decision: GovernorDecision) -> AuditEvent: ...

    def append_execution(
        self, proposal_id: str, action: str, payload: dict[str, Any]
    ) -> AuditEvent: ...

    def list_for_proposal(self, proposal_id: str) -> list[AuditEvent]: ...


class _AuditEventFactory:

    def append_risk(self, report: RiskReport) -> AuditEvent:
        event = AuditEvent(
            proposal_id=report.proposal_id,
            actor="risk_engine",
            action="RISK_EVALUATED",
            payload=report.model_dump(mode="json"),
        )
        return self._store(event)

    def append_ai_risk(self, analysis: AIRiskAnalysis) -> AuditEvent:
        event = AuditEvent(
            proposal_id=analysis.proposal_id,
            actor="ai_risk_agent",
            action="AI_RISK_ANALYZED",
            payload=analysis.model_dump(mode="json"),
        )
        return self._store(event)

    def append_governor(self, decision: GovernorDecision) -> AuditEvent:
        event = AuditEvent(
            proposal_id=decision.proposal_id,
            actor="governor",
            action=f"TRADE_{decision.decision.value}",
            payload=decision.model_dump(mode="json"),
        )
        return self._store(event)

    def append_execution(
        self,
        proposal_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        event = AuditEvent(
            proposal_id=proposal_id,
            actor="execution",
            action=action,
            payload=self._sanitize(payload),
        )
        return self._store(event)

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                exact_sensitive = {
                    "apikey",
                    "secret",
                    "secretkey",
                    "authorization",
                    "authorizationheader",
                    "credential",
                    "credentials",
                    "password",
                    "accesstoken",
                    "refreshtoken",
                    "token",
                    "header",
                    "headers",
                    "requestheaders",
                }
                sensitive_suffixes = (
                    "apikey",
                    "secret",
                    "secretkey",
                    "password",
                    "accesstoken",
                    "refreshtoken",
                    "credential",
                    "credentials",
                    "token",
                )
                if normalized in exact_sensitive or normalized.endswith(sensitive_suffixes):
                    safe[str(key)] = "[REDACTED]"
                else:
                    safe[str(key)] = cls._sanitize(item)
            return safe
        if isinstance(value, (list, tuple)):
            return [cls._sanitize(item) for item in value]
        return value

    def _store(self, event: AuditEvent) -> AuditEvent:
        raise NotImplementedError


class InMemoryAuditLog(_AuditEventFactory):
    def __init__(self):
        self._events: list[AuditEvent] = []

    def _store(self, event: AuditEvent) -> AuditEvent:
        self._events.append(event)
        return event

    def list_for_proposal(self, proposal_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.proposal_id == proposal_id]


class SQLiteAuditLog(_AuditEventFactory):
    """Durable implementation preserving the existing audit interface."""

    def __init__(self, repository: LifecycleRepository) -> None:
        self.repository = repository

    def _store(self, event: AuditEvent) -> AuditEvent:
        return self.repository.append_audit_event(event)

    def list_for_proposal(self, proposal_id: str) -> list[AuditEvent]:
        return self.repository.list_audit_events(proposal_id)
