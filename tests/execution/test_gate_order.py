"""The gate's check order, proven by a call log rather than by reading the source.

Hard Rule E4 is an *ordering* claim: the kill switch is consulted immediately before the mutation, not
at request entry. An outcome test cannot tell the two apart - a gate that reads the switch first and
happens to return BLOCKED looks identical from the outside. So every call the gate makes to the broker
and to the switch is appended to one shared log, and the assertions are about positions in that log.

This module is self-contained on purpose: it builds its own chain and its own gate, so it asserts what
the shipped code does and cannot be quietly re-pointed by a change to a shared fixture.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from mizan import authorization as authorization_module
from mizan import governor, risk
from mizan.adapters import BrokerContextProvider, MockBroker
from mizan.authorization import InMemoryAuthorizationRegistry
from mizan.contracts import (
    ExecutionAuthorization,
    GovernorDecision,
    Policy,
    RiskContext,
    TradeProposal,
    dec,
)
from mizan.contracts.errors import BrokerError
from mizan.execution import CHECK_ORDER, ExecutionConfig, ExecutionGate
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

READS = frozenset({"broker.get_portfolio_snapshot", "broker.get_market_snapshot", "broker.find_order"})


# ---------------------------------------------------------------------------------------------
# wiring (local to this module; see the docstring)
# ---------------------------------------------------------------------------------------------
class EventKillSwitch:
    """A kill switch that writes every consultation to the shared log. Ordering is the whole point."""

    def __init__(self, *, active: bool = False, log: list[str] | None = None) -> None:
        self.active = active
        self.log = log if log is not None else []
        self.calls = 0

    def is_active(self) -> bool:
        self.calls += 1
        self.log.append("kill_switch")
        return self.active


class SteppingClock:
    """A clock the test drives: ``advance_after(n, delta)`` moves time on the nth reading."""

    def __init__(self, start: datetime = FIXED_NOW) -> None:
        self.now = start
        self.readings = 0
        self._schedule: dict[int, timedelta] = {}

    def advance_after(self, reading: int, delta: timedelta) -> None:
        self._schedule[reading] = delta

    def __call__(self) -> datetime:
        self.readings += 1
        value = self.now
        delta = self._schedule.pop(self.readings, None)
        if delta is not None:
            self.now = self.now + delta
        return value


@dataclass
class Chain:
    policy: Policy
    proposal: TradeProposal
    context: RiskContext
    decision: GovernorDecision
    auth: ExecutionAuthorization
    broker: MockBroker
    provider: BrokerContextProvider

    @property
    def log(self) -> list[str]:
        return self.broker.log

    @property
    def scope_quantity(self) -> Decimal:
        return dec(self.auth.scope.total_quantity)


def a_chain(*, policy: Policy | None = None, proposal: TradeProposal | None = None) -> Chain:
    """A full, engine-produced chain: exactly what a real evaluation would have left behind."""
    policy = policy if policy is not None else make_policy()
    proposal = proposal if proposal is not None else make_proposal()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    provider = BrokerContextProvider(broker)
    context = provider.build(
        tenant_id=policy.tenant_id, agent_id=AGENT_ID, proposal=proposal, policy=policy, now=FIXED_NOW
    )
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", evaluation.reason_codes
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization_module.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    broker.log.clear()  # the chain's own reads are setup, not gate behaviour
    return Chain(
        policy=policy,
        proposal=proposal,
        context=context,
        decision=decision,
        auth=auth,
        broker=broker,
        provider=provider,
    )


def a_gate(
    chain: Chain,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    kill_switch: Any = None,
    registry: Any = None,
    provider: Any = None,
    policy: Policy | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ExecutionGate:
    return ExecutionGate(
        broker=chain.broker,
        kill_switch=kill_switch if kill_switch is not None else EventKillSwitch(log=chain.broker.log),
        registry=registry if registry is not None else InMemoryAuthorizationRegistry(),
        context_provider=provider if provider is not None else chain.provider,
        policy=policy if policy is not None else chain.policy,
        config=ExecutionConfig(enabled=enabled, dry_run=dry_run),
        clock=clock if clock is not None else (lambda: FIXED_NOW),
    )


def run(chain: Chain, gate: ExecutionGate) -> Any:
    return gate.execute(chain.auth, chain.proposal, chain.decision)


def codes(result: Any) -> set[str]:
    return {str(getattr(code, "value", code)) for code in result.reason_codes}


# ---------------------------------------------------------------------------------------------
# the order
# ---------------------------------------------------------------------------------------------
def test_the_happy_path_visits_every_step_in_the_documented_order():
    chain = a_chain()
    switch = EventKillSwitch(log=chain.log)
    result = run(chain, a_gate(chain, kill_switch=switch))

    assert result.status == "SUBMITTED", (result.status, result.message)
    assert chain.log == [
        "broker.find_order",
        "broker.get_portfolio_snapshot",
        "broker.get_market_snapshot",
        "kill_switch",
        "broker.submit_order",
    ], chain.log
    assert CHECK_ORDER.index("idempotency") < CHECK_ORDER.index("toctou_revalidation")
    assert CHECK_ORDER.index("toctou_revalidation") < CHECK_ORDER.index("authorization_consumed")
    assert CHECK_ORDER.index("authorization_consumed") < CHECK_ORDER.index("kill_switch")
    assert CHECK_ORDER[-2:] == ("kill_switch", "submit")


def test_the_kill_switch_is_read_after_the_last_broker_read_and_immediately_before_submit():
    """E4, as two facts about the log: last read < switch, and the switch is submit's predecessor."""
    chain = a_chain()
    switch = EventKillSwitch(log=chain.log)
    result = run(chain, a_gate(chain, kill_switch=switch))
    assert result.status == "SUBMITTED"

    reads = [index for index, event in enumerate(chain.log) if event in READS]
    switches = [index for index, event in enumerate(chain.log) if event == "kill_switch"]
    assert reads and switches
    assert switches[-1] > reads[-1], chain.log
    assert chain.log[-1] == "broker.submit_order"
    assert chain.log[-2] == "kill_switch", chain.log
    assert switch.calls == 1, "consulted once, at the boundary and nowhere else"


