"""Every route is authenticated, identity comes only from the credential, and refusals say nothing.

Finding F-3 had two halves and both are pinned here: an API with no authentication at all, and an
identity read out of the request body. The second is the subtler one — an authenticated API that
still lets a caller write ``"agent": {...}`` into a proposal has not actually bound anything.
"""

from __future__ import annotations

import pytest

from mizan.api import ROUTES
from tests.api.conftest import TOKEN_A, TOKEN_B, TOKEN_READONLY, bearer, proposal_body
from tests.fixtures import TENANT_A, make_agent


def _request(client, method: str, path: str, headers=None):
    path = path.replace("{decision_id}", "01a00000-0000-7000-8000-000000000000")
    return client.request(method, path, headers=headers or {}, json={} if method == "POST" else None)


@pytest.mark.parametrize(("method", "path"), [route for route in ROUTES if route[1] != "/v1/health"])
def test_no_route_answers_without_a_credential(client, method, path):
    """The whole ``/v1`` surface, enumerated from ROUTES so a new route cannot be forgotten."""
    response = _request(client, method, path)

    assert response.status_code in {401, 403}, (method, path, response.text)
    assert "error" in response.json()


def test_health_is_the_only_anonymous_route(client):
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": TOKEN_A},
        {"Authorization": f"Basic {TOKEN_A}"},
        {"Authorization": "Bearer wrong-token-wrong-token"},
    ],
)
def test_a_malformed_or_unknown_credential_is_refused_identically(client, header):
    """One message for every failure mode: "which of my guesses was once real" is not a question we answer."""
    response = client.get("/v1/decisions", headers=header)

    assert response.status_code in {401, 403}
    body = response.json()["error"]
    assert body["message"] == "A valid bearer token is required."
    assert "token" not in body["message"].lower() or "bearer" in body["message"].lower()


def test_a_credential_without_the_scope_cannot_use_the_route(client):
    read_only = bearer(TOKEN_READONLY)

    assert client.get("/v1/decisions", headers=read_only).status_code == 200
    assert client.post("/v1/proposals/evaluate", json=proposal_body(), headers=read_only).status_code == 403
    assert client.post("/v1/control/kill-switch", json={"active": True}, headers=read_only).status_code == 403


def test_the_agent_identity_comes_from_the_token_not_the_body(client):
    """F-3: a body that names another agent is refused outright rather than quietly corrected."""
    impostor = proposal_body()
    impostor["agent"] = make_agent(agent_id="somebody-elses-agent").model_dump(mode="json")

    response = client.post("/v1/proposals/evaluate", json=impostor, headers=bearer(TOKEN_A))

    assert response.status_code in {403, 422}
    assert "somebody-elses-agent" not in response.text


def test_a_body_that_names_its_own_agent_is_accepted(client):
    body = proposal_body()
    body["agent"] = make_agent().model_dump(mode="json")

    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))

    assert response.status_code == 200


def test_a_client_supplied_proposal_id_does_not_choose_the_identity(client):
    """``proposal_id`` is a hash of the content; a caller cannot pre-assign one."""
    body = proposal_body()
    body["proposal_id"] = "f" * 64

    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))

    assert response.status_code == 200
    assert response.json()["proposal_id"] != "f" * 64


def test_the_token_binds_the_tenant_the_request_is_served_as(client, pipelines):
    a = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A))
    b = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_B))

    assert a.status_code == b.status_code == 200
    assert a.json()["decision_id"] != b.json()["decision_id"]
    assert [r.decision_id for r in pipelines[TENANT_A].list_decisions()] == [a.json()["decision_id"]]


def test_repeated_bad_credentials_are_rate_limited(build_client):
    from mizan.api import ApiConfig
    from mizan.api.ratelimit import RateLimit

    client = build_client(config=ApiConfig(auth_rate_limit=RateLimit(max_requests=3, window_seconds=60)))
    statuses = [
        client.get("/v1/decisions", headers={"Authorization": "Bearer no-such-token-at-all"}).status_code
        for _ in range(6)
    ]

    assert statuses[:3] == [403, 403, 403]
    assert 429 in statuses[3:], statuses
    # a good credential still works: the limiter charges the attempt, not the address forever
    assert client.get("/v1/decisions", headers=bearer(TOKEN_A)).status_code == 200


def test_a_token_is_never_echoed_back(client):
    response = client.get("/v1/decisions", headers={"Authorization": f"Bearer {TOKEN_A}xx"})

    assert TOKEN_A not in response.text
    assert response.status_code in {401, 403}
