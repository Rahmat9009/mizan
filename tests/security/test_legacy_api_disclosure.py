"""L5 security pins for the legacy HTTP surface, audit taint flow and console sinks.

Subject: ``app.api``, ``app.services``, ``app.governor``, ``app.providers.featherless_risk``,
``ui/streamlit_app.py`` (legacy, read-only salvage).

All tests are offline: the broker adapter, portfolio provider and LLM client are
in-process stubs; the database is a throw-away SQLite file under ``tmp_path``.

Findings: security/findings.md F-3 (no authentication on state-changing routes),
F-8 (LLM text -> governor reason -> unsafe HTML sink), F-9 (no local single-use
authorization; duplicate suppression delegated to the broker), F-12 (debug print of
LLM output), F-14 (internal exception text forwarded to clients), F-15 (/health
control-plane disclosure), F-17 (no tenant / agent identity anywhere).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai_risk import MockAIRiskProvider
from app.api import create_app
from app.execution.gate import ExecutionConfig
from app.execution.models import BrokerOrder, ExecutionAsset, MarketClockSnapshot
from app.governor import PortfolioGovernor
from app.models import (
    AIRiskAnalysis,
    Decision,
    MarketRiskSnapshot,
    PortfolioPosition,
    PortfolioSnapshot,
    RiskReport,
    Side,
    TradeProposal,
)
from app.persistence import Database
from app.providers.featherless_risk import FeatherlessRiskProvider
from app.services import BackendServices

REPO_ROOT = Path(__file__).resolve().parents[2]
STREAMLIT_APP = REPO_ROOT / "ui" / "streamlit_app.py"


# ---------------------------------------------------------------------------
# Offline stubs
# ---------------------------------------------------------------------------

class StaticPortfolioProvider:
    def __init__(self, snapshot: PortfolioSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self) -> PortfolioSnapshot:
        return self.snapshot


class StubAdapter:
    """Paper-adapter stub that, unlike Alpaca, does NOT enforce client_order_id uniqueness."""

    paper_mode_verified = True

    def __init__(self, *, submit_delay: float = 0.0) -> None:
        self.submits = 0
        self.submit_delay = submit_delay
        self.orders: dict[str, BrokerOrder] = {}
        self.clock_calls = 0
        self._lock = threading.Lock()

    def get_account(self):
        return SimpleNamespace(equity="100000", last_equity="100000", cash="100000", buying_power="100000")

    def get_all_positions(self):
        return []

    def get_clock(self) -> MarketClockSnapshot:
        self.clock_calls += 1
        now = datetime.now(timezone.utc)
        return MarketClockSnapshot(timestamp=now, is_open=True, next_open=now + timedelta(hours=1), next_close=now + timedelta(hours=6))

    def get_asset(self, symbol: str) -> ExecutionAsset:
        return ExecutionAsset(symbol=symbol, asset_class="us_equity", status="active", tradable=True)

    def find_order_by_client_id(self, client_order_id: str):
        return self.orders.get(client_order_id)

    def get_order_by_id(self, alpaca_order_id: str):
        for order in self.orders.values():
            if order.alpaca_order_id == alpaca_order_id:
                return order
        raise RuntimeError("missing")

    def submit_market_order(self, intended) -> BrokerOrder:
        time.sleep(self.submit_delay)
        with self._lock:
            self.submits += 1
            order = BrokerOrder(
                alpaca_order_id=f"paper-order-{self.submits}",
                client_order_id=intended.client_order_id,
                symbol=intended.symbol,
                side=intended.side,
                quantity=intended.quantity,
                status="new",
                submitted_at=datetime.now(timezone.utc),
                filled_quantity=0,
            )
            self.orders[intended.client_order_id] = order
            return order


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=100_000,
        cash=60_000,
        buying_power=100_000,
        daily_pnl_pct=0.0,
        current_positions={"MSFT": 40_000.0},
        positions=[PortfolioPosition(symbol="MSFT", quantity=100, market_value=40_000.0, current_price=400.0, unrealized_pl=1_234.5)],
        source="ALPACA_PAPER",
    )


def proposal(proposal_id: str = "sec-p1", **changes) -> TradeProposal:
    values = {
        "proposal_id": proposal_id,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "estimated_price": 200.0,
        "strategy_confidence": 0.9,
        "thesis": "Security pin.",
        "invalidation_condition": "Signal reverses.",
    }
    values.update(changes)
    return TradeProposal(**values)


def market() -> MarketRiskSnapshot:
    return MarketRiskSnapshot(symbol="AAPL", annualized_volatility=0.25, max_drawdown_30d=0.08, liquidity_score=0.99)


def payload(trade: TradeProposal | None = None) -> dict:
    return {"proposal": (trade or proposal()).model_dump(mode="json"), "market_risk": market().model_dump(mode="json")}


def build(tmp_path, *, config: ExecutionConfig | None = None, adapter: StubAdapter | None = None):
    adapter = adapter or StubAdapter()
    services = BackendServices(
        database=Database(tmp_path / "security.db"),
        ai_provider=MockAIRiskProvider(),
        portfolio_provider=StaticPortfolioProvider(portfolio()),
        execution_config=config or ExecutionConfig(paper=True, enabled=True, dry_run=True, kill_switch=False),
        adapter_factory=lambda: adapter,
    )
    return services, adapter, create_app(services)


def keys_recursive(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key).casefold())
            found |= keys_recursive(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_recursive(item)
    return found


# ---------------------------------------------------------------------------
# F-3 / F-17: no authentication, no tenant, no agent identity
# ---------------------------------------------------------------------------

def test_execute_route_is_reachable_without_any_credential(tmp_path) -> None:
    """F-3 (HIGH). A bare POST with no Authorization header traverses every gate and
    reaches the broker adapter (clock lookup proves the adapter was invoked)."""

    _, adapter, app = build(tmp_path)
    with TestClient(app) as client:
        assert client.post("/proposals/evaluate", json=payload()).status_code == 200
        response = client.post("/proposals/sec-p1/execute")  # no headers at all
    assert response.status_code == 200
    assert response.json()["status"] == "WOULD_SUBMIT"
    assert adapter.clock_calls == 1


def test_reconcile_route_is_reachable_without_any_credential(tmp_path) -> None:
    _, adapter, app = build(tmp_path, config=ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False))
    with TestClient(app) as client:
        client.post("/proposals/evaluate", json=payload())
        submitted = client.post("/proposals/sec-p1/execute").json()
        assert submitted["status"] == "SUBMITTED"
        response = client.post(f"/orders/{submitted['client_order_id']}/reconcile")
    assert response.status_code == 200
    assert adapter.submits == 1


def test_anonymous_callers_receive_positions_and_buying_power(tmp_path) -> None:
    """F-3/F-17. The whole broker account view is served to anyone who can reach the port,
    via three routes, including the global unscoped /recent listing."""

    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        client.post("/proposals/evaluate", json=payload())
        bodies = [client.get("/portfolio").json(), client.get("/proposals/sec-p1").json()["portfolio_snapshot"], client.get("/recent").json()[0]["portfolio_snapshot"]]
    for body in bodies:
        assert body["buying_power"] == 100_000
        assert body["positions"][0]["symbol"] == "MSFT"
        assert body["positions"][0]["unrealized_pl"] == 1_234.5


def test_stored_lifecycle_carries_no_tenant_or_agent_identity(tmp_path) -> None:
    """F-17 (HIGH). No key anywhere in the persisted lifecycle names a tenant or an agent;
    ``proposal_id`` is a caller-chosen global namespace. B3 is absent by construction."""

    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        client.post("/proposals/evaluate", json=payload())
        lifecycle = client.get("/proposals/sec-p1").json()
        audit = client.get("/proposals/sec-p1/audit").json()
    keys = keys_recursive(lifecycle) | keys_recursive(audit)
    assert not any("tenant" in key or "agent_id" in key or key == "agent" for key in keys)


# ---------------------------------------------------------------------------
# F-15 / F-14: disclosure through /health and error bodies
# ---------------------------------------------------------------------------

def test_health_discloses_control_plane_state_to_anonymous_callers(tmp_path) -> None:
    """F-15 (LOW). Unauthenticated recon: execution flags, kill-switch state, AI vendor, DB technology."""

    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["execution_enabled"] is True
    assert body["dry_run"] is True
    assert body["kill_switch"] is False
    assert body["ai_provider"] == "MockAIRiskProvider"
    assert body["database"]["technology"] == "SQLite"


def test_conflict_error_forwards_internal_table_name(tmp_path) -> None:
    """F-14 (LOW). ``ServiceError("DATABASE_ERROR", str(exc))`` forwards the persistence
    layer's message verbatim, which names the SQLite table."""

    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        assert client.post("/proposals/evaluate", json=payload()).status_code == 200
        response = client.post("/proposals/evaluate", json=payload(proposal(thesis="different content")))
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "DATABASE_ERROR"
    assert "proposals already contains different data" in error["message"]


