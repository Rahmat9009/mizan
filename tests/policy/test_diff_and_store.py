"""Policy diffing and tenant-scoped storage.

The diff is what the console shows an operator before they activate a new version, and what policy
replay explains a changed verdict with, so it has to name the exact field: "portfolio.max_drawdown_pct
0.20 -> 0.10", not "the portfolio section changed".
"""

from __future__ import annotations

import json

import pytest

from mizan.contracts.canonical import canonical_json
from mizan.contracts.errors import NotFound, PolicyError
from mizan.policy import InMemoryPolicyStore, PolicyStore, diff_policies, policy_hash
from tests.fixtures import TENANT_A, TENANT_B, make_institutional_policy, make_policy


def _paths(changes) -> dict[str, tuple]:
    return {change.path: (change.old, change.new) for change in changes}


# ------------------------------------------------------------------------------------------------
# policy_hash / diff_policies
# ------------------------------------------------------------------------------------------------
def test_policy_hash_matches_the_contract_hash():
    policy = make_policy()
    assert policy_hash(policy) == policy.policy_hash


def test_an_identical_policy_has_no_differences():
    assert diff_policies(make_policy(), make_policy()) == []


def test_a_changed_scalar_is_reported_at_its_dotted_path():
    old = make_policy()
    new = make_policy(
        portfolio={
            "max_single_symbol_pct": "0.15",
            "max_sector_concentration_pct": "0.25",
            "max_drawdown_pct": "0.10",
            "max_buying_power_utilization": "0.80",
        }
    )
    paths = _paths(diff_policies(old, new))
    assert paths["portfolio.max_drawdown_pct"] == ("0.2", "0.1")
    assert "policy_hash" in paths  # the hash always moves with the content


def test_an_added_and_a_removed_mapping_key_are_both_reported():
    old = make_policy()
    checks = json.loads(canonical_json(old))["checks"]
    del checks["duplicate_order"]
    checks["leg_limit"] = {"enabled": False, "severity": "blocking"}
    new = make_policy(checks=checks)
    paths = _paths(diff_policies(old, new))
    assert paths["checks.duplicate_order.enabled"][1] is None
    assert paths["checks.leg_limit.enabled"] == (None, False)


def test_list_elements_are_reported_by_index():
    old = make_policy()
    new = make_policy(restricted={"symbols": ["GME", "TSLA", "SPY"], "strategies": []})
    paths = _paths(diff_policies(old, new))
    assert paths["restricted.symbols[1]"] == ("AMC", "TSLA")
    assert paths["restricted.symbols[2]"] == (None, "SPY")


def test_a_shorter_list_reports_the_dropped_element():
    old = make_policy()
    new = make_policy(restricted={"symbols": ["GME"], "strategies": []})
    paths = _paths(diff_policies(old, new))
    assert paths["restricted.symbols[1]"] == ("AMC", None)


def test_a_whole_optional_section_appearing_is_reported_field_by_field():
    old = make_policy()
    new = make_institutional_policy()
    paths = _paths(diff_policies(old, new))
    assert paths["path.max_consecutive_losses_before_review"] == (None, 5)
    assert paths["path.size_scaling_by_drawdown[0].drawdown_pct"] == (None, "0.05")
    assert paths["trade.require_invalidation"] == (None, True)


def test_nested_response_ladder_entries_are_addressable():
    old = make_institutional_policy()
    ladder = json.loads(canonical_json(old))["response_ladder"]
    ladder["levels"][0]["size_multiplier"] = "0.6"
    new = make_institutional_policy(response_ladder=ladder)
    paths = _paths(diff_policies(old, new))
    assert paths["response_ladder.levels[0].size_multiplier"] == ("0.75", "0.6")


def test_the_diff_is_stable_and_ordered_by_path():
    old = make_policy()
    new = make_institutional_policy()
    first = [change.path for change in diff_policies(old, new)]
    second = [change.path for change in diff_policies(old, new)]
    assert first == second == sorted(first, key=first.index)  # deterministic, same order every time


# ------------------------------------------------------------------------------------------------
# InMemoryPolicyStore
# ------------------------------------------------------------------------------------------------
def test_the_in_memory_store_satisfies_the_protocol():
    assert isinstance(InMemoryPolicyStore(), PolicyStore)


