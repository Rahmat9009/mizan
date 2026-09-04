"""L5 Sweep 6 — gate bypass. Every path that reaches a broker mutation, attacked.

Subject: the NEW core (``mizan.execution``, ``mizan.authorization``, ``mizan.adapters``,
``mizan.sdk``, ``mizan.api``). Run after every L2/L3 merge.

Most of this file is *proof of blocking*: an attack that is refused is as much a result as one
that succeeds, and a refusal nobody pinned is a refusal that can be removed by accident.

Findings raised here: F-28 (single-use authorization is process-local and the registry cannot be
injected into ``Mizan``, so two instances over one ledger submit one authorization twice) and
F-33 (``@protected`` with ``dry_run=False`` submits and *then* raises ``ConfigurationError``).

Hard Rules at stake: E3 (no bypass), E4 (kill switch immediately before the mutation), E5 (never
resize), E6 (authorization expires and is re-validated), E7 (derived idempotency key), E9 (TOCTOU).

Self-contained by design (ESC-3): this module builds its own chain, gate and doubles and takes
nothing from a shared conftest.
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mizan import authorization as authorization_module
from mizan import governor, risk
from mizan.adapters import BrokerContextProvider, MockBroker
from mizan.authorization import InMemoryAuthorizationRegistry, SqliteAuthorizationRegistry
from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts import ExecutionAuthorization
from mizan.contracts.errors import (
    AuthorizationError,
    ConfigurationError,
    ExecutionBlocked,
    LiveTradingForbidden,
    NotFound,
    ValidationFailed,
)
from mizan.execution import CHECK_ORDER, ExecutionConfig, ExecutionGate, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    AGENT_ID,
    FIXED_NOW,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTED = frozenset({"SUBMITTED", "RECONCILED_EXISTING"})


# ---------------------------------------------------------------------------------------------
# A chain of real objects: policy -> proposal -> context -> evaluation -> decision -> authorization
# ---------------------------------------------------------------------------------------------
def a_chain(policy: Any = None, proposal: Any = None) -> dict[str, Any]:
    policy = policy if policy is not None else make_policy()
    proposal = proposal if proposal is not None else make_proposal()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    provider = BrokerContextProvider(broker)
    context = provider.build(
        tenant_id=policy.tenant_id,
        agent_id=AGENT_ID,
        proposal=proposal,
        policy=policy,
        now=FIXED_NOW,
    )
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict != "REJECT", evaluation.reason_codes
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    auth = authorization_module.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
    broker.log.clear()
    return {
        "policy": policy,
        "proposal": proposal,
        "context": context,
        "decision": decision,
        "auth": auth,
        "broker": broker,
        "provider": provider,
    }


def a_gate(
    chain: dict[str, Any],
    *,
    enabled: bool = True,
    dry_run: bool = False,
    kill: bool = False,
    clock: Any = None,
    registry: Any = None,
    policy: Any = None,
) -> ExecutionGate:
    return ExecutionGate(
        broker=chain["broker"],
        kill_switch=InMemoryKillSwitch(active=kill),
        registry=registry if registry is not None else InMemoryAuthorizationRegistry(),
        context_provider=chain["provider"],
        policy=policy if policy is not None else chain["policy"],
        config=ExecutionConfig(enabled=enabled, dry_run=dry_run),
        clock=clock if clock is not None else (lambda: FIXED_NOW),
    )


def forge(auth: ExecutionAuthorization, **overrides: Any) -> ExecutionAuthorization:
    """Rebuild an authorization with attacker-chosen content, recomputing every derived hash.

    ``authorization_hash`` is a plain SHA-256 of the content, not a MAC, so anyone able to build one
    of these can make it self-consistent. That is the point of the test: the integrity that matters
    is not the authorization's own hash but the gate's comparison against the decision, the proposal
    and freshly derived state.
    """
    payload = auth.model_dump(mode="json")
    for derived in ("authorization_hash", "idempotency_key", "expires_at"):
        payload.pop(derived, None)
    payload.update(overrides)
    return ExecutionAuthorization.build(**payload)


def codes(result: Any) -> list[str]:
    return [str(getattr(code, "value", code)) for code in result.reason_codes]


# ---------------------------------------------------------------------------------------------
# 6.0 - the shape of the gate itself
# ---------------------------------------------------------------------------------------------
def test_check_order_is_the_documented_sequence_with_the_kill_switch_last() -> None:
    """E4: the switch is read after the final broker read and immediately before the mutation."""
    assert CHECK_ORDER == (
        "execution_enabled",
        "authorization_valid",
        "idempotency",
        "toctou_revalidation",
        "authorization_consumed",
        "authorization_fresh",
        "kill_switch",
        "submit",
    )
    assert CHECK_ORDER[-1] == "submit"
    assert CHECK_ORDER[-2] == "kill_switch"


def test_only_the_execution_gate_calls_submit_order_anywhere_in_mizan() -> None:
    """E3: enumerate every ``submit_order`` call site in the package by AST, not by grep.

    A broker mutation reachable from a second place is a second gate, and a second gate is no gate.
    The adapters define the method; only ``ExecutionGate._submit`` may call it.
    """
    callers: list[str] = []
    for path in sorted((REPO_ROOT / "mizan").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "submit_order":
                callers.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    # mizan/execution/__init__.py (the gate) and mizan/adapters/alpaca_paper.py (the SDK call
    # *inside* the one adapter method that is itself the mutation) are the only two.
    modules = {caller.rsplit(":", 1)[0] for caller in callers}
    assert modules == {
        "mizan/execution/__init__.py",
        "mizan/adapters/alpaca_paper.py",
    }, f"unexpected submit_order call site: {sorted(modules)}"
    gate_calls = [caller for caller in callers if caller.startswith("mizan/execution/")]
    assert len(gate_calls) == 1, f"the gate must have exactly one mutation site, found {gate_calls}"


def test_the_broker_protocol_has_no_cancel_replace_or_close_vocabulary() -> None:
    """B4: a capability that cannot be named cannot be reached by a bug or a helpful refactor."""
    from mizan.adapters import BrokerAdapter

    forbidden = {"cancel_order", "replace_order", "close_position", "close_all_positions", "liquidate"}
    assert forbidden.isdisjoint(dir(BrokerAdapter))
    assert forbidden.isdisjoint(dir(MockBroker))


def test_no_environment_variable_can_switch_a_check_off() -> None:
    """6.7: enumerate every env read in the enforcement path; none of them skips a check."""
    reads: list[tuple[str, str]] = []
    for path in sorted((REPO_ROOT / "mizan").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"getenv", "environ"}
            ):
                name = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else "?"
                reads.append((path.relative_to(REPO_ROOT).as_posix(), str(name)))
    variables = {name for _, name in reads}
    # Every one of these makes the system MORE restrictive or refuses outright. There is no
    # variable that grants an exemption, and no "MIZAN_SKIP_*", "DEBUG" or "TEST" anything.
    assert variables <= {"ALPACA_PAPER", "MIZAN_KILL_SWITCH", "?"}, sorted(variables)
    assert not any(
        token in name.upper() for _, name in reads for token in ("DEBUG", "SKIP", "BYPASS", "FORCE", "TEST")
    )


# ---------------------------------------------------------------------------------------------
# 6.1 - 6.8  the enumerated attacks
# ---------------------------------------------------------------------------------------------
def test_execution_disabled_blocks_before_anything_else() -> None:
    chain = a_chain()
    result = a_gate(chain, enabled=False).execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert codes(result) == ["EXECUTION_DISABLED"]
    assert chain["broker"].submitted == []
    assert chain["broker"].log == [], "a disabled deployment must not even read the broker"


def test_an_expired_authorization_cannot_execute() -> None:
    chain = a_chain()
    late = FIXED_NOW + timedelta(seconds=chain["auth"].ttl_seconds + 1)
    result = a_gate(chain, clock=lambda: late).execute(
        chain["auth"], chain["proposal"], chain["decision"]
    )
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in codes(result)
    assert chain["broker"].submitted == []


def test_an_authorization_from_the_future_cannot_execute() -> None:
    chain = a_chain()
    early = FIXED_NOW - timedelta(seconds=1)
    result = a_gate(chain, clock=lambda: early).execute(
        chain["auth"], chain["proposal"], chain["decision"]
    )
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_NOT_YET_VALID" in codes(result)
    assert chain["broker"].submitted == []


def test_a_consumed_authorization_cannot_be_replayed_through_the_same_registry() -> None:
    chain = a_chain()
    gate = a_gate(chain)
    first = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    assert first.status == "SUBMITTED"
    second = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    # Step 3 (the derived idempotency key) answers first, before the registry is even consulted:
    # the broker already holds this exact order, so nothing is submitted twice.
    assert second.status == "RECONCILED_EXISTING"
    assert "IDEMPOTENT_ORDER_EXISTS" in codes(second)
    assert len(chain["broker"].submitted) == 1


def test_a_consumed_authorization_is_refused_even_when_the_broker_forgets_the_order() -> None:
    """The registry, not the broker, is what makes single use true. Prove it in isolation."""
    chain = a_chain()
    gate = a_gate(chain)
    assert gate.execute(chain["auth"], chain["proposal"], chain["decision"]).status == "SUBMITTED"
    chain["broker"].orders.clear()  # a broker that lost its record of the order
    second = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    assert second.status == "BLOCKED"
    assert "AUTHORIZATION_ALREADY_USED" in codes(second)
    assert len(chain["broker"].submitted) == 1


def test_two_threads_racing_one_authorization_produce_exactly_one_submission() -> None:
    """F-9's new-core answer, within one process: ``registry.consume`` is the arbiter."""
    chain = a_chain()
    gate = a_gate(chain)
    both_read = threading.Barrier(2, timeout=10)
    original = chain["broker"].find_order

    def racing_find_order(client_order_id: str) -> Any:
        answer = original(client_order_id)
        both_read.wait()
        return answer

    chain["broker"].find_order = racing_find_order  # type: ignore[method-assign]
    statuses: list[str] = []
    lock = threading.Lock()

    def run() -> None:
        result = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
        with lock:
            statuses.append(result.status)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(statuses) == ["BLOCKED", "SUBMITTED"]
    assert len(chain["broker"].submitted) == 1


