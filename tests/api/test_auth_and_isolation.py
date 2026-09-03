"""Authentication on every route, and the tenant boundary that survives a direct attempt on it.

Finding F-3 was an API with no authentication at all: anyone who could reach the port could trigger a
paper order submission and read the whole account. This module walks the *entire* declared surface -
not a sample of it - with an absent, malformed, expired, unknown and other-tenant credential, so a
route added later without a dependency is caught by the sweep rather than by an incident.

Finding F-17 was the absence of any tenant concept. Here tenant A's decision id is asked for with
tenant B's token and the answer is 404: not 403, because "you may not see this" and "this does not
exist" must be indistinguishable, or the error code becomes an oracle for enumerating other tenants.

Self-contained: this module builds its own app so its assertions cannot be re-pointed by a change to a
shared fixture.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mizan.adapters import MockBroker
from mizan.api import ROUTES, ApiConfig, Principal, StaticTokenStore, create_app
from mizan.audit import InMemoryLedger
from mizan.contracts.errors import ConfigurationError
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

# Fixtures, not secrets. The store keeps only SHA-256 digests, and these strings are obviously inert.
TOKEN_A = "api-suite-token-tenant-a-00001"  # secret-scan: allow
TOKEN_B = "api-suite-token-tenant-b-00002"  # secret-scan: allow
TOKEN_EXPIRED = "api-suite-token-expired-00003"  # secret-scan: allow
TOKEN_UNKNOWN = "api-suite-token-unknown-00004"  # secret-scan: allow


def _pipeline(tenant_id: str, ledger: InMemoryLedger) -> Mizan:
    return Mizan(
        tenant_id=tenant_id,
        agent=make_agent(),
        policy=make_policy(tenant_id=tenant_id),
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=ledger,
        advisory=None,
        kill_switch=InMemoryKillSwitch(),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )


def _world(config: ApiConfig | None = None) -> tuple[TestClient, dict[str, Mizan]]:
    """One ledger shared by two tenants: the arrangement a cross-tenant read would have to exploit."""
    ledger = InMemoryLedger()
    pipelines = {TENANT_A: _pipeline(TENANT_A, ledger), TENANT_B: _pipeline(TENANT_B, ledger)}
    agent = make_agent()
    store = StaticTokenStore(
        {
            TOKEN_A: Principal(token_id="a", tenant_id=TENANT_A, agent=agent, scopes=ALL_SCOPES),
            TOKEN_B: Principal(token_id="b", tenant_id=TENANT_B, agent=agent, scopes=ALL_SCOPES),
            TOKEN_EXPIRED: Principal(
                token_id="x",
                tenant_id=TENANT_A,
                agent=agent,
                scopes=ALL_SCOPES,
                expires_at=FIXED_NOW - timedelta(seconds=1),
            ),
        }
    )
    app = create_app(
        lambda tenant_id: pipelines.get(tenant_id),
        tokens=store,
        config=config if config is not None else ApiConfig(),
    )
    return TestClient(app, raise_server_exceptions=False), pipelines


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(**overrides: Any) -> dict[str, Any]:
    """A proposal payload as a client sends it: no ``agent``, no ``proposal_id`` (F-3)."""
    payload = make_proposal(**overrides).model_dump(mode="json")
    payload.pop("proposal_id")
    payload.pop("agent")
    return payload


def _evaluate(client: TestClient, token: str) -> str:
    response = client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(token))
    assert response.status_code == 200, response.text
    return response.json()["decision_id"]


def _call(client: TestClient, method: str, path: str, headers: dict[str, str] | None = None) -> Any:
    """Issue one request against a declared route, substituting a placeholder for the path parameter."""
    concrete = path.replace("{decision_id}", "01a00000-0000-7000-8000-000000000000")
    body = _body() if concrete.endswith("/evaluate") else {"active": False}
    if method == "GET":
        return client.get(concrete, headers=headers)
    return client.post(concrete, json=body, headers=headers)


PROTECTED_ROUTES = tuple((method, path) for method, path in ROUTES if path != "/v1/health")


# ---------------------------------------------------------------------------------------------
# F-3: there is no route without a credential
# ---------------------------------------------------------------------------------------------
def test_the_declared_surface_is_the_surface_the_app_actually_serves():
    client, _ = _world()
    served = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if str(route.path).startswith("/v1") and method in {"GET", "POST"}
    }
    assert served == set(ROUTES), served.symmetric_difference(set(ROUTES))


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_every_route_refuses_an_absent_credential(method, path):
    client, _ = _world()
    response = _call(client, method, path)
    assert response.status_code == 403, (path, response.status_code, response.text)
    assert response.json()["error"]["code"]


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": TOKEN_A},
        {"Authorization": f"Basic {TOKEN_A}"},
        {"Authorization": f"bearer{TOKEN_A}"},
        {"Authorization": "Bearer ../../etc/passwd"},
        {"X-Api-Key": TOKEN_A},
    ],
)
@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_every_route_refuses_a_malformed_credential(method, path, header):
    client, _ = _world()
    response = _call(client, method, path, headers=header)
    assert response.status_code == 403, (path, header, response.text)


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_every_route_refuses_an_expired_credential(method, path):
    client, _ = _world()
    response = _call(client, method, path, headers=_bearer(TOKEN_EXPIRED))
    assert response.status_code == 403, (path, response.text)


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_every_route_refuses_an_unknown_credential(method, path):
    client, _ = _world()
    response = _call(client, method, path, headers=_bearer(TOKEN_UNKNOWN))
    assert response.status_code == 403, (path, response.text)


def test_a_refused_request_never_reaches_the_pipeline():
    client, pipelines = _world()
    for method, path in PROTECTED_ROUTES:
        _call(client, method, path)
        _call(client, method, path, headers=_bearer(TOKEN_UNKNOWN))
    for pipeline in pipelines.values():
        assert pipeline.list_decisions() == []
        assert pipeline.broker.submitted == []
        assert pipeline.broker.log == []


def test_a_scope_a_token_does_not_hold_is_refused():
    """The authority to read is not the authority to trade, or to stop trading."""
    ledger = InMemoryLedger()
    pipelines = {TENANT_A: _pipeline(TENANT_A, ledger)}
    reader = "api-suite-token-readonly-0005"  # secret-scan: allow
    store = StaticTokenStore(
        {
            reader: Principal(
                token_id="r", tenant_id=TENANT_A, agent=make_agent(), scopes=frozenset({"read"})
            )
        }
    )
    client = TestClient(
        create_app(lambda tenant_id: pipelines.get(tenant_id), tokens=store), raise_server_exceptions=False
    )

    assert client.get("/v1/decisions", headers=_bearer(reader)).status_code == 200
    assert client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(reader)).status_code == 403
    assert (
        client.post("/v1/control/kill-switch", json={"active": True}, headers=_bearer(reader)).status_code
        == 403
    )
    assert pipelines[TENANT_A].kill_switch.is_active() is False


def test_an_app_with_no_token_store_refuses_everything_rather_than_opening_up():
    """Fail closed: a deployment that forgot to configure credentials serves no one."""
    ledger = InMemoryLedger()
    pipelines = {TENANT_A: _pipeline(TENANT_A, ledger)}
    client = TestClient(create_app(lambda t: pipelines.get(t)), raise_server_exceptions=False)

    for method, path in PROTECTED_ROUTES:
        assert _call(client, method, path, headers=_bearer(TOKEN_A)).status_code == 403


# ---------------------------------------------------------------------------------------------
# F-17 / B3: the tenant boundary
# ---------------------------------------------------------------------------------------------
def test_tenant_b_cannot_fetch_tenant_as_decision_and_gets_404_not_403():
    """IDOR: the answer must not distinguish 'not yours' from 'does not exist' (B3)."""
    client, _ = _world()
    decision_id = _evaluate(client, TOKEN_A)

    assert client.get(f"/v1/decisions/{decision_id}", headers=_bearer(TOKEN_A)).status_code == 200

    stolen = client.get(f"/v1/decisions/{decision_id}", headers=_bearer(TOKEN_B))
    invented = client.get(
        "/v1/decisions/01a00000-0000-7000-8000-00000000dead", headers=_bearer(TOKEN_B)
    )
    assert stolen.status_code == 404, stolen.text
    assert invented.status_code == 404
    assert stolen.json()["error"]["code"] == invented.json()["error"]["code"]
    assert stolen.json()["error"]["message"] == invented.json()["error"]["message"]
    assert decision_id not in stolen.text


def test_tenant_b_can_neither_execute_nor_replay_tenant_as_decision():
    client, pipelines = _world()
    decision_id = _evaluate(client, TOKEN_A)

    for path in (f"/v1/decisions/{decision_id}/execute", f"/v1/decisions/{decision_id}/replay"):
        response = client.post(path, json={}, headers=_bearer(TOKEN_B))
        assert response.status_code == 404, (path, response.text)
    assert pipelines[TENANT_A].broker.submitted == []
    assert pipelines[TENANT_B].broker.submitted == []


def test_a_listing_only_ever_shows_the_callers_own_decisions():
    client, _ = _world()
    mine = _evaluate(client, TOKEN_A)
    theirs = _evaluate(client, TOKEN_B)
    assert mine != theirs

    listed = client.get("/v1/decisions", headers=_bearer(TOKEN_A)).json()["decisions"]
    assert [entry["decision_id"] for entry in listed] == [mine]
    assert theirs not in client.get("/v1/decisions", headers=_bearer(TOKEN_A)).text


def test_the_policy_route_serves_only_the_callers_own_policy():
    client, pipelines = _world()
    served = client.get("/v1/policy", headers=_bearer(TOKEN_A)).json()
    assert served["tenant_id"] == TENANT_A
    assert served["policy_hash"] == pipelines[TENANT_A].policy.policy_hash


# ---------------------------------------------------------------------------------------------
# F-3: identity comes from the token, never from the body
# ---------------------------------------------------------------------------------------------
def test_a_body_naming_another_agent_is_refused_rather_than_quietly_corrected():
    client, pipelines = _world()
    body = _body()
    body["agent"] = make_agent(agent_id="somebody-elses-agent").model_dump(mode="json")

    response = client.post("/v1/proposals/evaluate", json=body, headers=_bearer(TOKEN_A))

    assert response.status_code == 403, response.text
    assert pipelines[TENANT_A].list_decisions() == []


def test_a_body_naming_no_agent_is_completed_from_the_token():
    client, pipelines = _world()
    decision_id = _evaluate(client, TOKEN_A)
    record = pipelines[TENANT_A].get_decision(decision_id)
    assert record.agent_id == make_agent().agent_id
    assert record.proposal.agent.agent_id == make_agent().agent_id


def test_a_body_naming_the_callers_own_agent_is_accepted():
    client, _ = _world()
    body = _body()
    body["agent"] = make_agent().model_dump(mode="json")
    assert client.post("/v1/proposals/evaluate", json=body, headers=_bearer(TOKEN_A)).status_code == 200


def test_a_body_choosing_its_own_proposal_id_cannot_forge_one():
    client, pipelines = _world()
    body = _body()
    body["proposal_id"] = "f" * 64

    response = client.post("/v1/proposals/evaluate", json=body, headers=_bearer(TOKEN_A))

    assert response.status_code == 200, response.text
    assert response.json()["proposal_id"] != "f" * 64, "the id is derived from the content, not chosen"


# ---------------------------------------------------------------------------------------------
# F-14 / F-15: what an error and a health check may say
# ---------------------------------------------------------------------------------------------
def test_an_error_body_is_a_code_a_generic_message_and_a_correlation_id():
    client, _ = _world()
    response = client.get(
        "/v1/decisions/01a00000-0000-7000-8000-00000000beef", headers=_bearer(TOKEN_A)
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) >= {"code", "message", "correlation_id"}
    assert error["correlation_id"] and error["code"]
    body = response.text.lower()
    for leak in ("traceback", "sqlite", ".py", "line ", "mizan/", "site-packages"):
        assert leak not in body, leak


def test_a_malformed_body_does_not_echo_the_input_or_the_parser(caplog):
    client, _ = _world()
    response = client.post(
        "/v1/proposals/evaluate",
        content=b'{"symbol": "AAPL", "quantity": ',
        headers={**_bearer(TOKEN_A), "Content-Type": "application/json"},
    )
    assert response.status_code in {400, 422}
    assert "AAPL" not in response.text
    assert "quantity" not in response.text
    assert response.json()["error"]["correlation_id"]


def test_health_gives_liveness_to_anyone_and_control_plane_state_only_to_a_tenant():
    """F-15: execution flags and the vendor's name are not public information."""
    client, _ = _world()
    anonymous = client.get("/v1/health")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"status": "ok"}
    for leak in ("dry_run", "kill_switch", "enabled", "mock", "tenant"):
        assert leak not in anonymous.text

    authenticated = client.get("/v1/health", headers=_bearer(TOKEN_A)).json()
    assert authenticated["status"] == "ok"
    assert authenticated["tenant_id"] == TENANT_A
    assert authenticated["environment"] == "paper"
    assert set(authenticated["execution"]) == {"enabled", "dry_run", "kill_switch_active"}
    assert authenticated["broker"] == "mock"


