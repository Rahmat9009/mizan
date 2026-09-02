from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError

from app.alpaca.execution import (
    AlpacaExecutionError,
    AlpacaOrderNotFoundError,
    PaperExecutionAlpacaAdapter,
)
from app.audit import InMemoryAuditLog
from app.execution.gate import (
    ExecutionConfig,
    ExecutionConfigurationError,
    ExecutionGate,
)
from app.execution.models import (
    BrokerOrder,
    ExecutionAsset,
    ExecutionAuthorization,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    MarketClockSnapshot,
)
from app.execution.service import ControlledPaperExecutionService, deterministic_client_order_id
from app.models import Decision, GovernorDecision, MarketRiskSnapshot, PortfolioSnapshot, Side, TradeProposal
from app.risk_engine import RiskEngine


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def proposal(**updates) -> TradeProposal:
    value = TradeProposal(
        proposal_id="proposal-execution-123",
        symbol="AAPL",
        side="BUY",
        quantity=10,
        estimated_price=100,
        strategy_confidence=0.85,
        thesis="Fictional execution test thesis.",
        invalidation_condition="Fictional strategy signal reverses.",
    )
    return value.model_copy(update=updates)


def market(**updates) -> MarketRiskSnapshot:
    value = MarketRiskSnapshot(
        symbol="AAPL",
        annualized_volatility=0.30,
        max_drawdown_30d=0.10,
        liquidity_score=0.95,
    )
    return value.model_copy(update=updates)


def portfolio(**updates) -> PortfolioSnapshot:
    value = PortfolioSnapshot(
        equity=100_000,
        cash=100_000,
        buying_power=100_000,
        daily_pnl_pct=0.0,
        current_positions={},
    )
    return value.model_copy(update=updates)


def hard_report(trade: TradeProposal | None = None, snapshot: PortfolioSnapshot | None = None):
    return RiskEngine().evaluate(trade or proposal(), snapshot or portfolio(), market())


def governor(trade: TradeProposal | None = None, **updates) -> GovernorDecision:
    trade = trade or proposal()
    values = {
        "proposal_id": trade.proposal_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "decision": Decision.APPROVE,
        "original_quantity": trade.quantity,
        "approved_quantity": trade.quantity,
        "reason": "Authorized by test Governor.",
        "risk_score": 0,
        "decided_at": NOW,
    }
    values.update(updates)
    return GovernorDecision(**values)


def broker_order(trade: TradeProposal | None = None, **updates) -> BrokerOrder:
    trade = trade or proposal()
    values = {
        "alpaca_order_id": "alpaca-order-123",
        "client_order_id": deterministic_client_order_id(trade.proposal_id),
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "status": "accepted",
        "submitted_at": NOW,
        "filled_at": None,
        "filled_quantity": "0",
        "filled_avg_price": None,
    }
    values.update(updates)
    return BrokerOrder(**values)


class FakeAdapter:
    paper_mode_verified = True

    def __init__(self) -> None:
        self.account = SimpleNamespace(
            equity="100000",
            last_equity="100000",
            cash="100000",
            buying_power="100000",
        )
        self.positions = []
        self.asset = ExecutionAsset(
            symbol="AAPL", asset_class="us_equity", status="active", tradable=True
        )
        self.clock = MarketClockSnapshot(
            timestamp=NOW,
            is_open=True,
            next_open=NOW + timedelta(days=1),
            next_close=NOW + timedelta(hours=2),
        )
        self.lookup_results: list[BrokerOrder | None | Exception] = [None]
        self.submission: BrokerOrder | Exception = broker_order()
        self.submit_calls = []
        self.account_calls = 0
        self.asset_calls = 0
        self.clock_calls = 0

    def get_account(self):
        self.account_calls += 1
        if isinstance(self.account, Exception):
            raise self.account
        return self.account

    def get_all_positions(self):
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    def get_clock(self):
        self.clock_calls += 1
        if isinstance(self.clock, Exception):
            raise self.clock
        return self.clock

    def get_asset(self, symbol):
        self.asset_calls += 1
        if isinstance(self.asset, Exception):
            raise self.asset
        return self.asset

    def find_order_by_client_id(self, client_order_id):
        result = self.lookup_results.pop(0) if len(self.lookup_results) > 1 else self.lookup_results[0]
        if isinstance(result, Exception):
            raise result
        return result

    def submit_market_order(self, intended):
        self.submit_calls.append(intended)
        if isinstance(self.submission, Exception):
            raise self.submission
        return self.submission