def test_the_kill_switch_is_read_after_the_last_broker_read(monkeypatch: Any) -> None:
    """E4: a switch thrown during the TOCTOU re-check still stops the order."""
    chain = a_chain()
    switch = InMemoryKillSwitch()
    gate = ExecutionGate(
        broker=chain["broker"],
        kill_switch=switch,
        registry=InMemoryAuthorizationRegistry(),
        context_provider=chain["provider"],
        policy=chain["policy"],
        config=ExecutionConfig(enabled=True, dry_run=False),
        clock=lambda: FIXED_NOW,
    )
    # The operator throws the switch while the gate is mid-read of the fresh portfolio.
    chain["broker"].on_portfolio_read = switch.activate
    result = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert codes(result) == ["KILL_SWITCH_ACTIVE"]
    assert chain["broker"].submitted == []
    assert chain["broker"].log[-1] != "broker.submit_order"


def test_the_only_window_left_to_the_kill_switch_is_one_function_call_wide() -> None:
    """Honest about the irreducible race, so nobody later mistakes it for a defect or a defence.

    E4 puts the switch immediately before the mutation, which shrinks the window to the interval
    between ``is_active()`` returning False and ``submit_order`` being entered - a few bytecodes with
    nothing interposable. A switch thrown INSIDE ``submit_order`` cannot stop that order, and no
    ordering of checks could: at that point the request is already at the venue. What matters is that
    the window is not any wider than that, which the flip-during-the-TOCTOU-read test above proves.
    """
    chain = a_chain()
    switch = InMemoryKillSwitch()
    gate = ExecutionGate(
        broker=chain["broker"],
        kill_switch=switch,
        registry=InMemoryAuthorizationRegistry(),
        context_provider=chain["provider"],
        policy=chain["policy"],
        config=ExecutionConfig(enabled=True, dry_run=False),
        clock=lambda: FIXED_NOW,
    )
    chain["broker"].on_before_submit = switch.activate
    result = gate.execute(chain["auth"], chain["proposal"], chain["decision"])
    assert result.status == "SUBMITTED"
    assert len(chain["broker"].submitted) == 1
    # The gate read the switch AFTER the last broker read and BEFORE the mutation, in that order.
    assert chain["broker"].log[-1] == "broker.submit_order"
    assert result.kill_switch_checked_at is not None


