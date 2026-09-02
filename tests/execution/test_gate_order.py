"""The gate's check order, proven by a call log rather than by reading the source.

Hard Rule E4 is an *ordering* claim: the kill switch is consulted immediately before the mutation, not
at request entry. An outcome test cannot distinguish the two - a gate that reads the switch first and
happens to return BLOCKED looks identical from the outside. So every call the gate makes to the broker,
the context provider and the switch is appended to one shared log, and the assertions are about
positions in that log.
"""

from __future__ import annotations

from datetime import timedelta

from mizan.contracts.errors import BrokerError
from mizan.execution import CHECK_ORDER

from tests.fixtures import FIXED_NOW

READS = frozenset(
    {"broker.get_portfolio_snapshot", "broker.get_market_snapshot", "broker.find_order", "context.build"}
)


def test_the_happy_path_visits_every_step_in_the_documented_order(make_chain, make_wiring, run_gate):
    wiring = make_wiring(make_chain())
    result = run_gate(wiring)

    assert result.status == "SUBMITTED", (result.status, result.message)
    assert wiring.log == [
        "broker.find_order",
        "broker.get_portfolio_snapshot",
        "broker.get_market_snapshot",
        "context.build",
        "kill_switch",
        "broker.submit_order",
    ], wiring.log
    # the log is the documented order, in the documented direction
    assert CHECK_ORDER.index("idempotency") < CHECK_ORDER.index("toctou_revalidation")
    assert CHECK_ORDER.index("toctou_revalidation") < CHECK_ORDER.index("authorization_consumed")
    assert CHECK_ORDER.index("authorization_consumed") < CHECK_ORDER.index("kill_switch")
    assert CHECK_ORDER[-2:] == ("kill_switch", "submit")


def test_the_kill_switch_is_read_after_the_last_broker_read_and_immediately_before_submit(
    make_chain, make_wiring, run_gate
):
    """E4, stated as two facts about the log: last read < switch, and switch is submit's predecessor."""
    wiring = make_wiring(make_chain())
    result = run_gate(wiring)
    assert result.status == "SUBMITTED"

    reads = [index for index, event in enumerate(wiring.log) if event in READS]
    switches = [index for index, event in enumerate(wiring.log) if event == "kill_switch"]
    assert reads and switches
    assert switches[-1] > reads[-1], wiring.log
    assert wiring.log[-1] == "broker.submit_order"
    assert wiring.log[-2] == "kill_switch", wiring.log


def test_the_switch_is_consulted_exactly_once_and_only_at_the_boundary(make_chain, make_wiring, run_gate):
    wiring = make_wiring(make_chain())
    run_gate(wiring)
    assert wiring.log.count("kill_switch") == 1


def test_a_switch_flipped_after_the_last_read_still_blocks(make_chain, make_wiring, run_gate, reason_codes):
    """The window E4 exists to close: everything passed, then the operator pulled the handle."""
    wiring = make_wiring(make_chain(), on_build=lambda _context: None)
    wiring.provider.on_build = lambda _context: wiring.kill_switch.activate()
    result = run_gate(wiring)

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in reason_codes(result)
    assert wiring.broker.submitted == []
    assert "broker.submit_order" not in wiring.log
    assert result.kill_switch_checked_at is not None
    assert result.revalidation.performed is True


def test_a_switch_already_active_is_still_read_at_the_boundary_not_at_entry(
    make_chain, make_wiring, run_gate, reason_codes
):
    wiring = make_wiring(make_chain())
    wiring.kill_switch.activate()
    result = run_gate(wiring)

    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in reason_codes(result)
    # not a request-entry check: the re-validation ran first, and the switch was read after it
    assert result.revalidation.performed is True
    assert wiring.log.index("kill_switch") > wiring.log.index("context.build")


def test_execution_disabled_stops_before_the_broker_is_touched_at_all(
    make_chain, make_wiring, run_gate, reason_codes
):
    wiring = make_wiring(make_chain(), enabled=False)
    result = run_gate(wiring)

    assert result.status == "BLOCKED"
    assert reason_codes(result) == {"EXECUTION_DISABLED"}
    assert wiring.log == []
    assert result.revalidation.performed is False
    assert result.broker_order_id is None


def test_dry_run_passes_every_check_and_stops_one_step_short(make_chain, make_wiring, run_gate):
    wiring = make_wiring(make_chain(), dry_run=True)
    result = run_gate(wiring)

    assert result.status == "WOULD_SUBMIT"
    assert result.reason_codes == []
    assert wiring.broker.submitted == []
    assert "broker.submit_order" not in wiring.log
    assert wiring.log[-1] == "kill_switch"
    assert result.kill_switch_checked_at is not None
    assert result.authorization_validated_at is not None
    assert result.submitted_at is None
    assert result.client_order_id == wiring.chain.auth.idempotency_key


