"""Time-of-check/time-of-use, single use under concurrency, and the refusal to resize.

Two legacy findings are answered here directly:

* **F-9** - two concurrent executes both passed every gate and both called submit, with duplicate
  suppression delegated to the broker rejecting a reused client order id. Here threads race one
  authorization and exactly one submission happens, because ``registry.consume`` is atomic and sits
  *inside* the gate rather than at the venue.
* **F-1** - state read once at decision time and never re-read. Here the world changes between the
  authorization and the execution, and the gate notices.

Hard Rule E5 gets its own assertions: when fresh risk supports a smaller size, the gate does not place
the smaller order. A quiet cut is the most dangerous possible outcome, because it looks like success.

Self-contained by design (see ``test_gate_order.py``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
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
    PortfolioSnapshot,
    RiskContext,
    TradeProposal,
    dec,
    dstr,
)
from mizan.execution import ExecutionConfig, ExecutionGate, InMemoryKillSwitch
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

EXECUTED = frozenset({"SUBMITTED", "WOULD_SUBMIT", "RECONCILED_EXISTING"})


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

    def starved_portfolio(self, units_short: int) -> PortfolioSnapshot:
        """Buying power that affords strictly fewer units than the authorization scoped."""
        assert self.context.market_snapshot is not None
        price = dec(self.context.market_snapshot.quotes[self.proposal.symbol].price)
        affordable = max(self.scope_quantity - Decimal(units_short), Decimal(0))
        return make_portfolio_snapshot(buying_power=dstr(price * affordable))

    @staticmethod
    def shrunken_portfolio(equity: str) -> PortfolioSnapshot:
        """The account is simply smaller than it was: a drawdown between decision and execution.

        Every limit expressed as a fraction of equity now binds tighter, so fresh risk supports a
        *smaller but non-zero* size - which is the case E5 is really about. A gate that resized would
        place that smaller order and look successful while doing something nobody authorized.
        """
        return make_portfolio_snapshot(
            equity=equity,
            peak_equity=equity,
            cash=equity,
            buying_power=equity,
            daily_pnl="0",
            positions=[],
            greeks=None,
            gross_exposure="0",
            net_exposure="0",
            margin_requirement="0",
            maintenance_excess=equity,
            factor_exposures=None,
        )


def a_chain() -> Chain:
    policy = make_policy()
    proposal = make_proposal()
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
    broker.log.clear()
    return Chain(policy, proposal, context, decision, auth, broker, provider)


def a_gate(
    chain: Chain,
    *,
    dry_run: bool = False,
    registry: Any = None,
    provider: Any = None,
    policy: Policy | None = None,
) -> ExecutionGate:
    return ExecutionGate(
        broker=chain.broker,
        kill_switch=InMemoryKillSwitch(),
        registry=registry if registry is not None else InMemoryAuthorizationRegistry(),
        context_provider=provider if provider is not None else chain.provider,
        policy=policy if policy is not None else chain.policy,
        config=ExecutionConfig(enabled=True, dry_run=dry_run),
        clock=lambda: FIXED_NOW,
    )


def run(chain: Chain, gate: ExecutionGate) -> Any:
    return gate.execute(chain.auth, chain.proposal, chain.decision)


def codes(result: Any) -> set[str]:
    return {str(getattr(code, "value", code)) for code in result.reason_codes}


# ---------------------------------------------------------------------------------------------
# E9 / E5
# ---------------------------------------------------------------------------------------------
def test_state_that_degrades_between_authorization_and_execution_blocks():
    chain = a_chain()
    # the world moves after the authorization was minted and before the gate reads it
    chain.broker.set_portfolio_snapshot(chain.starved_portfolio(1))
    result = run(chain, a_gate(chain))

    assert result.status == "BLOCKED", (result.status, result.message)
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert "TOCTOU_STATE_CHANGED" in codes(result)
    assert result.revalidation.performed is True
    assert result.revalidation.supported is False
    assert result.revalidation.state_changed is True
    assert dec(result.revalidation.fresh_recommended_quantity) < chain.scope_quantity
    assert chain.broker.submitted == []
    assert "broker.submit_order" not in chain.log


def test_state_that_degrades_DURING_the_read_is_caught_too():
    """The hook fires inside the broker read, so the gate sees the degraded world, not a cached one."""
    chain = a_chain()
    starved = chain.starved_portfolio(1)
    chain.broker.on_portfolio_read = lambda: chain.broker.set_portfolio_snapshot(starved)

    result = run(chain, a_gate(chain))
    assert result.status == "BLOCKED"
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert chain.broker.submitted == []


def test_the_gate_never_places_the_smaller_order_it_could_have_placed():
    """E5: a supported smaller size is a refusal. Nothing is submitted, and the scope is untouched."""
    chain = a_chain()
    original_scope = chain.auth.scope.total_quantity
    chain.broker.set_portfolio_snapshot(chain.shrunken_portfolio("12000"))

    result = run(chain, a_gate(chain))

    assert result.status not in EXECUTED
    assert chain.broker.submitted == []
    assert chain.auth.scope.total_quantity == original_scope
    supported = dec(result.revalidation.fresh_recommended_quantity)
    assert Decimal(0) < supported < dec(original_scope), "this must be a *partial* shortfall, not a zero"


def test_a_fresh_rejection_requires_reauthorization_rather_than_a_zero_sized_order():
    chain = a_chain()
    chain.broker.set_portfolio_snapshot(make_portfolio_snapshot(buying_power="0"))
    result = run(chain, a_gate(chain))

    assert result.status == "BLOCKED"
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert result.revalidation.fresh_recommended_quantity == "0"
    assert chain.broker.submitted == []


def test_an_escalated_response_level_requires_reauthorization():
    """Addendum 1 B.5: the ladder moved under the authorization, though risk alone still supports it."""
    chain = a_chain()
    assert chain.auth.bound_state.response_level == 0
    escalated = BrokerContextProvider(chain.broker, response_level=3)

    result = run(chain, a_gate(chain, provider=escalated))

    assert result.status == "BLOCKED", (result.status, result.message)
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert "RESPONSE_LEVEL_ESCALATED" in codes(result)
    assert result.revalidation.response_level_at_execution == 3
    assert chain.broker.submitted == []


def test_a_policy_swapped_under_the_authorization_is_a_state_binding_mismatch():
    """The authorization is bound to a policy hash; a gate running a different policy may not use it."""
    chain = a_chain()
    other = make_policy(order={**chain.policy.order.model_dump(mode="json"), "max_quantity": "99"})
    assert other.policy_hash != chain.policy.policy_hash

    result = run(chain, a_gate(chain, policy=other))

    assert result.status == "BLOCKED"
    assert "STATE_BINDING_MISMATCH" in codes(result)
    assert chain.broker.submitted == []


def test_unchanged_state_still_re_evaluates_on_every_execution():
    """E9 is unconditional: the re-check runs even when nothing moved, so it cannot rot unused."""
    chain = a_chain()
    result = run(chain, a_gate(chain, dry_run=True))

    assert result.status == "WOULD_SUBMIT"
    assert result.revalidation.performed is True
    assert result.revalidation.supported is True
    assert result.revalidation.state_changed is False
    assert result.revalidation.fresh_context_id is not None
    assert result.revalidation.fresh_evaluation_id is not None
    assert "broker.get_portfolio_snapshot" in chain.log


# ---------------------------------------------------------------------------------------------
# F-9: single use under concurrency
# ---------------------------------------------------------------------------------------------
class BarrierRegistry:
    """A single-use registry that holds every racer at ``consume`` until they have all arrived.

    Without it the race is not reliably a race: one thread can finish submitting before another even
    reads the broker, and the second is then stopped by idempotency rather than by single use. Both
    outcomes are safe, but only one of them tests the thing F-9 was about, so the barrier forces every
    caller past the idempotency read and the TOCTOU re-check before any of them may consume.
    """

    def __init__(self, workers: int) -> None:
        self.inner = InMemoryAuthorizationRegistry()
        self.barrier = threading.Barrier(workers)
        self.calls = 0
        self._lock = threading.Lock()

    def consume(self, auth_id: str) -> bool:
        self.barrier.wait(timeout=10)
        with self._lock:
            self.calls += 1
        return self.inner.consume(auth_id)


def _race(chain: Chain, gate: ExecutionGate, workers: int) -> list[Any]:
    """Run ``workers`` executions of one authorization, all of them inside the gate at the same moment.

    Two barriers, because starting the threads together is not enough: a thread that reaches
    ``submit_order`` before the others have run their idempotency read would be found by them at step 3
    and reported as RECONCILED_EXISTING, which is correct behaviour but a different test. The second
    barrier sits inside the TOCTOU portfolio read - after every worker has passed ``find_order`` and
    before any of them can reach ``registry.consume`` - so what they actually race is the consume, which
    is the thing single use has to make atomic (F-9).
    """
    started = threading.Barrier(workers)
    at_the_consume = threading.Barrier(workers)
    chain.broker.on_portfolio_read = lambda: at_the_consume.wait(timeout=10)
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            started.wait(timeout=10)
            outcome = gate.execute(chain.auth, chain.proposal, chain.decision)
            with lock:
                results.append(outcome)
        except BaseException as failure:  # noqa: BLE001 - a dead thread must fail the test loudly
            with lock:
                errors.append(failure)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, errors
    assert len(results) == workers
    return results


def test_two_threads_racing_one_authorization_produce_exactly_one_submission():
    """The legacy race, run for real: both callers pass every check, one consume wins, one order exists."""
    chain = a_chain()
    registry = BarrierRegistry(2)
    results = _race(chain, a_gate(chain, registry=registry), 2)

    assert registry.calls == 2, "both callers really did reach the consume step"
    assert sorted(result.status for result in results) == ["BLOCKED", "SUBMITTED"]
    assert len(chain.broker.submitted) == 1, chain.broker.submitted
    assert chain.log.count("broker.submit_order") == 1

    blocked = next(result for result in results if result.status == "BLOCKED")
    assert "AUTHORIZATION_ALREADY_USED" in codes(blocked)
    assert blocked.broker_order_id is None
    assert blocked.submitted_at is None


def test_eight_threads_racing_one_authorization_still_produce_exactly_one_submission():
    chain = a_chain()
    registry = BarrierRegistry(8)
    results = _race(chain, a_gate(chain, registry=registry), 8)

    assert registry.calls == 8
    submitted = [result for result in results if result.status == "SUBMITTED"]
    assert len(submitted) == 1, [result.status for result in results]
    assert len(chain.broker.submitted) == 1
    assert chain.log.count("broker.submit_order") == 1
    for result in results:
        if result.status != "SUBMITTED":
            assert result.status == "BLOCKED"
            assert "AUTHORIZATION_ALREADY_USED" in codes(result)
            assert result.broker_order_id is None


def test_racers_that_arrive_after_the_winner_has_submitted_reconcile_instead_of_duplicating():
    """The other safe outcome: a late caller finds the winner's order and reports it (E7)."""
    chain = a_chain()
    gate = a_gate(chain)
    first = run(chain, gate)
    assert first.status == "SUBMITTED"

    second = run(chain, gate)
    assert second.status == "RECONCILED_EXISTING"
    assert second.broker_order_id == first.broker_order_id
    assert len(chain.broker.submitted) == 1


def test_a_second_sequential_execute_is_refused_by_the_registry_not_by_the_broker():
    """Single use is enforced locally: the second attempt never reaches submit, even at a fresh broker."""
    chain = a_chain()
    registry = InMemoryAuthorizationRegistry()
    first = run(chain, a_gate(chain, registry=registry))
    assert first.status == "SUBMITTED"

    chain.broker.orders.clear()  # a broker that has never seen this order: only the registry can refuse
    chain.log.clear()
    result = run(chain, a_gate(chain, registry=registry))

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_ALREADY_USED" in codes(result)
    assert len(chain.broker.submitted) == 1
    assert "broker.submit_order" not in chain.log