def test_a_stored_decision_whose_authorization_expired_cannot_be_executed_through_the_sdk() -> None:
    """The realistic path for attack 6.2: the record is real and chained, and time has passed."""
    policy = make_policy()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    now = FIXED_NOW

    def clock() -> Any:
        return now

    pipeline = Mizan(
        tenant_id=policy.tenant_id,
        agent=make_agent(),
        policy=policy,
        broker=broker,
        ledger=InMemoryLedger(),
        config=ExecutionConfig(enabled=True, dry_run=False),
        clock=clock,
    )
    record = pipeline.evaluate(make_proposal())
    assert record.authorization is not None
    now = FIXED_NOW + timedelta(seconds=record.authorization.ttl_seconds + 1)
    result = pipeline.execute(record.decision_id)
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in codes(result)
    assert broker.submitted == []


def test_the_env_kill_switch_is_re_read_on_every_call(monkeypatch: Any) -> None:
    """F-4 must not repeat: a value frozen at start-up is a switch that cannot be thrown."""
    from mizan.execution import EnvKillSwitch

    switch = EnvKillSwitch()
    monkeypatch.delenv("MIZAN_KILL_SWITCH", raising=False)
    assert switch.is_active() is False
    monkeypatch.setenv("MIZAN_KILL_SWITCH", "true")
    assert switch.is_active() is True
    monkeypatch.setenv("MIZAN_KILL_SWITCH", "banana")
    assert switch.is_active() is True, "an unparseable value must fail safe, not fail open"