def test_the_kill_switch_blocks_a_dry_run_too(make_chain, make_wiring, run_gate, reason_codes):
    wiring = make_wiring(make_chain(), dry_run=True)
    wiring.kill_switch.activate()
    result = run_gate(wiring)
    assert result.status == "BLOCKED"
    assert "KILL_SWITCH_ACTIVE" in reason_codes(result)


def test_an_existing_order_reconciles_and_never_reaches_the_revalidation_or_the_switch(
    make_chain, make_wiring, run_gate, reason_codes
):
    """E7: the idempotency key is derived from the proposal, so a retry finds its own earlier order."""
    chain = make_chain()
    wiring = make_wiring(chain)
    first = run_gate(wiring)
    assert first.status == "SUBMITTED"

    replay_wiring = make_wiring(chain, broker=wiring.broker)
    second = replay_wiring.gate.execute(chain.auth, chain.proposal, chain.decision)

    assert second.status == "RECONCILED_EXISTING"
    assert reason_codes(second) == {"IDEMPOTENT_ORDER_EXISTS"}
    assert second.broker_order_id == first.broker_order_id
    assert second.client_order_id == chain.auth.idempotency_key
    assert len(wiring.broker.submitted) == 1
    assert replay_wiring.log == ["broker.find_order"]
    assert second.revalidation.performed is False


def test_a_broker_that_cannot_be_reached_is_FAILED_and_never_an_assumed_success(
    make_chain, make_wiring, run_gate, reason_codes
):
    chain = make_chain()
    wiring = make_wiring(chain)
    wiring.broker.fail_with = BrokerError("venue down", reason_codes=["BROKER_UNAVAILABLE"])
    result = run_gate(wiring)

    assert result.status == "FAILED"
    assert "BROKER_UNAVAILABLE" in reason_codes(result)
    assert result.broker_order_id is None
    assert result.fills == []
    assert "venue down" not in result.message


def test_an_authorization_that_does_not_match_the_proposal_never_reaches_the_broker(
    make_chain, make_wiring, reason_codes
):
    """Step 2 checks the authorization against BOTH the decision and the proposal."""
    first, second = make_chain(), make_chain()
    wiring = make_wiring(first)
    result = wiring.gate.execute(first.auth, second.proposal, second.decision)

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_SCOPE_MISMATCH" in reason_codes(result)
    assert wiring.log == []
    assert wiring.broker.submitted == []


def test_the_authorization_window_is_exact_at_both_edges(make_chain, make_wiring, reason_codes):
    """E6: valid on [issued_at, expires_at). At expires_at itself it is already expired."""
    chain = make_chain()
    ttl = chain.auth.ttl_seconds
    moments = {
        FIXED_NOW: "SUBMITTED",
        FIXED_NOW + timedelta(seconds=ttl - 1): "SUBMITTED",
        FIXED_NOW + timedelta(seconds=ttl): "BLOCKED",
        FIXED_NOW + timedelta(seconds=ttl + 1): "BLOCKED",
        FIXED_NOW - timedelta(seconds=1): "BLOCKED",
    }
    for moment, expected in moments.items():
        fresh = make_chain()
        wiring = make_wiring(fresh, clock=lambda moment=moment: moment)
        result = wiring.gate.execute(fresh.auth, fresh.proposal, fresh.decision)
        assert result.status == expected, (moment, result.status, result.message)
        if expected == "BLOCKED":
            assert reason_codes(result) & {"AUTHORIZATION_EXPIRED", "AUTHORIZATION_NOT_YET_VALID"}
            assert wiring.broker.submitted == []


def test_an_authorization_that_goes_stale_mid_flight_is_caught_by_the_second_validate(
    make_chain, make_wiring, reason_codes
):
    """E6: step 6 exists because steps 3-5 take time. Stale after the re-validation still blocks."""
    chain = make_chain()
    ttl = chain.auth.ttl_seconds
    phase = {"stale": False}

    def clock():
        return FIXED_NOW + timedelta(seconds=ttl + 1) if phase["stale"] else FIXED_NOW

    wiring = make_wiring(chain, clock=clock, on_build=lambda _context: phase.__setitem__("stale", True))
    result = wiring.gate.execute(chain.auth, chain.proposal, chain.decision)

    assert result.status == "BLOCKED"
    assert "AUTHORIZATION_EXPIRED" in reason_codes(result)
    assert result.revalidation.performed is True
    assert wiring.broker.submitted == []
    assert "broker.submit_order" not in wiring.log