def test_a_stored_policy_comes_back_by_version():
    store = InMemoryPolicyStore()
    policy = make_policy()
    store.put(policy)
    assert store.get(TENANT_A, policy.policy_id, policy.policy_version) == policy


def test_get_without_a_version_returns_the_active_one():
    store = InMemoryPolicyStore()
    first = make_policy(policy_version="1.0.0")
    second = make_policy(policy_version="2.0.0")
    store.put(first)
    store.put(second)
    assert store.get(TENANT_A, first.policy_id).policy_version == "1.0.0"
    store.activate(TENANT_A, first.policy_id, "2.0.0")
    assert store.get(TENANT_A, first.policy_id).policy_version == "2.0.0"
    assert store.active(TENANT_A, first.policy_id).policy_version == "2.0.0"


def test_storing_a_new_version_never_silently_switches_the_active_one():
    store = InMemoryPolicyStore()
    store.put(make_policy(policy_version="1.0.0"))
    store.put(make_policy(policy_version="9.9.9"))
    assert store.active(TENANT_A, "options-conservative").policy_version == "1.0.0"


def test_activating_a_version_that_was_never_stored_is_not_found():
    store = InMemoryPolicyStore()
    store.put(make_policy())
    with pytest.raises(NotFound):
        store.activate(TENANT_A, "options-conservative", "3.0.0")


def test_an_unknown_policy_is_not_found():
    store = InMemoryPolicyStore()
    with pytest.raises(NotFound):
        store.get(TENANT_A, "options-conservative", "1.4.0")
    with pytest.raises(NotFound):
        store.active(TENANT_A, "options-conservative")


def test_a_policy_is_reachable_by_its_hash():
    store = InMemoryPolicyStore()
    policy = make_policy()
    store.put(policy)
    assert store.get_by_hash(TENANT_A, policy.policy_hash) == policy
    with pytest.raises(NotFound):
        store.get_by_hash(TENANT_A, "0" * 64)


def test_one_tenant_can_never_read_another_tenants_policy():
    store = InMemoryPolicyStore()
    theirs = make_policy(tenant_id=TENANT_B)
    store.put(theirs)
    with pytest.raises(NotFound):
        store.get(TENANT_A, theirs.policy_id, theirs.policy_version)
    with pytest.raises(NotFound):
        store.get_by_hash(TENANT_A, theirs.policy_hash)
    with pytest.raises(NotFound):
        store.active(TENANT_A, theirs.policy_id)
    assert store.get(TENANT_B, theirs.policy_id, theirs.policy_version) == theirs


def test_two_tenants_may_hold_the_same_policy_id_independently():
    store = InMemoryPolicyStore()
    mine = make_policy(tenant_id=TENANT_A)
    theirs = make_policy(tenant_id=TENANT_B, order={"max_notional": "1", "max_quantity": "1", "max_legs": 1})
    store.put(mine)
    store.put(theirs)
    assert store.active(TENANT_A, mine.policy_id).order.max_notional == "10000"
    assert store.active(TENANT_B, theirs.policy_id).order.max_notional == "1"


def test_rewriting_a_version_with_different_content_is_refused():
    store = InMemoryPolicyStore()
    store.put(make_policy())
    tampered = make_policy(order={"max_notional": "999999", "max_quantity": "20", "max_legs": 4})
    with pytest.raises(PolicyError):
        store.put(tampered)


def test_storing_the_same_version_twice_is_idempotent():
    store = InMemoryPolicyStore()
    store.put(make_policy())
    store.put(make_policy())
    assert store.versions(TENANT_A, "options-conservative") == ["1.4.0"]


def test_the_store_lists_versions_and_tenants_deterministically():
    store = InMemoryPolicyStore()
    store.put(make_policy(policy_version="2.0.0"))
    store.put(make_policy(policy_version="1.0.0"))
    store.put(make_policy(tenant_id=TENANT_B))
    assert store.versions(TENANT_A, "options-conservative") == ["1.0.0", "2.0.0"]
    assert store.tenants() == [TENANT_A, TENANT_B]
