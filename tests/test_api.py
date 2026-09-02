from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai_risk import MockAIRiskProvider
from app.api import create_app
from app.execution.gate import ExecutionConfig
from app.execution.models import (
    BrokerOrder,
    ExecutionAsset,
    MarketClockSnapshot,
)
from app.models import MarketRiskSnapshot, PortfolioSnapshot, Side, TradeProposal
from app.persistence import Database
from app.services import BackendServices


class StaticPortfolioProvider:
    def __init__(self, snapshot: PortfolioSnapshot | Exception) -> None:
        self.snapshot = snapshot

    def get_snapshot(self) -> PortfolioSnapshot:
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


class ApiAdapter:
    paper_mode_verified = True

    def __init__(self) -> None:
        self.is_open = True
        self.daily_pnl = 0.0
        self.submits = 0
        self.lookup: BrokerOrder | None = None

    def get_account(self):
        equity = 100_000
        last_equity = equity / (1 + self.daily_pnl)
        return SimpleNamespace(
            equity=str(equity),
            last_equity=str(last_equity),
            cash="100000",
            buying_power="100000",
        )

    def get_all_positions(self):
        return []

    def get_clock(self) -> MarketClockSnapshot:
        now = datetime.now(timezone.utc)
        return MarketClockSnapshot(
            timestamp=now,
            is_open=self.is_open,
            next_open=now + timedelta(hours=1),
            next_close=now + timedelta(hours=6),
        )

    def get_asset(self, symbol: str) -> ExecutionAsset:
        return ExecutionAsset(
            symbol=symbol,
            asset_class="us_equity",
            status="active",
            tradable=True,
        )

    def find_order_by_client_id(self, client_order_id: str):
        return self.lookup

    def get_order_by_id(self, alpaca_order_id: str):
        if self.lookup is None:
            raise RuntimeError("missing")
        return self.lookup

    def submit_market_order(self, intended) -> BrokerOrder:
        self.submits += 1
        now = datetime.now(timezone.utc)
        self.lookup = BrokerOrder(
            alpaca_order_id="paper-order-api-1",
            client_order_id=intended.client_order_id,
            symbol=intended.symbol,
            side=intended.side,
            quantity=intended.quantity,
            status="new",
            submitted_at=now,
            filled_quantity=0,
        )
        return self.lookup


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=100_000,
        cash=100_000,
        buying_power=100_000,
        daily_pnl_pct=0,
        current_positions={},
        source="ALPACA_PAPER",
    )


def proposal(proposal_id: str = "api-p1", **changes) -> TradeProposal:
    values = {
        "proposal_id": proposal_id,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "estimated_price": 200,
        "strategy_confidence": .9,
        "thesis": "API integration test.",
        "invalidation_condition": "Signal reverses.",
    }
    values.update(changes)
    return TradeProposal(**values)


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=.25,
        max_drawdown_30d=.08,
        liquidity_score=.99,
    )


def payload(trade: TradeProposal | None = None) -> dict:
    return {
        "proposal": (trade or proposal()).model_dump(mode="json"),
        "market_risk": market().model_dump(mode="json"),
    }


def build(tmp_path, *, config=None, adapter=None, portfolio_value=None):
    adapter = adapter or ApiAdapter()
    services = BackendServices(
        database=Database(tmp_path / "api.db"),
        ai_provider=MockAIRiskProvider(),
        portfolio_provider=StaticPortfolioProvider(portfolio_value or portfolio()),
        execution_config=config
        or ExecutionConfig(paper=True, enabled=False, dry_run=True, kill_switch=False),
        adapter_factory=lambda: adapter,
    )
    return services, adapter, create_app(services)


def evaluate(client: TestClient, trade: TradeProposal | None = None):
    return client.post("/proposals/evaluate", json=payload(trade))


def test_health_reports_safe_runtime_without_paths_or_credentials(tmp_path) -> None:
    services, _, app = build(tmp_path)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "paper_only": True,
        "execution_enabled": False,
        "dry_run": True,
        "kill_switch": False,
        "ai_provider": "MockAIRiskProvider",
        "database": {"status": "ok", "technology": "SQLite"},
    }
    assert str(services.database.path) not in response.text


def test_health_does_not_eagerly_initialize_broker_client(tmp_path) -> None:
    services = BackendServices(
        database=Database(tmp_path / "lazy-health.db"),
        ai_provider=MockAIRiskProvider(),
        execution_config=ExecutionConfig(),
    )
    with TestClient(create_app(services)) as client:
        assert client.get("/health").status_code == 200