def service(
    adapter: FakeAdapter,
    *,
    enabled: bool = True,
    dry_run: bool = True,
    paper: bool = True,
    kill_switch: bool = False,
    now_provider=None,
    audit: InMemoryAuditLog | None = None,
) -> ControlledPaperExecutionService:
    return ControlledPaperExecutionService(
        ExecutionConfig(
            paper=paper,
            enabled=enabled,
            dry_run=dry_run,
            kill_switch=kill_switch,
            max_decision_age_seconds=120,
        ),
        adapter_factory=lambda: adapter,
        now_provider=now_provider or (lambda: NOW),
        audit=audit,
    )


def execute_with(
    adapter: FakeAdapter,
    *,
    trade: TradeProposal | None = None,
    report=None,
    decision=None,
    market_value=None,
    **service_options,
):
    trade = trade or proposal()
    report = report or hard_report(trade)
    decision = decision or governor(trade)
    market_value = market_value or market()
    runner = service(adapter, **service_options)
    return runner.execute(trade, report, decision, market_value), runner


def test_execution_disabled_never_creates_adapter_or_submits() -> None:
    adapter = FakeAdapter()
    factory = Mock(return_value=adapter)
    runner = ControlledPaperExecutionService(
        ExecutionConfig(enabled=False), adapter_factory=factory, now_provider=lambda: NOW
    )

    result = runner.execute(proposal(), hard_report(), governor(), market())

    assert result.status == ExecutionState.DISABLED
    factory.assert_not_called()
    assert adapter.submit_calls == []


def test_paper_false_rejected_before_adapter_creation() -> None:
    factory = Mock()
    runner = ControlledPaperExecutionService(
        ExecutionConfig(paper=False, enabled=True, dry_run=False),
        adapter_factory=factory,
        now_provider=lambda: NOW,
    )

    result = runner.execute(proposal(), hard_report(), governor(), market())

    assert result.status == ExecutionState.BLOCKED
    assert "Live trading is unsupported" in result.message
    factory.assert_not_called()


def test_kill_switch_blocks_every_new_execution() -> None:
    adapter = FakeAdapter()
    result, _ = execute_with(adapter, kill_switch=True, dry_run=False)
    assert result.status == ExecutionState.KILL_SWITCH_ACTIVE
    assert adapter.submit_calls == []


@pytest.mark.parametrize(
    "decision",
    [
        governor(decision=Decision.REJECT, approved_quantity=0),
        governor(decision=Decision.APPROVE, approved_quantity=0),
        governor(proposal_id="wrong-proposal"),
        governor(symbol="MSFT"),
        governor(side=Side.SELL),
    ],
)
def test_invalid_governor_authority_never_reaches_adapter(decision) -> None:
    adapter = FakeAdapter()
    result, _ = execute_with(adapter, decision=decision, dry_run=False)
    assert result.status == ExecutionState.BLOCKED
    assert adapter.submit_calls == []
    assert adapter.account_calls == 0


def test_stale_governor_decision_is_rejected() -> None:
    adapter = FakeAdapter()
    decision = governor(decided_at=NOW - timedelta(seconds=121))
    result, _ = execute_with(adapter, decision=decision, dry_run=False)
    assert result.status == ExecutionState.STALE_AUTHORIZATION
    assert adapter.submit_calls == []


def test_market_closed_rejects_without_submission() -> None:
    adapter = FakeAdapter()
    adapter.clock = adapter.clock.model_copy(update={"is_open": False})
    result, _ = execute_with(adapter, dry_run=False)
    assert result.status == ExecutionState.MARKET_CLOSED
    assert adapter.submit_calls == []


@pytest.mark.parametrize(
    "asset",
    [
        ExecutionAsset(symbol="AAPL", asset_class="us_equity", status="active", tradable=False),
        ExecutionAsset(symbol="AAPL", asset_class="crypto", status="active", tradable=True),
        ExecutionAsset(symbol="AAPL", asset_class="us_equity", status="inactive", tradable=True),
        ExecutionAsset(symbol="MSFT", asset_class="us_equity", status="active", tradable=True),
    ],
)
def test_untradable_or_wrong_asset_is_rejected(asset) -> None:
    adapter = FakeAdapter()
    adapter.asset = asset
    result, _ = execute_with(adapter, dry_run=False)
    assert result.status == ExecutionState.ASSET_NOT_TRADABLE
    assert adapter.submit_calls == []


