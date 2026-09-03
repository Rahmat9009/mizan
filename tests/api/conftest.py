"""Fixtures for the REST suite: two tenants, one app, and tokens that carry the only identity there is.

Every request in this suite authenticates with a bearer token and nothing else. That is not a testing
convenience — it is the property under test. Finding F-3 was an API that read the tenant and the agent
out of the request body, so the suite is arranged so that a test *cannot* express "act as tenant X" any
way other than by presenting X's credential.
"""

from __future__ import annotations

from typing import Any

import pytest

from mizan.adapters import MockBroker
from mizan.api import ApiConfig, Principal, StaticTokenStore, create_app
from mizan.audit import InMemoryLedger
from mizan.execution import ExecutionConfig, InMemoryKillSwitch
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    TENANT_B,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

ALL_SCOPES = frozenset({"read", "evaluate", "execute", "control"})

#: Test credentials. Sixteen characters is the store's floor; these are fixtures, not secrets, and the
#: store keeps only their SHA-256 digests either way.
TOKEN_A = "test-token-tenant-a-0000000000"  # secret-scan: allow
TOKEN_B = "test-token-tenant-b-0000000000"  # secret-scan: allow
TOKEN_READONLY = "test-token-readonly-000000000"  # secret-scan: allow


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def proposal_body(**overrides: Any) -> dict[str, Any]:
    """A proposal payload as a client sends it: no ``agent``, no ``proposal_id`` (F-3)."""
    body = make_proposal(**overrides).model_dump(mode="json")
    body.pop("proposal_id")
    body.pop("agent")
    return body


def build_pipeline(tenant_id: str, ledger, **overrides: Any) -> Mizan:
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "agent": make_agent(),
        "policy": make_policy(tenant_id=tenant_id),
        "broker": MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        "ledger": ledger,
        "advisory": None,
        "kill_switch": InMemoryKillSwitch(),
        "config": ExecutionConfig(enabled=True, dry_run=True),
        "clock": lambda: FIXED_NOW,
    }
    fields.update(overrides)
    return Mizan(**fields)


@pytest.fixture
def ledger() -> InMemoryLedger:
    """One ledger for both tenants: the arrangement a cross-tenant read would have to exploit."""
    return InMemoryLedger()


@pytest.fixture
def pipelines(ledger) -> dict[str, Mizan]:
    return {
        TENANT_A: build_pipeline(TENANT_A, ledger),
        TENANT_B: build_pipeline(TENANT_B, ledger),
    }


@pytest.fixture
def tokens() -> StaticTokenStore:
    agent = make_agent()
    return StaticTokenStore(
        {
            TOKEN_A: Principal(
                token_id="tok-a", tenant_id=TENANT_A, agent=agent, scopes=frozenset(ALL_SCOPES)
            ),
            TOKEN_B: Principal(
                token_id="tok-b", tenant_id=TENANT_B, agent=agent, scopes=frozenset(ALL_SCOPES)
            ),
            TOKEN_READONLY: Principal(
                token_id="tok-r", tenant_id=TENANT_A, agent=agent, scopes=frozenset({"read"})
            ),
        }
    )


@pytest.fixture
def build_client(pipelines, tokens):
    """Factory for a TestClient over the multi-tenant resolver form of ``create_app``."""
    from fastapi.testclient import TestClient

    def build(*, config: ApiConfig | None = None, store: Any = None, resolver: Any = None):
        app = create_app(
            resolver if resolver is not None else (lambda tenant_id: pipelines.get(tenant_id)),
            tokens=tokens if store is None else store,
            config=config if config is not None else ApiConfig(),
        )
        return TestClient(app, raise_server_exceptions=False)

    return build


@pytest.fixture
def client(build_client):
    return build_client()
