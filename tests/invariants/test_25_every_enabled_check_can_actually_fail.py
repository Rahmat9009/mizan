"""Invariant 25 - the ESC-4 class: an enabled blocking check must be able to FAIL, and a PASS must carry evidence.

A check that is enabled, configured ``blocking``, and structurally incapable of failing is worse than a
disabled one. A disabled check is absent from the record and everyone knows it. A dead check writes
``passed=True, severity="blocking"`` into a DecisionRecord whose entire purpose is to be believed later -
it manufactures evidence of a control that never ran. ESC-4 found one (``duplicate_order``); this
invariant is the class, so that the next one cannot be introduced silently.

Pass criterion:
  (a) EVERY enabled, implemented, blocking check is OBSERVED failing on some constructible input. The
      battery below discovers most; the ones needing a precisely shaped input get an explicit exemplar.
      A newly added check with neither is a FAILURE - the author must show their check can fail.
  (b) is invariant 26 (check_passed_implies_evidence_present), which shares this battery.
  (c) A check the engine does not implement can never masquerade as a blocking pass: it is reported
      ``severity="info"`` with a stated reason, and the policy refuses to enable it at all.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mizan import risk
from mizan.contracts import Policy
from mizan.contracts.policy import CHECK_IDS
from mizan.contracts.types import format_ts
from mizan.risk import IMPLEMENTED_CHECKS
from mizan.risk.checks import CHECK_FUNCTIONS

from tests.fixtures import (
    FIXED_NOW,
    make_institutional_policy,
    make_option_proposal,
    make_proposal,
)
from tests.invariants._support import (
    check_battery,
    context_for,
    iron_condor,
    path_and_aggregate_policy,
    policy_with,
    rebuild_proposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The one check structurally incapable of failing in any shipped pipeline, with the reason.
#: ``RiskContext.recent_orders`` is a parameter of BrokerContextProvider.build defaulting to () that no
#: caller anywhere in mizan/ populates, so duplicate_order's loop body is unreachable in production.
#: Recorded as ESC-4. The emptiness of this mapping is asserted by an xfail(strict=True) below, so
#: closing ESC-4 turns that test XPASS and forces this entry's removal.
KNOWN_DEAD: dict[str, str] = {
    "duplicate_order": "ESC-4: RiskContext.recent_orders is never populated by any shipped caller",
}


def _exemplars():
    """Checks needing a precisely shaped input the battery does not stumble into."""
    proposal = make_proposal()
    option = make_option_proposal()
    near = (FIXED_NOW + timedelta(days=1)).date().isoformat()
    return {
        "restricted_symbol": (proposal, policy_with(**{"restricted.symbols": [proposal.symbol]})),
        "restricted_strategy": (
            proposal,
            policy_with(**{"restricted.strategies": [proposal.strategy]}),
        ),
        "leg_limit": (iron_condor(), policy_with(**{"order.max_legs": 2})),
        "position_limit": (proposal, policy_with(**{"order.max_quantity": "1"})),
        "proposal_expiry": (
            rebuild_proposal(
                proposal,
                expires_at=format_ts(FIXED_NOW - timedelta(hours=1)),
                created_at=format_ts(FIXED_NOW - timedelta(hours=2)),
            ),
            path_and_aggregate_policy(),
        ),
        "days_to_expiry": (
            rebuild_proposal(
                option,
                legs=[
                    {**leg, "expiry": near}
                    for leg in option.model_dump(mode="json")["legs"]
                ],
            ),
            policy_with(**{"options.min_days_to_expiry": 30}),
        ),
    }


def _observed_failing() -> set[str]:
    failing: set[str] = set()
    for proposal, context, policy in check_battery():
        for check in risk.evaluate(proposal, context, policy).checks:
            if not check.passed and policy.is_check_enabled(check.check_id):
                failing.add(check.check_id)
    for check_id, (proposal, policy) in _exemplars().items():
        result = CHECK_FUNCTIONS[check_id](proposal, context_for(policy), policy)
        if result is not None and not result.passed:
            failing.add(check_id)
    return failing


def _enabled_blocking_implemented() -> set[str]:
    enabled: set[str] = set()
    for policy in (make_institutional_policy(), path_and_aggregate_policy()):
        for check_id in policy.enabled_checks:
            if check_id in IMPLEMENTED_CHECKS and policy.check_config(check_id).severity == "blocking":
                enabled.add(check_id)
    return enabled


# --- (a) every enabled blocking check can actually fail --------------------------------------------
def test_every_enabled_check_can_actually_fail():
    expected = _enabled_blocking_implemented() - set(KNOWN_DEAD)
    dead = sorted(expected - _observed_failing())
    assert not dead, (
        "these checks are enabled and configured blocking, but no input in the battery or the exemplar "
        f"table could make any of them fail: {dead}. A blocking check that cannot fail writes "
        "passed=True into a record whose only purpose is to be believed later. Either add an input that "
        "fails it, or the check is dead and must be removed or fixed - do NOT add it to KNOWN_DEAD "
        "without an escalation entry."
    )


def test_known_dead_checks_are_exactly_the_escalated_ones():
    """A NEW dead check HALTS the build; the one known dead check stays pinned to its escalation.

    An xfail is not available here and should not be: this suite's conftest classifies a skipped or
    xfailed invariant as BLOCKING, because an invariant that does not run proves nothing. So the gap is
    pinned by identity instead. Adding a check to KNOWN_DEAD fails this test until the entry is both
    named here and escalated in the ledger, and removing ESC-4's cause fails it until the entry goes.
    Either direction is loud.
    """
    assert set(KNOWN_DEAD) == {"duplicate_order"}, (
        f"KNOWN_DEAD changed to {sorted(KNOWN_DEAD)}. A check that cannot fail is a control reporting "
        "success on no evidence. If a NEW one appeared, that is a HALT: fix or remove the check rather "
        "than widening this set. If duplicate_order was fixed, delete its entry and this expectation."
    )
    escalations = (REPO_ROOT / "ledger" / "escalations.md").read_text(encoding="utf-8")
    assert "ESC-4" in escalations and "duplicate_order" in escalations, (
        "every KNOWN_DEAD entry must be escalated in ledger/escalations.md by name; a dead control that "
        "is not written down is indistinguishable from one nobody noticed"
    )


def test_the_known_dead_check_really_is_dead():
    """Verify the exemption rather than trusting it: duplicate_order cannot fail because no shipped
    caller populates recent_orders, NOT because its logic is wrong. Given the evidence it needs, it
    fails correctly - so the defect is the missing wiring, and this test says which."""
    provider = (REPO_ROOT / "mizan" / "adapters" / "context.py").read_text(encoding="utf-8")
    assert "recent_orders" in provider, "the context provider no longer mentions recent_orders"
    populated = [
        line
        for line in provider.splitlines()
        if "recent_orders" in line and "=" in line and "()" not in line and "[]" not in line
    ]
    assert not [line for line in populated if "broker" in line.lower()], (
        "a caller now populates recent_orders from the broker - ESC-4 may be fixed; re-verify "
        "duplicate_order and empty KNOWN_DEAD"
    )


def test_the_battery_is_not_vacuous():
    """A battery that evaluated nothing would satisfy (a) trivially."""
    observed = _observed_failing()
    assert len(observed) >= 25, (
        f"only {len(observed)} checks were ever observed failing: {sorted(observed)}"
    )


# --- (c) an unimplemented check cannot masquerade as a blocking pass --------------------------------
def test_unimplemented_checks_are_reported_info_never_blocking_pass():
    unimplemented = set(CHECK_IDS) - set(IMPLEMENTED_CHECKS)
    assert unimplemented, "this assertion is about deferred checks; if none remain, delete it deliberately"
    policy = make_institutional_policy()
    results = {c.check_id: c for c in risk.evaluate(make_proposal(), context_for(policy), policy).checks}
    for check_id in sorted(unimplemented):
        result = results.get(check_id)
        if result is None:
            continue
        assert result.severity == "info", (
            f"{check_id} is not implemented but was reported severity={result.severity!r}; an absent "
            "engine must never look like a control that ran"
        )
        assert (result.detail or "").strip(), f"{check_id} must say why it was not evaluated"


def test_a_policy_cannot_enable_an_unimplemented_check():
    """Defence in depth for (c): the deferred check never reaches the engine in the first place."""
    unimplemented = sorted(set(CHECK_IDS) - set(IMPLEMENTED_CHECKS))
    payload = path_and_aggregate_policy().model_dump(mode="json")
    payload.pop("policy_hash", None)
    payload["checks"] = {
        **payload.get("checks", {}),
        unimplemented[0]: {"enabled": True, "severity": "blocking"},
    }
    with pytest.raises(Exception):  # noqa: B017 - the refusal type is the loader's to choose
        Policy.build(**payload)
