"""The SDK: one tenant's governed pipeline, and the boundaries it will not let a caller cross.

``evaluate`` never executes and ``execute`` never re-decides - approving and acting are separate
authorities (Barings, R-BLOW-3), and the SDK is where that separation is visible to a developer. The
rest of this module is about what a caller cannot do: hand in their own prices (F-1/F-2), propose as
another agent, reach another tenant's decisions (B3), or get past the gate by calling twice.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mizan.adapters import MockBroker
from mizan.advisory import OfflineAdvisoryProvider
from mizan.audit import InMemoryLedger
from mizan.contracts import DecisionRecord, canonical_json
from mizan.contracts.errors import (
    ConfigurationError,
    ExecutionBlocked,
    NotFound,
    ValidationFailed,
)
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import EXECUTABLE_STATUSES, Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    TENANT_B,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)


def a_broker() -> MockBroker:
    return MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )


def a_mizan(**overrides) -> Mizan:
    tenant_id = overrides.pop("tenant_id", TENANT_A)
    defaults = dict(
        tenant_id=tenant_id,
        agent=make_agent(),
        policy=make_policy(tenant_id=tenant_id),
        broker=a_broker(),
        ledger=InMemoryLedger(),
        advisory=None,
        kill_switch=InMemoryKillSwitch(),
        config=ExecutionConfig(enabled=True, dry_run=False),
        clock=lambda: FIXED_NOW,
    )
    defaults.update(overrides)
    return Mizan(**defaults)


# ---------------------------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------------------------
def test_evaluate_records_the_whole_decision_and_touches_no_broker_mutation():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal())

    assert isinstance(record, DecisionRecord)
    assert record.tenant_id == TENANT_A
    assert record.sequence == 1
    assert record.verdict in {"APPROVE", "REDUCE"}
    assert record.authorization is not None
    assert record.execution is None
    assert record.policy_snapshot.policy_hash == mizan.policy.policy_hash
    assert mizan.broker.submitted == [], "evaluate never executes"
    assert "broker.submit_order" not in mizan.broker.log
    assert mizan.verify_chain().ok is True


def test_a_rejected_proposal_is_recorded_with_codes_and_no_authorization():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal(legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "100000",
            "limit_price": "228.50",
            "order_type": "limit",
        }
    ]))

    assert record.verdict == "REJECT"
    assert record.reason_codes, "every rejection carries a machine code (A4)"
    assert record.authorization is None
    assert record.authorized.total_quantity == "0"
    with pytest.raises(ExecutionBlocked):
        mizan.execute(record.decision_id)
    assert mizan.broker.submitted == []


def test_the_caller_cannot_supply_the_prices_their_order_is_judged_against():
    """F-1/F-2: evaluate takes a proposal and nothing else. There is no market-data parameter."""
    import inspect

    parameters = set(inspect.signature(Mizan.evaluate).parameters)
    assert parameters == {"self", "proposal"}, parameters

    honest = make_proposal()
    poisoned = make_proposal(legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "10",
            "limit_price": "0.01",
            "order_type": "limit",
        }
    ])
    mizan = a_mizan()
    honest_record = mizan.evaluate(honest)
    poisoned_record = mizan.evaluate(poisoned)
    quoted = honest_record.risk_context.market_snapshot.quotes["AAPL"].price
    assert poisoned_record.risk_context.market_snapshot.quotes["AAPL"].price == quoted


def test_a_proposal_claiming_another_agent_is_refused_rather_than_governed():
    mizan = a_mizan()
    impostor = make_proposal(agent=make_agent(agent_id="somebody-elses-agent"))
    with pytest.raises(ValidationFailed):
        mizan.evaluate(impostor)
    assert mizan.list_decisions() == []


def test_a_pipeline_without_a_broker_refuses_to_evaluate_rather_than_inventing_prices():
    mizan = a_mizan(broker=None)
    with pytest.raises(ConfigurationError):
        mizan.evaluate(make_proposal())


def test_a_policy_belonging_to_another_tenant_cannot_be_wired_up_at_all():
    with pytest.raises(ConfigurationError):
        a_mizan(tenant_id=TENANT_A, policy=make_policy(tenant_id=TENANT_B))


def test_the_advisory_layer_is_optional_and_downward_only():
    """E8/E1: with and without an advisory, the deterministic evaluation is byte-identical."""
    without = a_mizan().evaluate(make_proposal())
    with_advisory = a_mizan(advisory=OfflineAdvisoryProvider()).evaluate(make_proposal())

    assert canonical_json(without.risk_evaluation) == canonical_json(with_advisory.risk_evaluation)
    assert with_advisory.authorized.total_quantity <= without.authorized.total_quantity


# ---------------------------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------------------------
def test_execute_runs_the_recorded_authorization_through_the_gate():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal())
    result = mizan.execute(record.decision_id)

    assert result.status == "SUBMITTED", (result.status, result.message)
    assert result.decision_id == record.decision_id
    assert result.auth_id == record.authorization.auth_id
    assert result.broker.environment == "paper"
    assert len(mizan.broker.submitted) == 1
    assert mizan.broker.submitted[0].client_order_id == record.authorization.idempotency_key
    assert mizan.get_execution(record.decision_id) == result


def test_a_second_execute_is_refused_because_the_authorization_is_single_use():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal())
    assert mizan.execute(record.decision_id).status == "SUBMITTED"

    second = mizan.execute(record.decision_id)
    assert second.status == "RECONCILED_EXISTING"
    assert len(mizan.broker.submitted) == 1


def test_an_expired_authorization_cannot_be_executed_later():
    """E6: the SDK does not re-mint. Come back through evaluate or do not trade."""
    clock = {"now": FIXED_NOW}
    mizan = a_mizan(clock=lambda: clock["now"])
    record = mizan.evaluate(make_proposal())

    clock["now"] = FIXED_NOW + timedelta(seconds=record.authorization.ttl_seconds + 1)
    result = mizan.execute(record.decision_id)

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in {str(code.value) for code in result.reason_codes}
    assert mizan.broker.submitted == []


def test_the_kill_switch_stops_execution_and_leaves_the_decision_intact():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal())
    mizan.kill_switch.activate()

    result = mizan.execute(record.decision_id)

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in {str(code.value) for code in result.reason_codes}
    assert mizan.broker.submitted == []
    assert mizan.get_decision(record.decision_id).verdict == record.verdict


def test_execute_on_an_unknown_decision_is_NotFound():
    with pytest.raises(NotFound):
        a_mizan().execute("01a00000-0000-7000-8000-000000000000")


# ---------------------------------------------------------------------------------------------
# protected
# ---------------------------------------------------------------------------------------------
def test_protected_runs_the_wrapped_function_only_behind_an_executable_result():
    mizan = a_mizan(config=ExecutionConfig(enabled=True, dry_run=True))
    submitted: list[str] = []

    @mizan.protected
    def submit_trade(proposal):
        submitted.append(proposal.symbol)
        return "sent"

    assert submit_trade(make_proposal()) == "sent"
    assert submitted == ["AAPL"]
    assert mizan.broker.submitted == [], "dry run: the gate checked everything and the caller submitted"
    assert mizan.list_decisions()[0].verdict in {"APPROVE", "REDUCE"}


def test_protected_never_runs_the_wrapped_function_for_a_rejected_proposal():
    mizan = a_mizan(config=ExecutionConfig(enabled=True, dry_run=True))  # @protected requires dry_run
    ran: list[str] = []

    @mizan.protected
    def submit_trade(proposal):
        ran.append(proposal.symbol)

    oversized = make_proposal(legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "100000",
            "limit_price": "228.50",
            "order_type": "limit",
        }
    ])
    with pytest.raises(ExecutionBlocked) as refusal:
        submit_trade(oversized)

    assert ran == [], "the function behind the gate never ran"
    assert refusal.value.reason_codes
    assert mizan.broker.submitted == []
    assert mizan.list_decisions()[0].verdict == "REJECT", "the refusal is still recorded"


def test_protected_never_runs_the_wrapped_function_when_the_kill_switch_is_active():
    mizan = a_mizan(config=ExecutionConfig(enabled=True, dry_run=True))
    mizan.kill_switch.activate()
    ran: list[str] = []

    @mizan.protected
    def submit_trade(proposal):
        ran.append(proposal.symbol)

    with pytest.raises(ExecutionBlocked):
        submit_trade(make_proposal())
    assert ran == []


def test_protected_keeps_the_wrapped_functions_name_and_docstring():
    mizan = a_mizan(config=ExecutionConfig(enabled=True, dry_run=True))  # @protected requires dry_run

    @mizan.protected
    def submit_trade(proposal):
        """Send it."""

    assert submit_trade.__name__ == "submit_trade"
    assert submit_trade.__doc__ == "Send it."


def test_every_executable_status_is_one_the_contract_can_actually_produce():
    from typing import get_args

    from mizan.contracts.execution_result import ExecutionStatus

    assert EXECUTABLE_STATUSES <= set(get_args(ExecutionStatus))
    assert "FAILED" not in EXECUTABLE_STATUSES and "BLOCKED" not in EXECUTABLE_STATUSES


# ---------------------------------------------------------------------------------------------
# reads and tenant scoping
# ---------------------------------------------------------------------------------------------
def test_reads_are_tenant_scoped_over_a_shared_ledger():
    shared = InMemoryLedger()
    tenant_a = a_mizan(tenant_id=TENANT_A, ledger=shared)
    tenant_b = a_mizan(tenant_id=TENANT_B, ledger=shared)

    record = tenant_a.evaluate(make_proposal())

    assert tenant_a.get_decision(record.decision_id).decision_id == record.decision_id
    for read in (tenant_b.get_decision, tenant_b.replay, tenant_b.get_execution):
        with pytest.raises(NotFound):
            read(record.decision_id)
    assert tenant_b.list_decisions() == []


def test_list_decisions_pages_newest_first():
    mizan = a_mizan()
    first = mizan.evaluate(make_proposal())
    second = mizan.evaluate(make_proposal(symbol="MSFT", strategy="long_equity"))

    listed = mizan.list_decisions(limit=10)
    assert [record.decision_id for record in listed] == [second.decision_id, first.decision_id]
    assert [record.decision_id for record in mizan.list_decisions(before_sequence=second.sequence)] == [
        first.decision_id
    ]


def test_get_execution_is_NotFound_until_something_has_been_executed():
    mizan = a_mizan()
    record = mizan.evaluate(make_proposal())
    with pytest.raises(NotFound):
        mizan.get_execution(record.decision_id)

    mizan.execute(record.decision_id)
    assert mizan.get_execution(record.decision_id).status == "SUBMITTED"