def test_fresh_portfolio_hard_block_requires_reauthorization() -> None:
    adapter = FakeAdapter()
    adapter.account = SimpleNamespace(
        equity="94000", last_equity="100000", cash="94000", buying_power="94000"
    )
    result, _ = execute_with(adapter, dry_run=False)
    assert result.status == ExecutionState.REAUTHORIZATION_REQUIRED
    assert adapter.submit_calls == []


def test_fresh_portfolio_lower_quantity_requires_reauthorization() -> None:
    trade = proposal(quantity=10, estimated_price=1000)
    initial = hard_report(trade, portfolio(equity=100_000))
    assert initial.recommended_quantity == 10
    adapter = FakeAdapter()
    adapter.account = SimpleNamespace(
        equity="50000", last_equity="50000", cash="50000", buying_power="50000"
    )

    result, _ = execute_with(
        adapter,
        trade=trade,
        report=initial,
        decision=governor(trade),
        dry_run=False,
    )

    assert result.status == ExecutionState.REAUTHORIZATION_REQUIRED
    assert adapter.submit_calls == []


def test_missing_fresh_daily_pnl_requires_reauthorization() -> None:
    adapter = FakeAdapter()
    adapter.account = SimpleNamespace(
        equity="100000", last_equity=None, cash="100000", buying_power="100000"
    )
    result, _ = execute_with(adapter, dry_run=False)
    assert result.status == ExecutionState.REAUTHORIZATION_REQUIRED
    assert adapter.submit_calls == []


def test_valid_approve_dry_run_reaches_would_submit_without_mutation() -> None:
    adapter = FakeAdapter()
    result, runner = execute_with(adapter, dry_run=True)

    assert result.status == ExecutionState.WOULD_SUBMIT
    assert result.execution_mode == ExecutionMode.PAPER_DRY_RUN
    assert result.client_order_id == deterministic_client_order_id(proposal().proposal_id)
    assert result.quantity == 10
    assert result.time_in_force == "day"
    assert result.extended_hours is False
    assert adapter.submit_calls == []
    actions = [event.action for event in runner.audit.list_for_proposal(proposal().proposal_id)]
    assert actions == [
        "EXECUTION_AUTHORIZATION_CREATED",
        "EXECUTION_GATE_PASSED",
        "ORDER_IDEMPOTENCY_CHECKED",
        "EXECUTION_FRESH_RISK_EVALUATED",
        "ORDER_DRY_RUN_READY",
    ]


def test_valid_approve_submits_exact_day_market_order() -> None:
    adapter = FakeAdapter()
    result, runner = execute_with(adapter, dry_run=False)

    assert result.status == ExecutionState.SUBMITTED
    assert result.execution_mode == ExecutionMode.PAPER
    assert result.broker_status == "accepted"
    assert result.alpaca_order_id == "alpaca-order-123"
    assert len(adapter.submit_calls) == 1
    intended = adapter.submit_calls[0]
    assert intended.symbol == "AAPL"
    assert intended.side == Side.BUY
    assert intended.quantity == 10
    assert intended.time_in_force == "day"
    assert intended.extended_hours is False
    actions = [event.action for event in runner.audit.list_for_proposal(proposal().proposal_id)]
    assert "ORDER_SUBMITTING" in actions
    assert "ORDER_SUBMITTED" in actions


def test_valid_reduce_submits_only_governor_quantity() -> None:
    adapter = FakeAdapter()
    adapter.submission = broker_order(quantity=4)
    decision = governor(decision=Decision.REDUCE, approved_quantity=4)
    result, _ = execute_with(adapter, decision=decision, dry_run=False)

    assert result.status == ExecutionState.SUBMITTED
    assert result.quantity == 4
    assert adapter.submit_calls[0].quantity == 4


def test_malicious_million_share_governor_is_blocked() -> None:
    adapter = FakeAdapter()
    decision = governor(approved_quantity=1_000_000)
    result, _ = execute_with(adapter, decision=decision, dry_run=False)
    assert result.status == ExecutionState.BLOCKED
    assert adapter.submit_calls == []
    assert adapter.account_calls == 0


def test_deterministic_client_order_id_is_stable_bounded_and_proposal_specific() -> None:
    first = deterministic_client_order_id("proposal-a")
    assert first == deterministic_client_order_id("proposal-a")
    assert first != deterministic_client_order_id("proposal-b")
    assert first.startswith("pgv5-")
    assert len(first) == 45


def test_duplicate_reconciles_without_fresh_reads_or_submission() -> None:
    adapter = FakeAdapter()
    adapter.lookup_results = [broker_order()]
    result, runner = execute_with(adapter, dry_run=False)

    assert result.status == ExecutionState.RECONCILED_EXISTING_ORDER
    assert adapter.submit_calls == []
    assert adapter.account_calls == 0
    actions = [event.action for event in runner.audit.list_for_proposal(proposal().proposal_id)]
    assert "ORDER_RECONCILED" in actions


