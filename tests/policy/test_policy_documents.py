"""The shipped policy documents under ``policies/`` load, validate, hash and evaluate.

A policy file that does not load is a policy nobody is protected by, so these are treated as artefacts
under test rather than as documentation. The institutional document is also the byte-for-byte document
form of ``tests.fixtures.make_institutional_policy()``: if the two ever drift, one of them is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mizan import risk
from mizan.contracts import Policy
from mizan.policy import diff_policies, load_policy, policy_hash
from mizan.risk import IMPLEMENTED_CHECKS
from tests.fixtures import (
    make_context,
    make_institutional_context,
    make_institutional_policy,
    make_policy,
    make_proposal,
)

POLICIES_DIR = Path(__file__).resolve().parents[2] / "policies"
DOCUMENTS = sorted(POLICIES_DIR.glob("*.yaml"))


def _load(name: str) -> Policy:
    return load_policy((POLICIES_DIR / name).read_text(encoding="utf-8"))


def test_the_policies_directory_is_not_empty():
    assert [path.name for path in DOCUMENTS] == [
        "institutional.yaml",
        "options-conservative.yaml",
        # Added for the live options run: the same controls minus the greek/DTE section, because
        # Alpaca serves no greeks without an OPRA agreement and OptionsLimits makes all three
        # portfolio greek limits REQUIRED - so the section is all-or-nothing on that data tier.
        "options-defined-risk.yaml",
    ]


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_every_shipped_document_loads_validates_and_hashes(path):
    policy = load_policy(path.read_text(encoding="utf-8"))
    assert isinstance(policy, Policy)
    assert policy_hash(policy) == policy.policy_hash
    assert set(policy.enabled_checks) <= IMPLEMENTED_CHECKS


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_every_shipped_document_carries_no_binary_fraction(path):
    policy = load_policy(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    def walk(value, at):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{at}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{at}[{index}]")
        elif value is not None and not isinstance(value, (str, bool, int)):
            offenders.append(f"{at}: {type(value).__name__}")

    walk(policy.model_dump(mode="json"), "$")
    assert not offenders, offenders


def test_the_conservative_document_is_the_master_plan_policy():
    policy = _load("options-conservative.yaml")
    assert policy.policy_id == "options-conservative"
    assert policy.policy_version == "1.4.0"
    assert policy.order.max_notional == "10000"
    assert policy.order.max_quantity == "20"
    assert policy.portfolio.max_drawdown_pct == "0.2"
    assert policy.options is not None
    assert (policy.options.min_days_to_expiry, policy.options.max_days_to_expiry) == (7, 45)
    assert policy.restricted.symbols == ["GME", "AMC"]
    assert policy.authorization.ttl_seconds == 15
    assert policy.fail_closed.on_advisory_unavailable is False


def test_the_conservative_document_differs_from_the_fixture_only_by_the_deferred_disables():
    changed = {change.path for change in diff_policies(make_policy(), _load("options-conservative.yaml"))}
    assert changed == {
        "policy_hash",
        "checks.assignment_risk.enabled",
        "checks.assignment_risk.severity",
        "checks.pin_risk.enabled",
        "checks.pin_risk.severity",
    }


def test_the_institutional_document_is_the_fixture_policy_exactly():
    assert _load("institutional.yaml").policy_hash == make_institutional_policy().policy_hash
    assert diff_policies(make_institutional_policy(), _load("institutional.yaml")) == []


def test_the_institutional_document_populates_every_optional_section():
    policy = _load("institutional.yaml")
    for section in ("trade", "path", "aggregate", "response_ladder", "liquidity", "time", "tail", "factor"):
        assert getattr(policy, section) is not None, section
    assert policy.agent_budgets
    assert policy.options is not None and policy.options.max_short_gamma == "100"


def test_the_conservative_document_evaluates_a_proposal():
    policy = _load("options-conservative.yaml")
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    evaluation = risk.evaluate(make_proposal(), context, policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.policy.hash == policy.policy_hash


def test_the_institutional_document_evaluates_a_proposal():
    policy = _load("institutional.yaml")
    context = make_institutional_context(policy=policy)
    proposal = make_proposal(invalidation={"level": "224", "direction": "below", "target": "240"})
    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict == "PASS"
    assert evaluation.data_complete is True
