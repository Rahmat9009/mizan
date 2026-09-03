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
from mizan.contracts.types import dstr, format_ts
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

#: Checks that are enabled and blocking but structurally incapable of failing in a shipped pipeline.
#: EMPTY, and it must stay empty.
#:
#: ESC-4 put ``duplicate_order`` here: ``RiskContext.recent_orders`` was a parameter of
#: ``BrokerContextProvider.build`` defaulting to ``()`` that no caller in ``mizan/`` populated, so the
#: check's loop body was unreachable and it reported ``passed=True`` at blocking severity on every
#: proposal. ESC-4 is now CLOSED - the provider derives recent orders from the tenant's own chain, and
#: the exemplar below observes the check failing on a real duplicate.
#:
#: Adding anything here is a HALT, not a workaround: ``test_no_check_is_known_dead`` asserts this is
#: empty, and ``test_the_wiring_that_closed_esc4_is_still_in_place`` pins the derivation itself, so
#: deleting that wiring reopens ESC-4 loudly rather than silently.
KNOWN_DEAD: dict[str, str] = {}


def _exemplars():
    """Checks needing a precisely shaped input the battery does not stumble into.

    Each entry is ``(proposal, policy)`` or ``(proposal, policy, context)`` - the third form is for
    checks whose failing condition lives in the CONTEXT rather than the proposal or policy.
    """
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
        # ESC-4's check, now that recent_orders is derived rather than structurally empty. The failing
        # condition is a prior order at the venue, which lives in the context.
        "duplicate_order": (
            proposal,
            path_and_aggregate_policy(),
            context_for(
                path_and_aggregate_policy(),
                recent_orders=[
                    {
                        "proposal_id": proposal.proposal_id,
                        "symbol": proposal.symbol,
                        "side": proposal.legs[0].side,
                        "total_quantity": dstr(proposal.total_quantity),
                        "submitted_at": format_ts(FIXED_NOW - timedelta(seconds=5)),
                        "status": "accepted",
                    }
                ],
            ),
        ),
        # F-31: a bull_call_spread whose legs are BOTH short - the right leg COUNT, the wrong
        # structure. This exact input was APPROVEd for 10 contracts before structure_valid existed.
        "structure_valid": (
            rebuild_proposal(
                make_option_proposal(),
                strategy="bull_call_spread",
                legs=[
                    {**leg, "leg_index": i, "side": "sell", "contract_type": "call", "strike": strike}
                    for i, (leg, strike) in enumerate(
                        zip(
                            make_option_proposal().model_dump(mode="json")["legs"] * 2,
                            ("230", "235"),
                            strict=False,
                        )
                    )
                ],
            ),
            path_and_aggregate_policy(),
        ),
        # REQ-35: the failing condition is the ACCOUNT's state, not the order's.
        "account_capability": (
            proposal,
            path_and_aggregate_policy(),
            context_for(
                path_and_aggregate_policy(),
                account_state={
                    "as_of": format_ts(FIXED_NOW),
                    "status": "ACTIVE",
                    "trading_blocked": True,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                    "shorting_enabled": True,
                    "options_trading_level": 2,
                },
            ),
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
    for check_id, entry in _exemplars().items():
        proposal, policy = entry[0], entry[1]
        context = entry[2] if len(entry) > 2 else context_for(policy)
        result = CHECK_FUNCTIONS[check_id](proposal, context, policy)
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


def test_no_check_is_known_dead():
    """The ratchet. KNOWN_DEAD must be EMPTY; adding to it is a HALT, not a workaround.

    An xfail is not available here and should not be: this suite's conftest classifies a skipped or
    xfailed invariant as BLOCKING, because an invariant that does not run proves nothing. So the gap is
    pinned by identity instead - and now that ESC-4 is closed, the identity is "nothing".
    """
    assert KNOWN_DEAD == {}, (
        f"KNOWN_DEAD is not empty: {sorted(KNOWN_DEAD)}. A check that cannot fail is a control reporting "
        "success on no evidence, written into a record whose only purpose is to be believed later. Fix "
        "or remove the check. Do NOT widen this set to go green - that is the ESC-4 mistake repeated."
    )


def test_the_wiring_that_closed_esc4_is_still_in_place():
    """ESC-4 was missing WIRING, not broken logic: duplicate_order could not fail because nothing ever
    populated what it reads. Deleting that derivation would silently reopen it, and every other test
    here would stay green - so the derivation itself is pinned."""
    provider = (REPO_ROOT / "mizan" / "adapters" / "context.py").read_text(encoding="utf-8")
    assert "_recent_orders_from_ledger" in provider, (
        "BrokerContextProvider no longer derives recent_orders from the ledger; ESC-4 is reopened and "
        "duplicate_order is dead again"
    )
    assert "self.ledger" in provider, "the derivation must read the tenant's chain"


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