def test_a_forged_upsized_scope_is_refused_against_the_decision() -> None:
    """The authorization hash is not a MAC; the comparison against the decision is the control."""
    chain = a_chain()
    scope = chain["auth"].scope.model_dump(mode="json")
    scope["legs"][0]["quantity"] = "1000"
    scope["total_quantity"] = "1000"
    forged = forge(chain["auth"], scope=scope)
    assert forged.authorization_hash != chain["auth"].authorization_hash
    result = a_gate(chain).execute(forged, chain["proposal"], chain["decision"])
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(result)
    assert chain["broker"].submitted == []


def test_an_authorization_cannot_be_used_for_a_different_proposal() -> None:
    chain = a_chain()
    other = make_proposal(symbol="MSFT")
    result = a_gate(chain).execute(chain["auth"], other, chain["decision"])
    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_SCOPE_MISMATCH" in codes(result)
    assert chain["broker"].submitted == []


def test_an_authorization_cannot_be_used_under_another_tenants_policy() -> None:
    chain = a_chain()
    other_policy = make_policy(tenant_id="tenant-b")
    result = a_gate(chain, policy=other_policy).execute(
        chain["auth"], chain["proposal"], chain["decision"]
    )
    assert result.status == "BLOCKED"
    assert "STATE_BINDING_MISMATCH" in codes(result)
    assert "REAUTHORIZATION_REQUIRED" in codes(result)
    assert chain["broker"].submitted == []


