from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.ai_risk import MockAIRiskProvider
from app.audit import SQLiteAuditLog
from app.execution.models import (
    BrokerOrderSnapshot,
    ExecutionAuthorization,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    OrderLifecycleState,
)
from app.execution.service import deterministic_client_order_id
from app.models import Decision, MarketRiskSnapshot, PortfolioSnapshot, Side, TradeProposal
from app.persistence import (
    CorruptPersistedDataError,
    Database,
    LifecycleRepository,
    PersistenceConflictError,
    PersistenceError,
)
from app.pipeline import DecisionPipeline


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def proposal(proposal_id: str = "persist-p1") -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="AAPL",
        side="BUY",
        quantity=1,
        estimated_price=200,
        strategy_confidence=0.9,
        thesis="Persistence test.",
        invalidation_condition="Signal reverses.",
        created_at=NOW,
    )


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=100_000,
        cash=90_000,
        buying_power=90_000,
        daily_pnl_pct=0,
        current_positions={},
        source="ALPACA_PAPER",
    )


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=0.25,
        max_drawdown_30d=0.08,
        liquidity_score=0.99,
    )


@pytest.fixture
def durable(tmp_path):
    database = Database(tmp_path / "state.db")
    return database, LifecycleRepository(database)


def evaluated(repository: LifecycleRepository):
    audit = SQLiteAuditLog(repository)
    pipeline = DecisionPipeline(
        MockAIRiskProvider(), audit=audit, repository=repository
    )
    trade = proposal()
    risk, ai, governor = pipeline.run(trade, portfolio(), market())
    return trade, risk, ai, governor, audit


def test_database_initializes_all_tables_and_foreign_keys(durable) -> None:
    database, _ = durable
    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert {
        "proposals",
        "risk_reports",
        "ai_risk_analyses",
        "governor_decisions",
        "execution_authorizations",
        "execution_results",
        "broker_orders",
        "audit_events",
    }.issubset(tables)
    assert foreign_keys == 1


def test_database_initialization_is_idempotent(durable) -> None:
    database, _ = durable
    database.initialize()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_database_parent_is_created(tmp_path) -> None:
    path = tmp_path / "nested" / "data" / "governor.db"
    Database(path)
    assert path.exists()


def test_wal_is_enabled_for_file_database(durable) -> None:
    database, _ = durable
    with database.connection() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.casefold() == "wal"


def test_full_evaluation_round_trips_typed_models(durable) -> None:
    _, repository = durable
    trade, risk, ai, governor, _ = evaluated(repository)
    assert repository.get_proposal(trade.proposal_id) == trade
    assert repository.get_portfolio_snapshot(trade.proposal_id) == portfolio()
    assert repository.get_market_risk(trade.proposal_id) == market()
    assert repository.get_risk_report(trade.proposal_id) == risk
    assert repository.get_ai_risk_analysis(trade.proposal_id) == ai
    assert repository.get_governor_decision(trade.proposal_id) == governor


def test_audit_is_oldest_first_and_survives_new_repository(durable) -> None:
    database, repository = durable
    trade, _, _, _, audit = evaluated(repository)
    assert [event.action for event in audit.list_for_proposal(trade.proposal_id)] == [
        "RISK_EVALUATED",
        "AI_RISK_ANALYZED",
        "TRADE_APPROVE",
    ]
    restarted = SQLiteAuditLog(LifecycleRepository(Database(database.path)))
    assert [event.action for event in restarted.list_for_proposal(trade.proposal_id)] == [
        "RISK_EVALUATED",
        "AI_RISK_ANALYZED",
        "TRADE_APPROVE",
    ]


def test_recursive_adversarial_secret_redaction_is_durable(durable) -> None:
    _, repository = durable
    repository.save_proposal(proposal())
    audit = SQLiteAuditLog(repository)
    audit.append_execution(
        "persist-p1",
        "ADVERSARIAL",
        {
            "ALPACA_API_KEY": "alpaca-key-marker",
            "outer": [
                {"FEATHERLESS_API_KEY": "featherless-marker"},
                {"headers": {"Authorization": "Bearer marker", "safe": "value"}},
                {"credentials": {"token": "nested-marker"}},
            ],
            "authorization_created_at": "safe-timestamp",
        },
    )
    serialized = json.dumps(audit.list_for_proposal("persist-p1")[0].payload)
    for marker in ("alpaca-key-marker", "featherless-marker", "Bearer marker", "nested-marker"):
        assert marker not in serialized
    assert "safe-timestamp" in serialized