def test_portfolio_returns_only_domain_snapshot(tmp_path) -> None:
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        response = client.get("/portfolio")
    assert response.status_code == 200
    assert response.json()["source"] == "ALPACA_PAPER"
    assert "account_id" not in response.text


def test_portfolio_broker_failure_is_structured(tmp_path) -> None:
    _, _, app = build(tmp_path, portfolio_value=RuntimeError("private detail"))
    with TestClient(app) as client:
        response = client.get("/portfolio")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BROKER_UNAVAILABLE"
    assert "private detail" not in response.text


def test_valid_evaluation_persists_complete_decision(tmp_path) -> None:
    services, _, app = build(tmp_path)
    with TestClient(app) as client:
        response = evaluate(client)
        lifecycle = client.get("/proposals/api-p1")
    assert response.status_code == 200
    assert response.json()["governor_decision"]["decision"] == "APPROVE"
    assert lifecycle.status_code == 200
    assert lifecycle.json()["risk_report"]["proposal_id"] == "api-p1"
    assert services.repository.get_ai_risk_analysis("api-p1") is not None


def test_evaluation_rejects_market_symbol_mismatch(tmp_path) -> None:
    _, _, app = build(tmp_path)
    bad = payload()
    bad["market_risk"]["symbol"] = "MSFT"
    with TestClient(app) as client:
        response = client.post("/proposals/evaluate", json=bad)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_MARKET_RISK"


def test_invalid_proposal_has_structured_validation_error(tmp_path) -> None:
    _, _, app = build(tmp_path)
    bad = payload()
    bad["proposal"]["quantity"] = 0
    bad["unexpected"] = "rejected"
    with TestClient(app) as client:
        response = client.post("/proposals/evaluate", json=bad)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["details"], list)
    assert "Traceback" not in response.text


def test_missing_proposal_is_structured(tmp_path) -> None:
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        get_response = client.get("/proposals/missing")
        execute_response = client.post("/proposals/missing/execute")
    assert get_response.status_code == 404
    assert execute_response.json()["error"]["code"] == "PROPOSAL_NOT_FOUND"