def test_error_bodies_never_contain_environment_secrets(tmp_path, monkeypatch) -> None:
    """Positive pin (carry forward): error paths use type names, never secret values."""

    markers = {
        "ALPACA_API_KEY": "alpaca-key-marker-for-errors",
        "ALPACA_SECRET_KEY": "alpaca-secret-marker-for-errors",
        "FEATHERLESS_API_KEY": "featherless-marker-for-errors",
        "ANTHROPIC_API_KEY": "anthropic-marker-for-errors",
        "APP_DB_PATH": str(tmp_path / "path-marker-should-not-leak.db"),
    }
    for key, value in markers.items():
        monkeypatch.setenv(key, value)
    _, _, app = build(tmp_path)
    with TestClient(app) as client:
        client.post("/proposals/evaluate", json=payload())
        responses = [
            client.get("/proposals/does-not-exist"),
            client.post("/proposals/does-not-exist/execute"),
            client.get("/orders/does-not-exist"),
            client.post("/proposals/evaluate", json={"proposal": {"symbol": "AAPL"}, "market_risk": {}}),
            client.post("/proposals/evaluate", json=payload(proposal(thesis="conflict"))),
            client.get("/health"),
        ]
    combined = " ".join(response.text for response in responses)
    for value in markers.values():
        assert value not in combined
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# F-9: concurrent execution has no local single-use guard
# ---------------------------------------------------------------------------