def test_a_switch_flipped_after_the_last_read_still_blocks():
    """The window E4 exists to close: every check passed, then the operator pulled the handle."""
    chain = a_chain()
    switch = EventKillSwitch(log=chain.log)

    class TrippingRegistry:
        """Consumption sits between the last risk re-check and the mutation: flip the switch there."""

        def __init__(self) -> None:
            self.inner = InMemoryAuthorizationRegistry()

        def consume(self, auth_id: str) -> bool:
            switch.active = True
            return self.inner.consume(auth_id)

    result = run(chain, a_gate(chain, kill_switch=switch, registry=TrippingRegistry()))

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result)
    assert chain.broker.submitted == []
    assert "broker.submit_order" not in chain.log
    assert result.kill_switch_checked_at is not None
    assert result.revalidation.performed is True, "the re-validation ran before the switch was read"


def test_a_switch_already_active_is_still_read_at_the_boundary_not_at_entry():
    chain = a_chain()
    switch = EventKillSwitch(active=True, log=chain.log)
    result = run(chain, a_gate(chain, kill_switch=switch))

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result)
    # not a request-entry check: the broker reads of the re-validation happened first
    assert result.revalidation.performed is True
    assert chain.log.index("kill_switch") > chain.log.index("broker.get_market_snapshot")
    assert chain.broker.submitted == []


def test_execution_disabled_stops_before_the_broker_is_touched_at_all():
    chain = a_chain()
    result = run(chain, a_gate(chain, enabled=False))

    assert result.status == "BLOCKED"
    assert codes(result) == {"EXECUTION_DISABLED"}
    assert chain.log == []
    assert result.revalidation.performed is False
    assert result.broker_order_id is None


def test_dry_run_passes_every_check_and_stops_one_step_short():
    chain = a_chain()
    result = run(chain, a_gate(chain, dry_run=True))

    assert result.status == "WOULD_SUBMIT"
    assert result.reason_codes == []
    assert chain.broker.submitted == []
    assert "broker.submit_order" not in chain.log
    assert chain.log[-1] == "kill_switch", chain.log
    assert result.kill_switch_checked_at is not None
    assert result.authorization_validated_at is not None
    assert result.submitted_at is None
    assert result.client_order_id == chain.auth.idempotency_key