def test_execution_disabled_never_constructs_adapter(tmp_path) -> None:
    services, adapter, app = build(tmp_path)
    with TestClient(app) as client:
        assert evaluate(client).status_code == 200
        response = client.post("/proposals/api-p1/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "EXECUTION_DISABLED"
    assert adapter.submits == 0
    assert services.repository.get_execution_result("api-p1") is not None


def test_dry_run_passes_every_gate_without_submission(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=True, kill_switch=False)
    _, adapter, app = build(tmp_path, config=config)
    with TestClient(app) as client:
        evaluate(client)
        response = client.post("/proposals/api-p1/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "WOULD_SUBMIT"
    assert response.json()["quantity"] == 1
    assert adapter.submits == 0


def test_rejected_proposal_cannot_execute(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False)
    _, adapter, app = build(tmp_path, config=config)
    rejected = proposal(strategy_confidence=.1)
    with TestClient(app) as client:
        assert evaluate(client, rejected).json()["governor_decision"]["decision"] == "REJECT"
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "BLOCKED"
    assert adapter.submits == 0


def test_kill_switch_blocks_before_adapter(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=True)
    _, adapter, app = build(tmp_path, config=config)
    with TestClient(app) as client:
        evaluate(client)
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "KILL_SWITCH_ACTIVE"
    assert adapter.submits == 0


def test_market_closed_blocks_submission(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False)
    adapter = ApiAdapter()
    adapter.is_open = False
    _, adapter, app = build(tmp_path, config=config, adapter=adapter)
    with TestClient(app) as client:
        evaluate(client)
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "MARKET_CLOSED"
    assert adapter.submits == 0


def test_fresh_risk_change_requires_reauthorization(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False)
    adapter = ApiAdapter()
    _, adapter, app = build(tmp_path, config=config, adapter=adapter)
    with TestClient(app) as client:
        evaluate(client)
        adapter.daily_pnl = -.05
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "REAUTHORIZATION_REQUIRED"
    assert adapter.submits == 0


def test_stale_governor_decision_is_blocked(tmp_path) -> None:
    config = ExecutionConfig(
        paper=True, enabled=True, dry_run=True, kill_switch=False, max_decision_age_seconds=1
    )
    services, adapter, app = build(tmp_path, config=config)
    with TestClient(app) as client:
        evaluate(client)
        governor = services.repository.get_governor_decision("api-p1")
        old = governor.model_copy(
            update={"decided_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
        )
        with services.database.connection() as connection:
            connection.execute(
                "UPDATE governor_decisions SET payload_json = ?, decided_at = ? WHERE proposal_id = ?",
                (
                    json.dumps(old.model_dump(mode="json")),
                    old.decided_at.isoformat(),
                    "api-p1",
                ),
            )
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "STALE_AUTHORIZATION"
    assert adapter.submits == 0


def test_audit_endpoint_returns_durable_ordered_timeline(tmp_path) -> None:
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        evaluate(client)
        response = client.get("/proposals/api-p1/audit")
    assert [event["action"] for event in response.json()] == [
        "RISK_EVALUATED",
        "AI_RISK_ANALYZED",
        "TRADE_APPROVE",
    ]


def test_submission_then_explicit_read_only_reconciliation(tmp_path) -> None:
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False)
    services, adapter, app = build(tmp_path, config=config)
    with TestClient(app) as client:
        evaluate(client)
        submitted = client.post("/proposals/api-p1/execute")
        assert submitted.json()["status"] == "SUBMITTED"
        client_id = submitted.json()["client_order_id"]
        adapter.lookup = adapter.lookup.model_copy(
            update={
                "status": "filled",
                "filled_at": datetime.now(timezone.utc),
                "filled_quantity": 1,
                "filled_avg_price": 200.5,
            }
        )
        reconciled = client.post(f"/orders/{client_id}/reconcile")
        timeline = client.get("/proposals/api-p1/audit")
    assert reconciled.status_code == 200
    assert reconciled.json()["lifecycle_status"] == "FILLED"
    assert reconciled.json()["filled_quantity"] == 1
    assert [event["action"] for event in timeline.json()].count("ORDER_STATE_CHANGED") == 1
    assert services.repository.get_broker_order(client_id).lifecycle_status.value == "FILLED"
    assert adapter.submits == 1


def test_restart_execute_reconciles_known_order_without_duplicate(tmp_path) -> None:
    path = tmp_path / "restart-api.db"
    config = ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False)
    adapter = ApiAdapter()
    first = BackendServices(
        database=Database(path),
        ai_provider=MockAIRiskProvider(),
        portfolio_provider=StaticPortfolioProvider(portfolio()),
        execution_config=config,
        adapter_factory=lambda: adapter,
    )
    with TestClient(create_app(first)) as client:
        evaluate(client)
        original = client.post("/proposals/api-p1/execute").json()
    restarted = BackendServices(
        database=Database(path),
        ai_provider=MockAIRiskProvider(),
        portfolio_provider=StaticPortfolioProvider(portfolio()),
        execution_config=config,
        adapter_factory=lambda: adapter,
    )
    with TestClient(create_app(restarted)) as client:
        duplicate = client.post("/proposals/api-p1/execute").json()
    assert original["client_order_id"] == duplicate["client_order_id"]
    assert duplicate["status"] == "RECONCILED_EXISTING_ORDER"
    assert adapter.submits == 1


def test_api_responses_never_expose_environment_secrets(monkeypatch, tmp_path) -> None:
    markers = {
        "ALPACA_API_KEY": "alpaca-response-secret",
        "ALPACA_SECRET_KEY": "alpaca-secret-response",
        "FEATHERLESS_API_KEY": "featherless-response-secret",
    }
    for key, value in markers.items():
        monkeypatch.setenv(key, value)
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        responses = [client.get("/health"), client.get("/portfolio"), evaluate(client)]
    combined = " ".join(response.text for response in responses)
    assert not any(value in combined for value in markers.values())


def test_recent_is_bounded_and_returns_lifecycles(tmp_path) -> None:
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        evaluate(client, proposal("api-p1"))
        evaluate(client, proposal("api-p2"))
        response = client.get("/recent?limit=1")
        invalid = client.get("/recent?limit=101")
    assert len(response.json()) == 1
    assert invalid.status_code == 422


def test_live_mode_configuration_remains_non_executable(tmp_path) -> None:
    config = ExecutionConfig(paper=False, enabled=True, dry_run=False, kill_switch=False)
    _, adapter, app = build(tmp_path, config=config)
    with TestClient(app) as client:
        evaluate(client)
        response = client.post("/proposals/api-p1/execute")
    assert response.json()["status"] == "BLOCKED"
    assert "Live trading is unsupported" in response.json()["message"]
    assert adapter.submits == 0


def test_no_production_broker_mutations_outside_isolated_submit() -> None:
    app_root = Path(__file__).parents[1] / "app"
    forbidden = (
        ".cancel_order_by_id(",
        ".cancel_orders(",
        ".replace_order_by_id(",
        ".close_position(",
        ".close_all_positions(",
    )
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in app_root.rglob("*.py")
    )
    assert not any(call in production for call in forbidden)
