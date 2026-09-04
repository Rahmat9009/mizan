"""Production host: same-origin SPA plus a server-authenticated, read-only API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mizan.execution import LiveTradingForbidden
from scripts.run_operator_api import _build_app, _parser


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<title>Mizan production</title>", encoding="utf-8")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("MIZAN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("MIZAN_EXECUTION_DRY_RUN", "true")
    monkeypatch.delenv("MIZAN_API_TOKEN", raising=False)
    args = _parser().parse_args(
        ["--ledger", str(tmp_path / "ledger"), "--serve-frontend", str(frontend)]
    )
    return TestClient(_build_app(args))


def test_spa_routes_refresh_to_the_built_index(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert "Mizan production" in root_resp.text

    response = client.get("/app/proposals/nvda-example")
    assert response.status_code == 200
    assert "Mizan production" in response.text


def test_all_spa_routes_direct_navigation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    routes = [
        "/",
        "/app",
        "/app/proposals",
        "/app/proposals/",
        "/app/portfolio",
        "/app/risk",
        "/app/crowding",
        "/app/orders",
        "/app/audit",
        "/app/agents",
        "/unknown-frontend-route",
    ]
    for route in routes:
        resp = client.get(route)
        assert resp.status_code == 200, f"Failed for route {route}"
        assert "Mizan production" in resp.text


def test_spa_fallback_preserves_404_for_unknown_api_routes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/v1/unknown-endpoint")
    assert response.status_code == 404
    assert response.headers.get("content-type", "").startswith("application/json")
    assert response.json() == {"error": {"code": "not_found"}}


def test_gateway_adds_an_internal_token_and_keeps_execution_disabled(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/v1/health", headers={"Authorization": "Bearer attacker-value"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "tenant_id": "tenant-a",
        "environment": "paper",
        "execution": {"enabled": False, "dry_run": True, "kill_switch_active": False},
        "broker": None,
    }
    # Invariant: internal token is never leaked in headers or body
    assert "authorization" not in response.headers
    assert "Bearer" not in response.text


def test_public_gateway_has_no_control_or_execute_scope(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    # Control scope rejected
    resp_control = client.post("/api/v1/control/kill-switch", json={"active": True})
    assert resp_control.status_code == 403

    # Execute scope rejected
    resp_execute = client.post("/api/v1/decisions/dec-test/execute")
    assert resp_execute.status_code == 403

    # Evaluate scope rejected
    resp_eval = client.post("/api/v1/proposals/evaluate", json={})
    assert resp_eval.status_code == 403


def test_missing_built_frontend_fails_clearly(tmp_path, monkeypatch):
    missing = tmp_path / "nonexistent_dist"
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("MIZAN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("MIZAN_EXECUTION_DRY_RUN", "true")
    args = _parser().parse_args(
        ["--ledger", str(tmp_path / "ledger"), "--serve-frontend", str(missing)]
    )
    with pytest.raises(SystemExit, match="Frontend build not found"):
        _build_app(args)


def test_paper_safe_settings_enforced(tmp_path, monkeypatch):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<title>Mizan</title>", encoding="utf-8")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    args = _parser().parse_args(
        ["--ledger", str(tmp_path / "ledger"), "--serve-frontend", str(frontend)]
    )
    with pytest.raises(LiveTradingForbidden):
        _build_app(args)