def test_duplicate_with_mismatched_order_fails_closed() -> None:
    adapter = FakeAdapter()
    adapter.lookup_results = [broker_order(symbol="MSFT")]
    result, _ = execute_with(adapter, dry_run=False)
    assert result.status == ExecutionState.FAILED
    assert adapter.submit_calls == []


def test_submit_race_or_timeout_reconciles_by_client_id() -> None:
    adapter = FakeAdapter()
    adapter.lookup_results = [None, broker_order()]
    adapter.submission = TimeoutError("unknown submit outcome")
    result, runner = execute_with(adapter, dry_run=False)

    assert result.status == ExecutionState.RECONCILED_EXISTING_ORDER
    assert len(adapter.submit_calls) == 1
    actions = [event.action for event in runner.audit.list_for_proposal(proposal().proposal_id)]
    assert "ORDER_SUBMISSION_FAILED" in actions
    assert "ORDER_RECONCILED" in actions


@pytest.mark.parametrize(
    "failure_point",
    ["lookup", "portfolio", "asset", "clock", "submit"],
)
def test_alpaca_provider_errors_fail_safely(failure_point: str) -> None:
    adapter = FakeAdapter()
    if failure_point == "lookup":
        adapter.lookup_results = [TimeoutError("lookup failed")]
    elif failure_point == "portfolio":
        adapter.account = TimeoutError("account failed")
    elif failure_point == "asset":
        adapter.asset = TimeoutError("asset failed")
    elif failure_point == "clock":
        adapter.clock = TimeoutError("clock failed")
    else:
        adapter.submission = TimeoutError("submit failed")
        adapter.lookup_results = [None, None]

    result, _ = execute_with(adapter, dry_run=False)

    assert result.status == ExecutionState.FAILED
    if failure_point != "submit":
        assert adapter.submit_calls == []


def test_sell_without_long_position_cannot_create_short() -> None:
    trade = proposal(side=Side.SELL)
    adapter = FakeAdapter()
    adapter.submission = broker_order(trade)
    result, _ = execute_with(
        adapter,
        trade=trade,
        report=hard_report(trade),
        decision=governor(trade),
        market_value=market(),
        dry_run=False,
    )
    assert result.status == ExecutionState.REAUTHORIZATION_REQUIRED
    assert adapter.submit_calls == []


def test_tampered_authorization_symbol_is_blocked() -> None:
    trade = proposal()
    authorization = ExecutionAuthorization(
        proposal_id=trade.proposal_id,
        symbol="MSFT",
        side=trade.side,
        original_quantity=trade.quantity,
        approved_quantity=trade.quantity,
        governor_decision=Decision.APPROVE,
        governor_decided_at=NOW,
        authorization_created_at=NOW,
        risk_score=0,
    )
    adapter = FakeAdapter()
    runner = service(adapter, dry_run=False)
    result = runner.execute(
        trade,
        hard_report(trade),
        governor(trade),
        market(),
        authorization=authorization,
    )
    assert result.status == ExecutionState.BLOCKED
    assert adapter.submit_calls == []


def test_prebuilt_authorization_cannot_hide_mismatched_governor_identity() -> None:
    trade = proposal()
    decision = governor(trade)
    authorization = ExecutionAuthorization(
        proposal_id=trade.proposal_id,
        symbol=trade.symbol,
        side=trade.side,
        original_quantity=trade.quantity,
        approved_quantity=trade.quantity,
        governor_decision=Decision.APPROVE,
        governor_decided_at=decision.decided_at,
        authorization_created_at=NOW,
        risk_score=decision.risk_score,
    )
    adapter = FakeAdapter()
    runner = service(adapter, dry_run=False)
    forged_governor = decision.model_copy(update={"proposal_id": "different-proposal"})
    result = runner.execute(
        trade,
        hard_report(trade),
        forged_governor,
        market(),
        authorization=authorization,
    )
    assert result.status == ExecutionState.BLOCKED
    assert adapter.submit_calls == []


def test_execution_result_validation_rejects_impossible_submitted_state() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(
            proposal_id="p",
            symbol="AAPL",
            side=Side.BUY,
            quantity=1,
            status=ExecutionState.SUBMITTED,
            execution_mode=ExecutionMode.PAPER,
            message="Missing broker identity.",
        )