def test_concurrent_executes_both_reach_submit_when_broker_does_not_dedupe(tmp_path) -> None:
    """F-9 (MEDIUM). Two concurrent POST /execute for one proposal both pass every gate
    and both call ``submit_market_order``. Legacy duplicate suppression relies on Alpaca
    rejecting a reused client_order_id plus the post-failure reconcile; there is no
    local atomic ``consume(auth_id)`` (API-SURFACE §3.5/§3.8 step 5)."""

    adapter = StubAdapter(submit_delay=0.3)
    services, _, _ = build(tmp_path, config=ExecutionConfig(paper=True, enabled=True, dry_run=False, kill_switch=False), adapter=adapter)
    services.evaluate(proposal("race-p1"), market())

    results = []
    workers = [threading.Thread(target=lambda: results.append(services.execute_proposal("race-p1"))) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert adapter.submits == 2
    assert sorted(result.status.value for result in results) == ["SUBMITTED", "SUBMITTED"]


# ---------------------------------------------------------------------------
# F-8: LLM-authored text is a taint source for an HTML sink in the console
# ---------------------------------------------------------------------------

def _report(blocked: bool = False) -> RiskReport:
    return RiskReport(proposal_id="xss-p1", symbol="AAPL", original_quantity=10, recommended_quantity=10, blocked=blocked, risk_score=0, reasons=["ok"], checks=[])


def test_llm_reasoning_flows_verbatim_into_governor_reason() -> None:
    """F-8 (MEDIUM). ``PortfolioGovernor.decide`` joins ``ai_risk.reasoning`` into
    ``GovernorDecision.reason`` with no escaping; that field is persisted, served by the
    API, and rendered by the console (see next test)."""

    marker = "<img src=x onerror=alert(1)>"
    analysis = AIRiskAnalysis(
        proposal_id="xss-p1",
        recommendation=Decision.REDUCE,
        confidence=0.5,
        recommended_quantity=5,
        risk_thesis="t",
        hidden_risks=[],
        reasoning=[marker],
        model_name="m",
    )
    decision = PortfolioGovernor().decide(proposal("xss-p1", quantity=10), _report(), analysis)
    assert marker in decision.reason


def test_streamlit_renders_tainted_fields_with_unsafe_allow_html() -> None:
    """F-8 continued: static pin of the sinks in ui/streamlit_app.py."""

    source = STREAMLIT_APP.read_text(encoding="utf-8")
    sinks = [
        'governor.get("reason", "")',   # LLM reasoning joined by the governor
        "check.get('message', '')",     # risk-engine check text
        "str(message)[:300]",           # audit payload message / reason / risk_thesis
    ]
    for sink in sinks:
        index = source.index(sink)
        window = source[index : index + 400]
        assert "unsafe_allow_html=True" in window, sink


# ---------------------------------------------------------------------------
# Streamlit session state: no credentials (positive pin)
# ---------------------------------------------------------------------------

def test_streamlit_session_state_and_environment_hold_no_credentials() -> None:
    source = STREAMLIT_APP.read_text(encoding="utf-8")
    env_reads = set(re.findall(r'os\.getenv\("([A-Z_]+)"', source))
    assert env_reads == {"MIZAN_BACKEND_URL", "MIZAN_BACKEND_TIMEOUT"}
    state_keys = set(re.findall(r'st\.session_state(?:\[|\.setdefault\(|\.get\(|\.pop\()"([a-z_]+)"', source))
    assert state_keys == {"form_defaults", "execution_result", "refresh_lifecycle", "lifecycle", "proposal_id", "base_url", "timeout", "max_age"}
    assert "localStorage" not in source
    # The lifecycle object in session_state DOES carry the portfolio snapshot (positions,
    # buying power) in the Streamlit server's memory; that is data, not a credential.


# ---------------------------------------------------------------------------
# F-12: FEATHERLESS_DEBUG prints LLM output to stdout via print()
# ---------------------------------------------------------------------------

def test_debug_flag_prints_llm_reasoning_to_stdout(monkeypatch, capsys) -> None:
    """F-12 (MEDIUM). The provider has no logging framework; with FEATHERLESS_DEBUG=true the
    full parsed model output (risk_thesis, reasoning, hidden_risks) goes to stdout via
    ``print``. It cannot be routed, levelled or redacted centrally."""

    monkeypatch.setenv("FEATHERLESS_DEBUG", "true")
    thesis_marker = "model-authored-thesis-text-lands-on-stdout"
    body = {
        "recommendation": "APPROVE",
        "confidence": 0.7,
        "recommended_quantity": 1,
        "risk_thesis": thesis_marker,
        "hidden_risks": ["hr"],
        "reasoning": ["model reasoning line"],
    }
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body), tool_calls=None))],
        usage=None,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    provider = FeatherlessRiskProvider(client=client, model="test/model")

    trade = proposal("debug-p1")
    report = RiskReport(proposal_id="debug-p1", symbol="AAPL", original_quantity=1, recommended_quantity=1, blocked=False, risk_score=0, reasons=["ok"], checks=[])
    analysis = provider.analyze(trade, portfolio(), market(), report)
    out = capsys.readouterr().out

    assert analysis.recommendation is Decision.APPROVE
    assert "[FEATHERLESS DEBUG]" in out
    assert thesis_marker in out
    assert "model reasoning line" in out