def test_the_contract_refuses_a_ttl_outside_the_five_to_thirty_second_window() -> None:
    """F-11 must not repeat: the legacy freshness window was configurable up to an hour."""
    chain = a_chain()
    for ttl in (0, 1, 4, 31, 120, 3600):
        with pytest.raises(ValidationError):
            forge(chain["auth"], ttl_seconds=ttl)


def test_a_rejected_decision_can_never_be_authorized() -> None:
    policy = make_policy()
    proposal = make_proposal(legs=[
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
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    provider = BrokerContextProvider(broker)
    context = provider.build(
        tenant_id=policy.tenant_id, agent_id=AGENT_ID, proposal=proposal, policy=policy, now=FIXED_NOW
    )
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, None, context=context)
    assert decision.verdict == "REJECT"
    with pytest.raises(AuthorizationError):
        authorization_module.issue(decision, proposal, policy, now=FIXED_NOW, context=context)


# ---------------------------------------------------------------------------------------------
# 6.9 - the SDK and the /v1 surface
# ---------------------------------------------------------------------------------------------
def a_pipeline(**overrides: Any) -> Mizan:
    policy = overrides.pop("policy", None) or make_policy()
    broker = overrides.pop("broker", None) or MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    return Mizan(
        tenant_id=policy.tenant_id,
        agent=make_agent(),
        policy=policy,
        broker=broker,
        ledger=overrides.pop("ledger", None) or InMemoryLedger(),
        config=overrides.pop("config", None) or ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
        **overrides,
    )


def test_the_sdk_refuses_a_proposal_that_claims_another_agent() -> None:
    pipeline = a_pipeline()
    impostor = make_proposal(agent=make_agent(agent_id="agent-someone-else"))
    with pytest.raises(ValidationFailed):
        pipeline.evaluate(impostor)


def test_the_sdk_cannot_execute_a_decision_it_never_recorded() -> None:
    pipeline = a_pipeline()
    with pytest.raises(NotFound):
        pipeline.execute("01a00000-0000-7000-8000-000000000000")


def test_protected_never_calls_the_wrapped_function_when_the_gate_refuses() -> None:
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    pipeline = a_pipeline(broker=broker, config=ExecutionConfig(enabled=False, dry_run=True))
    called: list[Any] = []

    @pipeline.protected
    def submit(proposal: Any) -> str:
        called.append(proposal)
        return "submitted"

    with pytest.raises(ExecutionBlocked):
        submit(make_proposal())
    assert called == []
    assert broker.submitted == []


# ---------------------------------------------------------------------------------------------
# FINDING F-28 - single use is process-local and the registry cannot be replaced
# ---------------------------------------------------------------------------------------------
def test_f28_the_authorization_registry_is_injectable_like_every_other_collaborator() -> None:
    """The mechanism that makes single use true was the one collaborator a deployment could not supply.

    ``AuthorizationRegistry`` was already a Protocol, so a durable registry was expressible - but
    ``Mizan`` took no argument for one and built ``InMemoryAuthorizationRegistry()`` itself. An
    interface nobody can pass an implementation to is documentation, not a seam.
    """
    parameters = set(inspect.signature(Mizan.__init__).parameters)
    assert {"broker", "ledger", "advisory", "kill_switch", "config", "clock", "registry"} <= parameters


def test_f28_single_use_belongs_to_the_ledger_not_to_the_pipeline() -> None:
    """Two pipelines over one book share one registry, because it is one book.

    This is the property the race test depends on, pinned directly rather than inferred from a
    threading outcome: a registry per Mizan instance made "consumed once" mean "consumed once per
    object", which is not a constraint on anything.
    """
    policy = make_policy()
    ledger = InMemoryLedger()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )

    def pipeline() -> Mizan:
        return Mizan(
            tenant_id=policy.tenant_id, agent=make_agent(), policy=policy, broker=broker,
            ledger=ledger, config=ExecutionConfig(enabled=True, dry_run=False),
            clock=lambda: FIXED_NOW,
        )

    assert pipeline().registry is pipeline().registry
    assert Mizan(
        tenant_id=policy.tenant_id, agent=make_agent(), policy=policy, broker=broker,
        ledger=InMemoryLedger(), config=ExecutionConfig(enabled=True, dry_run=False),
        clock=lambda: FIXED_NOW,
    ).registry is not pipeline().registry, "a different book is a different set of authorizations"