def test_audit_payload_redacts_api_secrets() -> None:
    audit = InMemoryAuditLog()
    audit.append_execution(
        "p",
        "TEST",
        {
            "api_key": "do-not-log",
            "authorization_created_at": "2026-09-01T00:00:00Z",
            "nested": {"authorization_header": "do-not-log", "safe": "ok"},
        },
    )
    payload = audit.list_for_proposal("p")[0].payload
    serialized = json.dumps(payload)
    assert "do-not-log" not in serialized
    assert payload["api_key"] == "[REDACTED]"
    assert payload["authorization_created_at"] == "2026-09-01T00:00:00Z"
    assert payload["nested"]["safe"] == "ok"


def test_execution_adapter_requires_provable_paper_base_url() -> None:
    live_client = Mock()
    live_client._base_url = "https://api.alpaca.markets"
    with pytest.raises(AlpacaExecutionError, match="cannot prove"):
        PaperExecutionAlpacaAdapter(live_client)


def test_execution_adapter_accepts_alpaca_sdk_paper_base_enum() -> None:
    paper_client = Mock()
    paper_client._base_url = BaseURL.TRADING_PAPER
    assert PaperExecutionAlpacaAdapter(paper_client).paper_mode_verified is True


def test_execution_adapter_constructs_exact_market_order_request() -> None:
    sdk = Mock()
    sdk._base_url = BaseURL.TRADING_PAPER.value
    sdk.submit_order.return_value = SimpleNamespace(
        id="order-1",
        client_order_id="pgv5-" + "a" * 40,
        symbol="AAPL",
        side="buy",
        qty="1",
        status="new",
        submitted_at=NOW,
        filled_at=None,
        filled_qty="0",
        filled_avg_price=None,
    )
    adapter = PaperExecutionAlpacaAdapter(sdk)
    intended = SimpleNamespace(
        proposal_id="p",
        client_order_id="pgv5-" + "a" * 40,
        symbol="AAPL",
        side=Side.BUY,
        quantity=1,
    )

    mapped = adapter.submit_market_order(intended)

    request = sdk.submit_order.call_args.kwargs["order_data"]
    assert request.symbol == "AAPL"
    assert request.qty == 1
    assert request.side.value == "buy"
    assert request.time_in_force.value == "day"
    assert request.extended_hours is False
    assert request.client_order_id == intended.client_order_id
    assert mapped.alpaca_order_id == "order-1"


def api_error(status: int) -> APIError:
    http_error = SimpleNamespace(
        response=SimpleNamespace(status_code=status), request=SimpleNamespace()
    )
    return APIError(json.dumps({"code": 40410000, "message": "not found"}), http_error)


def test_execution_adapter_maps_order_lookup_404_to_none() -> None:
    sdk = Mock()
    sdk._base_url = BaseURL.TRADING_PAPER.value
    sdk.get_order_by_client_id.side_effect = api_error(404)
    adapter = PaperExecutionAlpacaAdapter(sdk)
    assert adapter.find_order_by_client_id("pgv5-" + "a" * 40) is None


def test_execution_adapter_maps_order_id_404_to_typed_not_found() -> None:
    sdk = Mock()
    sdk._base_url = BaseURL.TRADING_PAPER.value
    sdk.get_order_by_id.side_effect = api_error(404)
    adapter = PaperExecutionAlpacaAdapter(sdk)
    with pytest.raises(AlpacaOrderNotFoundError):
        adapter.get_order_by_id("missing-order")


def test_config_defaults_are_disabled_dry_run_paper(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("ALPACA_EXECUTION_DRY_RUN", raising=False)
    monkeypatch.delenv("ALPACA_EXECUTION_KILL_SWITCH", raising=False)
    monkeypatch.delenv("EXECUTION_MAX_DECISION_AGE_SECONDS", raising=False)
    monkeypatch.setenv("ALPACA_PAPER", "true")
    config = ExecutionConfig.from_environment()
    assert config.paper is True
    assert config.enabled is False
    assert config.dry_run is True
    assert config.kill_switch is False
    assert config.max_decision_age_seconds == 120


def test_invalid_execution_environment_values_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_EXECUTION_ENABLED", "maybe")
    with pytest.raises(ExecutionConfigurationError):
        ExecutionConfig.from_environment()


def test_submit_order_is_isolated_to_one_production_adapter() -> None:
    app_root = Path(__file__).parents[1] / "app"
    containing = []
    for path in app_root.rglob("*.py"):
        if ".submit_order(" in path.read_text(encoding="utf-8"):
            containing.append(path.relative_to(app_root).as_posix())
    assert containing == ["alpaca/execution.py"]
