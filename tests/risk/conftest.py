"""Helpers for the risk-engine suite, as fixtures (test modules must not import each other).

Everything is built from ``tests.fixtures``; nothing here constructs a contract object by hand.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fixtures import make_context, make_institutional_context


@pytest.fixture
def codes():
    """The set of reason-code strings on an evaluation (or any object with ``reason_codes``)."""

    def collect(obj: Any) -> set[str]:
        return {str(getattr(code, "value", code)) for code in obj.reason_codes}

    return collect


@pytest.fixture
def context_for():
    """A RiskContext bound to a policy: same tenant, same policy hash, plus overrides."""

    def build(policy, **overrides: Any):
        fields: dict[str, Any] = {"tenant_id": policy.tenant_id, "policy": policy.ref}
        fields.update(overrides)
        return make_context(**fields)

    return build


@pytest.fixture
def institutional_context_for():
    """A RiskContext carrying every Addendum-1 state, bound to an institutional policy."""

    def build(policy, **overrides: Any):
        fields: dict[str, Any] = {"tenant_id": policy.tenant_id, "policy": policy}
        fields.update(overrides)
        return make_institutional_context(**fields)

    return build


@pytest.fixture
def check_of():
    """One CheckResult out of an evaluation, by check id."""

    def pick(evaluation, check_id: str):
        for check in evaluation.checks:
            if check.check_id == check_id:
                return check
        raise AssertionError(f"no CheckResult for {check_id}")

    return pick
