"""Fail-closed validation: a policy may not name a control the running engine does not evaluate.

This is the check that keeps a policy document honest. A tenant who writes ``crowding: {enabled: true}``
against an engine that has no crowding check would otherwise read their own policy as protection they
do not have -- and would find out at the worst possible moment. So the load fails, loudly, naming the
checks, with CHECK_NOT_IMPLEMENTED.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mizan.contracts import CHECK_IDS, Policy
from mizan.contracts.canonical import canonical_json
from mizan.contracts.errors import PolicyError
from mizan.policy import validate_policy
from mizan.risk import DEFERRED_CHECKS, IMPLEMENTED_CHECKS
from tests.fixtures import make_institutional_policy, make_policy

SPRINT_3_DISABLED = {check_id: {"enabled": False, "severity": "blocking"} for check_id in DEFERRED_CHECKS}


def _payload(policy: Policy, **edits) -> dict:
    payload = json.loads(canonical_json(policy))
    payload.pop("policy_hash")
    payload.update(edits)
    return payload


def _conservative_payload(**edits) -> dict:
    policy = make_policy()
    checks = {**json.loads(canonical_json(policy))["checks"], **SPRINT_3_DISABLED}
    payload = _payload(policy, checks=checks)
    payload.update(edits)
    return payload


def test_a_policy_within_the_implemented_set_validates():
    policy = validate_policy(_conservative_payload())
    assert isinstance(policy, Policy)
    assert set(policy.enabled_checks) <= IMPLEMENTED_CHECKS


def test_the_institutional_policy_validates_as_it_ships():
    payload = _payload(make_institutional_policy())
    assert isinstance(validate_policy(payload), Policy)


def test_enabling_an_unimplemented_check_is_refused_and_the_check_is_named():
    checks = {**json.loads(canonical_json(make_institutional_policy()))["checks"]}
    checks["crowding"] = {"enabled": True, "severity": "blocking"}
    payload = _payload(make_institutional_policy(), checks=checks)
    with pytest.raises(PolicyError) as caught:
        validate_policy(payload)
    message = str(caught.value)
    assert "CHECK_NOT_IMPLEMENTED" in message
    assert "crowding" in message


def test_every_deferred_check_is_refused_when_switched_on():
    base = json.loads(canonical_json(make_institutional_policy()))["checks"]
    for check_id in sorted(DEFERRED_CHECKS):
        checks = {**base, check_id: {"enabled": True, "severity": "blocking"}}
        payload = _payload(make_institutional_policy(), checks=checks)
        with pytest.raises(PolicyError) as caught:
            validate_policy(payload)
        assert check_id in str(caught.value)


def test_a_section_that_implies_a_deferred_check_is_refused_by_default():
    """The options section implies assignment_risk and pin_risk unless they are explicitly disabled."""
    with pytest.raises(PolicyError) as caught:
        validate_policy(_payload(make_policy()))
    assert "assignment_risk" in str(caught.value)
    assert "pin_risk" in str(caught.value)


def test_the_implemented_set_can_be_widened_by_the_caller():
    """A future engine build declares more; validate_policy takes the engine's word, not its own."""
    payload = _payload(make_policy())
    policy = validate_policy(payload, implemented=frozenset(CHECK_IDS))
    assert policy.policy_id == "options-conservative"


def test_the_implemented_set_can_be_narrowed_by_the_caller():
    with pytest.raises(PolicyError) as caught:
        validate_policy(_conservative_payload(), implemented=frozenset({"market_data_presence"}))
    assert "CHECK_NOT_IMPLEMENTED" in str(caught.value)


def test_a_check_whose_section_is_absent_counts_as_disabled():
    payload = _conservative_payload()
    assert payload.get("aggregate") is None
    policy = validate_policy(payload)
    assert "aggregate_exposure" not in policy.enabled_checks
    assert policy.is_check_enabled("aggregate_exposure") is False


def test_enabling_a_check_whose_section_is_absent_is_refused():
    payload = _conservative_payload()
    payload["checks"]["aggregate_exposure"] = {"enabled": True, "severity": "blocking"}
    with pytest.raises(PolicyError) as caught:
        validate_policy(payload)
    assert "POLICY_INVALID" in str(caught.value)


@pytest.mark.parametrize(
    "check_id", ["market_data_presence", "portfolio_state_presence", "proposal_expiry", "response_level_gate"]
)
def test_an_always_on_check_cannot_be_disabled(check_id):
    payload = _conservative_payload()
    payload["checks"][check_id] = {"enabled": False, "severity": "blocking"}
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_an_always_on_check_cannot_be_downgraded_to_a_warning():
    payload = _conservative_payload()
    payload["checks"]["proposal_expiry"] = {"enabled": True, "severity": "warning"}
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_fail_closed_flags_cannot_be_switched_off():
    payload = _conservative_payload()
    payload["fail_closed"] = {**payload["fail_closed"], "on_missing_market_data": False}
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_an_unknown_check_id_is_refused():
    payload = _conservative_payload()
    payload["checks"]["not_a_real_check"] = {"enabled": True, "severity": "blocking"}
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_an_unknown_field_is_refused():
    with pytest.raises(PolicyError):
        validate_policy(_conservative_payload(surprise="value"))


def test_a_kelly_fraction_above_the_canon_cap_is_refused():
    payload = _payload(make_institutional_policy())
    payload["trade"] = {**payload["trade"], "kelly_fraction_cap": "0.75"}
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_drawdown_scaling_must_not_grow_with_drawdown():
    payload = _payload(make_institutional_policy())
    payload["path"] = {
        **payload["path"],
        "size_scaling_by_drawdown": [
            {"drawdown_pct": "0.05", "size_multiplier": "0.5"},
            {"drawdown_pct": "0.10", "size_multiplier": "1"},
        ],
    }
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_a_declared_hash_that_does_not_match_the_content_is_refused():
    payload = json.loads(canonical_json(make_institutional_policy()))
    payload["order"] = {**payload["order"], "max_quantity": "501"}
    with pytest.raises(PolicyError) as caught:
        validate_policy(payload)
    assert "POLICY_HASH_MISMATCH" in str(caught.value)


def test_a_non_string_hash_is_refused():
    payload = _payload(make_institutional_policy())
    payload["policy_hash"] = 12345
    with pytest.raises(PolicyError):
        validate_policy(payload)


def test_the_contract_still_refuses_a_hand_built_policy_with_a_wrong_hash():
    """validate_policy is a gate in front of the contract, not a replacement for it."""
    payload = json.loads(canonical_json(make_institutional_policy()))
    payload["policy_hash"] = "b" * 64
    with pytest.raises(ValidationError):
        Policy.model_validate(payload)
