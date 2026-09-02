from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.execution.models import (
    BrokerOrderSnapshot,
    ExecutionAuthorization,
    ExecutionResult,
)
from app.models import (
    AIRiskAnalysis,
    AuditEvent,
    GovernorDecision,
    MarketRiskSnapshot,
    PortfolioSnapshot,
    RiskReport,
    TradeProposal,
)
from app.persistence.database import Database, DatabaseError


ModelT = TypeVar("ModelT", bound=BaseModel)


class PersistenceError(RuntimeError):
    """A safe public persistence-layer failure."""


class PersistenceConflictError(PersistenceError):
    """An immutable identity was reused with different data."""


class CorruptPersistedDataError(PersistenceError):
    """Persisted JSON was malformed or violated its domain model."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _serialize_legs(legs: list[BaseModel] | None) -> str | None:
    """Store leg structure as a queryable column. No legs means NULL, never `[]`.

    An equity order and a not-yet-reconciled order both genuinely have no leg
    structure; writing an empty array would claim otherwise.
    """

    if not legs:
        return None
    payload = [leg.model_dump(mode="json") for leg in legs]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deserialize(model: type[ModelT], raw: str, label: str) -> ModelT:
    try:
        return model.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise CorruptPersistedDataError(f"Persisted {label} is malformed.") from exc


def _same_immutable_payload(model: type[BaseModel], stored: str, candidate: str) -> bool:
    """Decide whether stored JSON and a candidate describe the same record.

    Byte equality is the fast path. When the bytes differ the stored row is
    parsed back through the same model and re-serialized canonically, so a
    difference that is purely representational — a key added with its default, a
    different key order, different whitespace — is recognized as the same
    record.

    This stays strict about meaning. A stored payload that will not parse as the
    incoming model, or that canonicalizes to different content, is not the same
    record and the caller must treat it as a conflict.
    """

    if stored == candidate:
        return True
    try:
        normalized = _serialize(model.model_validate_json(stored))
    except (ValidationError, ValueError, TypeError):
        return False
    return normalized == candidate


class LifecycleRepository:
    """Small typed repository; SQL rows never escape into the domain layer."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _immutable_insert(
        self,
        table: str,
        proposal_id: str,
        payload: BaseModel,
        timestamp_column: str,
        timestamp: str,
    ) -> None:
        serialized = _serialize(payload)
        try:
            with self.database.connection() as connection:
                current = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if current is not None:
                    if not _same_immutable_payload(
                        type(payload), current["payload_json"], serialized
                    ):
                        raise PersistenceConflictError(
                            f"{table} already contains different data for this proposal."
                        )
                    return
                connection.execute(
                    f"INSERT INTO {table}(proposal_id, payload_json, {timestamp_column}) VALUES (?, ?, ?)",
                    (proposal_id, serialized, timestamp),
                )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def save_proposal(self, proposal: TradeProposal) -> None:
        self._immutable_insert(
            "proposals",
            proposal.proposal_id,
            proposal,
            "created_at",
            proposal.created_at.astimezone(timezone.utc).isoformat(),
        )

    def save_portfolio_snapshot(self, proposal_id: str, snapshot: PortfolioSnapshot) -> None:
        self._immutable_insert(
            "portfolio_snapshots", proposal_id, snapshot, "captured_at", utc_now_iso()
        )

    def save_market_risk(self, proposal_id: str, snapshot: MarketRiskSnapshot) -> None:
        self._immutable_insert(
            "market_risk_snapshots", proposal_id, snapshot, "captured_at", utc_now_iso()
        )

    def save_risk_report(self, report: RiskReport) -> None:
        self._immutable_insert(
            "risk_reports", report.proposal_id, report, "created_at", utc_now_iso()
        )

    def save_ai_risk_analysis(self, analysis: AIRiskAnalysis) -> None:
        self._immutable_insert(
            "ai_risk_analyses", analysis.proposal_id, analysis, "created_at", utc_now_iso()
        )

    def save_governor_decision(self, decision: GovernorDecision) -> None:
        self._immutable_insert(
            "governor_decisions",
            decision.proposal_id,
            decision,
            "decided_at",
            decision.decided_at.astimezone(timezone.utc).isoformat(),
        )

    def save_execution_authorization(self, authorization: ExecutionAuthorization) -> None:
        created_at = authorization.authorization_created_at.astimezone(timezone.utc).isoformat()
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO execution_authorizations(
                        proposal_id, payload_json, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (authorization.proposal_id, _serialize(authorization), created_at),
                )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def save_execution_result(self, result: ExecutionResult) -> None:
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO execution_results(
                        proposal_id, client_order_id, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(proposal_id) DO UPDATE SET
                        client_order_id=excluded.client_order_id,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        result.proposal_id,
                        result.client_order_id,
                        _serialize(result),
                        utc_now_iso(),
                    ),
                )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def save_broker_order(self, order: BrokerOrderSnapshot) -> None:
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO broker_orders(
                        client_order_id, alpaca_order_id, proposal_id, symbol, side,
                        quantity, lifecycle_status, broker_status, submitted_at,
                        filled_at, filled_quantity, filled_avg_price, updated_at,
                        paper, asset_class, order_class, underlying, legs_json,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(client_order_id) DO UPDATE SET
                        alpaca_order_id=excluded.alpaca_order_id,
                        lifecycle_status=excluded.lifecycle_status,
                        broker_status=excluded.broker_status,
                        filled_at=excluded.filled_at,
                        filled_quantity=excluded.filled_quantity,
                        filled_avg_price=excluded.filled_avg_price,
                        updated_at=excluded.updated_at,
                        legs_json=excluded.legs_json,
                        payload_json=excluded.payload_json
                    """,
                    (
                        order.client_order_id,
                        order.alpaca_order_id,
                        order.proposal_id,
                        order.symbol,
                        order.side.value,
                        order.quantity,
                        order.lifecycle_status.value,
                        order.broker_status,
                        order.submitted_at.astimezone(timezone.utc).isoformat(),
                        order.filled_at.astimezone(timezone.utc).isoformat()
                        if order.filled_at
                        else None,
                        order.filled_quantity,
                        order.filled_avg_price,
                        order.updated_at.astimezone(timezone.utc).isoformat(),
                        order.asset_class,
                        order.order_class,
                        order.underlying,
                        _serialize_legs(order.legs),
                        _serialize(order),
                    ),
                )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        try:
            with self.database.connection() as connection:
                existing = connection.execute(
                    "SELECT payload_json FROM audit_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_json"] != _serialize(event.payload):
                        raise PersistenceConflictError("Audit event ID was reused with different data.")
                    return event
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        event_id, proposal_id, actor, action, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.proposal_id,
                        event.actor,
                        event.action,
                        _serialize(event.payload),
                        event.created_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
            return event
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def list_audit_events(self, proposal_id: str) -> list[AuditEvent]:
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, proposal_id, actor, action, payload_json, created_at
                    FROM audit_events WHERE proposal_id = ? ORDER BY sequence ASC
                    """,
                    (proposal_id,),
                ).fetchall()
            events: list[AuditEvent] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    events.append(
                        AuditEvent(
                            event_id=row["event_id"],
                            proposal_id=row["proposal_id"],
                            actor=row["actor"],
                            action=row["action"],
                            payload=payload,
                            created_at=row["created_at"],
                        )
                    )
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                    raise CorruptPersistedDataError("Persisted audit event is malformed.") from exc
            return events
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def _get(self, table: str, proposal_id: str, model: type[ModelT], label: str) -> ModelT | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
            return _deserialize(model, row["payload_json"], label) if row else None
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def get_proposal(self, proposal_id: str) -> TradeProposal | None:
        return self._get("proposals", proposal_id, TradeProposal, "proposal")

    def get_portfolio_snapshot(self, proposal_id: str) -> PortfolioSnapshot | None:
        return self._get("portfolio_snapshots", proposal_id, PortfolioSnapshot, "portfolio snapshot")

    def get_market_risk(self, proposal_id: str) -> MarketRiskSnapshot | None:
        return self._get("market_risk_snapshots", proposal_id, MarketRiskSnapshot, "market risk")

    def get_risk_report(self, proposal_id: str) -> RiskReport | None:
        return self._get("risk_reports", proposal_id, RiskReport, "risk report")

    def get_ai_risk_analysis(self, proposal_id: str) -> AIRiskAnalysis | None:
        return self._get("ai_risk_analyses", proposal_id, AIRiskAnalysis, "AI risk analysis")

    def get_governor_decision(self, proposal_id: str) -> GovernorDecision | None:
        return self._get("governor_decisions", proposal_id, GovernorDecision, "Governor decision")

    def get_execution_authorization(self, proposal_id: str) -> ExecutionAuthorization | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json FROM execution_authorizations
                    WHERE proposal_id = ? ORDER BY created_at DESC, authorization_id DESC LIMIT 1
                    """,
                    (proposal_id,),
                ).fetchone()
            return (
                _deserialize(ExecutionAuthorization, row["payload_json"], "execution authorization")
                if row
                else None
            )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def get_execution_result(self, proposal_id: str) -> ExecutionResult | None:
        return self._get("execution_results", proposal_id, ExecutionResult, "execution result")

    def get_broker_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM broker_orders WHERE client_order_id = ?",
                    (client_order_id,),
                ).fetchone()
            return (
                _deserialize(BrokerOrderSnapshot, row["payload_json"], "broker order")
                if row
                else None
            )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def get_broker_order_by_alpaca_id(self, alpaca_order_id: str) -> BrokerOrderSnapshot | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM broker_orders WHERE alpaca_order_id = ?",
                    (alpaca_order_id,),
                ).fetchone()
            return (
                _deserialize(BrokerOrderSnapshot, row["payload_json"], "broker order")
                if row
                else None
            )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def get_broker_order_for_proposal(self, proposal_id: str) -> BrokerOrderSnapshot | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json FROM broker_orders
                    WHERE proposal_id = ? ORDER BY updated_at DESC LIMIT 1
                    """,
                    (proposal_id,),
                ).fetchone()
            return (
                _deserialize(BrokerOrderSnapshot, row["payload_json"], "broker order")
                if row
                else None
            )
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc

    def lifecycle(self, proposal_id: str) -> dict[str, Any] | None:
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            return None
        return {
            "proposal": proposal,
            "portfolio_snapshot": self.get_portfolio_snapshot(proposal_id),
            "market_risk_snapshot": self.get_market_risk(proposal_id),
            "risk_report": self.get_risk_report(proposal_id),
            "ai_risk_analysis": self.get_ai_risk_analysis(proposal_id),
            "governor_decision": self.get_governor_decision(proposal_id),
            "execution_authorization": self.get_execution_authorization(proposal_id),
            "execution_result": self.get_execution_result(proposal_id),
            "broker_order": self.get_broker_order_for_proposal(proposal_id),
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = min(max(limit, 1), 100)
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT proposal_id FROM proposals ORDER BY created_at DESC, proposal_id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            return [value for row in rows if (value := self.lifecycle(row["proposal_id"]))]
        except DatabaseError as exc:
            raise PersistenceError(str(exc)) from exc