def test_an_invalid_credential_on_health_still_only_yields_liveness():
    client, _ = _world()
    for header in ({"Authorization": "Bearer nope"}, {"Authorization": "garbage"}):
        response = client.get("/v1/health", headers=header)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------------------------
# rate limiting and CORS
# ---------------------------------------------------------------------------------------------
def test_the_evaluate_route_is_rate_limited_per_principal():
    from mizan.api.ratelimit import RateLimit

    client, pipelines = _world(ApiConfig(evaluate_rate_limit=RateLimit(max_requests=2, window_seconds=60)))

    assert client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(TOKEN_A)).status_code == 200
    assert client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(TOKEN_A)).status_code == 200
    limited = client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(TOKEN_A))
    assert limited.status_code == 429, limited.text

    # the limit is per principal: another tenant's budget is untouched
    assert client.post("/v1/proposals/evaluate", json=_body(), headers=_bearer(TOKEN_B)).status_code == 200
    assert len(pipelines[TENANT_A].list_decisions()) == 2


def test_reads_are_not_rate_limited_by_the_evaluate_budget():
    from mizan.api.ratelimit import RateLimit

    client, _ = _world(ApiConfig(evaluate_rate_limit=RateLimit(max_requests=1, window_seconds=60)))
    _evaluate(client, TOKEN_A)
    for _ in range(5):
        assert client.get("/v1/decisions", headers=_bearer(TOKEN_A)).status_code == 200


@pytest.mark.parametrize("origin", ["*", "", "   ", "null"])
def test_a_wildcard_cors_origin_is_refused_at_configuration_time(origin):
    with pytest.raises(ConfigurationError):
        ApiConfig(cors_origins=(origin,))


def test_a_named_cors_origin_is_honoured():
    client, _ = _world(ApiConfig(cors_origins=("https://console.example.com",)))
    response = client.get(
        "/v1/health", headers={"Origin": "https://console.example.com"}
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://console.example.com"


def test_the_interactive_docs_and_schema_are_not_served():
    """A machine-readable map of the surface is not something an unauthenticated caller needs."""
    client, _ = _world()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