def test_f28_a_durable_ledger_gets_a_durable_registry_without_being_asked(tmp_path) -> None:
    """The deployment that gets this wrong is the ordinary one, so the default has to be right.

    A registry that only holds within a process is indistinguishable from a correct one until there
    are two processes - by which point it is a duplicate order, not a test failure. So a SQLite
    ledger produces a SQLite registry by default rather than on request, and single use is enforced
    by a PRIMARY KEY that no amount of simultaneity can race.
    """
    policy = make_policy()
    ledger = SqliteLedger(root_dir=tmp_path)
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    instance = Mizan(
        tenant_id=policy.tenant_id, agent=make_agent(), policy=policy, broker=broker,
        ledger=ledger, config=ExecutionConfig(enabled=True, dry_run=False), clock=lambda: FIXED_NOW,
    )

    assert isinstance(instance.registry, SqliteAuthorizationRegistry)
    assert instance.registry.consume("auth-1") is True
    assert instance.registry.consume("auth-1") is False

    # A SEPARATE registry object over the same file - which is what another worker holds - agrees.
    # Deliberately naming the path the SDK chose: the registry lives in a subdirectory, not beside
    # the per-tenant chains, because <root>/<tenant>.sqlite is a namespace this file is not part of.
    elsewhere = SqliteAuthorizationRegistry(tmp_path / "_registry" / "authorizations.sqlite")
    assert elsewhere.was_consumed("auth-1") is True
    assert elsewhere.consume("auth-1") is False