def test_duplicate_identical_insert_is_idempotent(durable) -> None:
    _, repository = durable
    trade = proposal()
    repository.save_proposal(trade)
    repository.save_proposal(trade)
    assert repository.get_proposal(trade.proposal_id) == trade


def test_duplicate_identity_with_different_payload_conflicts(durable) -> None:
    _, repository = durable
    repository.save_proposal(proposal())
    with pytest.raises(PersistenceConflictError):
        repository.save_proposal(proposal().model_copy(update={"quantity": 2}))


def test_malformed_persisted_json_fails_safely(durable) -> None:
    database, repository = durable
    repository.save_proposal(proposal())
    with database.connection() as connection:
        connection.execute(
            "UPDATE proposals SET payload_json = ? WHERE proposal_id = ?",
            ("{not-json", "persist-p1"),
        )
    with pytest.raises(CorruptPersistedDataError, match="malformed"):
        repository.get_proposal("persist-p1")


def test_execution_authorization_result_and_order_round_trip(durable) -> None:
    _, repository = durable
    trade, _, _, governor, _ = evaluated(repository)
    authorization = ExecutionAuthorization(
        proposal_id=trade.proposal_id,
        symbol=trade.symbol,
        side=trade.side,
        original_quantity=1,
        approved_quantity=1,
        governor_decision=Decision.APPROVE,
        governor_decided_at=governor.decided_at,
        authorization_created_at=governor.decided_at,
        risk_score=governor.risk_score,
    )
    client_id = deterministic_client_order_id(trade.proposal_id)
    result = ExecutionResult(
        proposal_id=trade.proposal_id,
        client_order_id=client_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=1,
        status=ExecutionState.WOULD_SUBMIT,
        execution_mode=ExecutionMode.PAPER_DRY_RUN,
        message="Safe dry run.",
    )
    order = BrokerOrderSnapshot(
        alpaca_order_id="alpaca-1",
        client_order_id=client_id,
        proposal_id=trade.proposal_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=1,
        lifecycle_status=OrderLifecycleState.NEW,
        broker_status="new",
        submitted_at=NOW,
        updated_at=NOW,
    )
    repository.save_execution_authorization(authorization)
    repository.save_execution_result(result)
    repository.save_broker_order(order)
    assert repository.get_execution_authorization(trade.proposal_id) == authorization
    assert repository.get_execution_result(trade.proposal_id) == result
    assert repository.get_broker_order(client_id) == order
    assert repository.get_broker_order_by_alpaca_id("alpaca-1") == order


def test_execution_result_upsert_preserves_latest_state(durable) -> None:
    _, repository = durable
    trade, _, _, _, _ = evaluated(repository)
    first = ExecutionResult(
        proposal_id=trade.proposal_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=0,
        status=ExecutionState.DISABLED,
        execution_mode=ExecutionMode.PAPER_DRY_RUN,
        message="Disabled.",
    )
    second = first.model_copy(update={"status": ExecutionState.BLOCKED, "message": "Blocked."})
    repository.save_execution_result(first)
    repository.save_execution_result(second)
    assert repository.get_execution_result(trade.proposal_id) == second


def test_restart_retains_governor_and_client_order_identity(durable) -> None:
    database, repository = durable
    trade, _, _, governor, _ = evaluated(repository)
    before = deterministic_client_order_id(trade.proposal_id)
    restarted = LifecycleRepository(Database(database.path))
    assert restarted.get_governor_decision(trade.proposal_id) == governor
    assert deterministic_client_order_id(restarted.get_proposal(trade.proposal_id).proposal_id) == before


def test_concurrent_distinct_proposal_writes_are_serialized(tmp_path) -> None:
    database = Database(tmp_path / "concurrent.db")
    repository = LifecycleRepository(database)

    def write(index: int) -> None:
        repository.save_proposal(proposal(f"p-{index}"))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(20)))
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 20


def test_database_failure_is_wrapped_without_sql(monkeypatch, durable) -> None:
    database, repository = durable

    def fail_open():
        raise sqlite3.OperationalError("sensitive filesystem detail")

    monkeypatch.setattr(database, "_open", fail_open)
    with pytest.raises(PersistenceError) as caught:
        repository.get_proposal("p")
    assert "SELECT" not in str(caught.value)
    assert "sensitive filesystem detail" not in str(caught.value)