def test_the_kill_switch_blocks_a_dry_run_too():
    chain = a_chain()
    switch = EventKillSwitch(active=True, log=chain.log)
    result = run(chain, a_gate(chain, dry_run=True, kill_switch=switch))
    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in codes(result)
    assert chain.broker.submitted == []


def test_an_existing_order_reconciles_and_never_reaches_the_revalidation_or_the_switch():
    """E7: the key is derived from the proposal, so a retry finds its own earlier order and stops."""
    chain = a_chain()
    first = run(chain, a_gate(chain))
    assert first.status == "SUBMITTED"

    chain.log.clear()
    switch = EventKillSwitch(log=chain.log)
    second = run(chain, a_gate(chain, kill_switch=switch))

    assert second.status == "RECONCILED_EXISTING"
    assert codes(second) == {"IDEMPOTENT_ORDER_EXISTS"}
    assert second.broker_order_id == first.broker_order_id
    assert second.client_order_id == chain.auth.idempotency_key
    assert len(chain.broker.submitted) == 1
    assert chain.log == ["broker.find_order"]
    assert switch.calls == 0
    assert second.revalidation.performed is False


def test_a_broker_that_cannot_be_reached_is_FAILED_and_never_an_assumed_success():
    chain = a_chain()
    chain.broker.fail_with = BrokerError("venue down at 10.0.0.9", reason_codes=["BROKER_UNAVAILABLE"])
    result = run(chain, a_gate(chain))

    assert result.status == "FAILED"
    assert "BROKER_UNAVAILABLE" in codes(result)
    assert result.broker_order_id is None
    assert result.fills == []
    # F-14: the vendor's own words never reach the caller-visible result
    assert "10.0.0.9" not in result.message and "venue down" not in result.message


def test_an_authorization_that_does_not_match_the_proposal_never_reaches_the_broker():
    """Step 2 checks the authorization against BOTH the decision and the proposal."""
    chain, other = a_chain(), a_chain(proposal=make_proposal(legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "7",
            "limit_price": "228.50",
            "order_type": "limit",
        }
    ]))
    gate = a_gate(chain)
    result = gate.execute(chain.auth, other.proposal, other.decision)

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(result)
    assert chain.log == []
    assert chain.broker.submitted == []


def test_the_authorization_window_is_exact_at_both_edges():
    """E6: valid on [issued_at, expires_at). At expires_at itself it is already expired."""
    ttl = a_chain().auth.ttl_seconds
    expectations = (
        (FIXED_NOW, "SUBMITTED"),
        (FIXED_NOW + timedelta(seconds=ttl - 1), "SUBMITTED"),
        (FIXED_NOW + timedelta(seconds=ttl), "BLOCKED"),
        (FIXED_NOW + timedelta(seconds=ttl + 1), "BLOCKED"),
        (FIXED_NOW - timedelta(seconds=1), "BLOCKED"),
    )
    for moment, expected in expectations:
        chain = a_chain()
        result = run(chain, a_gate(chain, clock=lambda moment=moment: moment))
        assert result.status == expected, (moment, result.status, result.message)
        if expected == "BLOCKED":
            assert codes(result) & {"AUTHORIZATION_EXPIRED", "AUTHORIZATION_NOT_YET_VALID"}
            assert chain.broker.submitted == []


def test_an_authorization_that_goes_stale_mid_flight_is_caught_by_the_second_validate():
    """E6: step 6 exists because steps 3-5 take time. Stale after the re-validation still blocks."""
    chain = a_chain()
    clock = SteppingClock()
    # readings: 1 checked_at, 2 entry validate, 3 context build, 4 pre-submit validate, 5 switch
    clock.advance_after(3, timedelta(seconds=chain.auth.ttl_seconds + 1))
    result = run(chain, a_gate(chain, clock=clock))

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in codes(result)
    assert result.revalidation.performed is True, "it went stale AFTER the re-validation, not before"
    assert chain.broker.submitted == []
    assert "broker.submit_order" not in chain.log