def test_f28_two_pipelines_over_one_ledger_must_not_submit_one_authorization_twice() -> None:
    policy = make_policy()
    ledger = InMemoryLedger()

    class RacingBroker(MockBroker):
        """``find_order`` answers, then holds both callers until both have their answer."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.both_read = threading.Barrier(2, timeout=10)

        def find_order(self, client_order_id: str) -> Any:
            answer = super().find_order(client_order_id)
            self.both_read.wait()
            return answer

    broker = RacingBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )

    def pipeline() -> Mizan:
        return Mizan(
            tenant_id=policy.tenant_id,
            agent=make_agent(),
            policy=policy,
            broker=broker,
            ledger=ledger,
            config=ExecutionConfig(enabled=True, dry_run=False),
            clock=lambda: FIXED_NOW,
        )

    first, second = pipeline(), pipeline()
    record = first.evaluate(make_proposal())
    assert record.authorization is not None

    def run(instance: Mizan) -> None:
        try:
            instance.execute(record.decision_id)
        except Exception:  # noqa: BLE001 - the assertion below is about the broker, not the caller
            pass

    threads = [threading.Thread(target=run, args=(instance,)) for instance in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(broker.submitted) == 1, (
        f"one authorization reached the broker {len(broker.submitted)} times: single use is "
        "enforced only within one process"
    )


def test_f28_the_broker_idempotency_read_is_the_only_remaining_defence() -> None:
    """The non-racing half of F-28: sequentially, the second attempt is refused by US.

    It used to be refused by Alpaca. A second ``Mizan`` re-consumed the authorization and submitted,
    and only the broker's memory of the client order id prevented a duplicate - precisely the posture
    F-9 faulted the legacy build for ("duplicate suppression is delegated entirely to Alpaca").

    Now the registry follows the ledger, so the second pipeline sees the authorization already
    consumed and reconciles instead of submitting. The broker's idempotency is still there and is
    still worth having; it is no longer the only thing standing between one authorization and two
    orders.
    """
    policy = make_policy()
    ledger = InMemoryLedger()
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )

    def pipeline() -> Mizan:
        return Mizan(
            tenant_id=policy.tenant_id,
            agent=make_agent(),
            policy=policy,
            broker=broker,
            ledger=ledger,
            config=ExecutionConfig(enabled=True, dry_run=False),
            clock=lambda: FIXED_NOW,
        )

    first, second = pipeline(), pipeline()
    record = first.evaluate(make_proposal())
    assert first.execute(record.decision_id).status == "SUBMITTED"
    assert second.registry.was_consumed(record.authorization.auth_id) is True, (
        "the second pipeline must see the authorization as already spent"
    )
    replay = second.execute(record.decision_id)
    assert replay.status == "RECONCILED_EXISTING"
    assert len(broker.submitted) == 1


# ---------------------------------------------------------------------------------------------
# FINDING F-33 - @protected refuses a double-submit configuration only after submitting
# ---------------------------------------------------------------------------------------------
def test_f33_protected_must_refuse_a_double_submit_config_before_it_submits() -> None:
    """The refusal happens at DECORATION, so no proposal and no broker are ever involved.

    It used to happen after the gate had placed a real order, and the error said the combination
    "would double-submit" while the first of the two was already live. A caller reading
    ConfigurationError as "nothing happened" - the only sane reading - was wrong.
    """
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    pipeline = a_pipeline(broker=broker, config=ExecutionConfig(enabled=True, dry_run=False))
    called: list[Any] = []

    with pytest.raises(ConfigurationError, match="dry-run"):

        @pipeline.protected
        def submit(proposal: Any) -> str:  # pragma: no cover - decoration raises first
            called.append(proposal)
            return "caller submitted"

    assert called == [], "the caller's function must not run"
    assert broker.submitted == [], (
        "nothing may reach the broker: this is decidable from configuration alone, with no "
        "proposal in hand"
    )


def test_f33_a_config_swapped_after_decoration_is_still_refused() -> None:
    """Decorating early is not a licence to stop checking: `config` is a mutable attribute.

    A guard that only runs when nothing has changed is the one that misses.
    """
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    pipeline = a_pipeline(broker=broker, config=ExecutionConfig(enabled=True, dry_run=True))
    called: list[Any] = []

    @pipeline.protected
    def submit(proposal: Any) -> str:
        called.append(proposal)
        return "caller submitted"

    pipeline.config = ExecutionConfig(enabled=True, dry_run=False)
    with pytest.raises(ConfigurationError, match="dry-run"):
        submit(make_proposal())

    assert called == []
    assert broker.submitted == []


def test_f33_the_dry_run_config_protected_is_built_for_still_works() -> None:
    """The control: refusing the wrong config must not have broken the right one.

    Under dry_run the gate runs every check and stops one step short of the mutation, and the
    CALLER's function places the order. That is the whole arrangement @protected exists for.
    """
    broker = MockBroker(
        portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
    )
    pipeline = a_pipeline(broker=broker, config=ExecutionConfig(enabled=True, dry_run=True))
    called: list[Any] = []

    @pipeline.protected
    def submit(proposal: Any) -> str:
        called.append(proposal)
        return "caller submitted"

    assert submit(make_proposal()) == "caller submitted"
    assert len(called) == 1
    assert broker.submitted == [], "under dry_run the gate must not submit; the caller does"


def test_no_live_trading_configuration_is_representable(monkeypatch: Any) -> None:
    """B1: paper is a deployment boundary, not a flag. Nothing can express a live config."""
    with pytest.raises(ValidationError):
        ExecutionConfig(paper=False)  # type: ignore[arg-type]
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()
    monkeypatch.setenv("ALPACA_PAPER", "true")
    assert ExecutionConfig.from_environment().paper is True
    assert os.getenv("ALPACA_PAPER") == "true"
