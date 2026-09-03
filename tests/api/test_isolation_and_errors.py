"""No IDOR, and no internal state in an error body.

Two findings, one theme: a response must not tell a caller anything about a world it is not entitled
to see. F-17 is about *existence* — an id that belongs to someone else must be indistinguishable from
one that never existed. F-14 is about *internals* — a traceback, a table name, a filesystem path or a
lower layer's exception text must never leave the process.
"""

from __future__ import annotations

import json

import pytest

from tests.api.conftest import TOKEN_A, TOKEN_B, bearer, proposal_body

UNKNOWN_ID = "01a00000-0000-7000-8000-000000000000"


@pytest.fixture
def decision(client):
    response = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A))
    assert response.status_code == 200
    return response.json()["decision_id"]


def _error(response) -> dict:
    return response.json()["error"]


def test_another_tenants_decision_id_answers_exactly_like_an_unknown_one(client, decision):
    stolen = client.get(f"/v1/decisions/{decision}", headers=bearer(TOKEN_B))
    invented = client.get(f"/v1/decisions/{UNKNOWN_ID}", headers=bearer(TOKEN_B))

    assert stolen.status_code == invented.status_code == 404
    stolen_body, invented_body = _error(stolen), _error(invented)
    assert stolen_body["code"] == invented_body["code"] == "NOT_FOUND"
    assert stolen_body["message"] == invented_body["message"]
    assert stolen_body["reason_codes"] == invented_body["reason_codes"]
    # the correlation id is the ONLY field allowed to differ, and it is not derived from the id
    assert set(stolen_body) == set(invented_body)
    assert stolen_body["correlation_id"] != invented_body["correlation_id"]
    assert decision not in stolen.text


@pytest.mark.parametrize("template", ["/v1/decisions/{}", "/v1/decisions/{}/replay"])
def test_cross_tenant_reads_are_not_found_on_every_addressable_route(client, decision, template):
    method = client.post if template.endswith("replay") else client.get
    response = method(template.format(decision), headers=bearer(TOKEN_B))

    assert response.status_code == 404
    assert _error(response)["code"] == "NOT_FOUND"


def test_cross_tenant_execution_never_reaches_the_gate(client, decision, pipelines):
    from tests.fixtures import TENANT_B

    response = client.post(f"/v1/decisions/{decision}/execute", headers=bearer(TOKEN_B))

    assert response.status_code in {403, 404, 409}
    assert pipelines[TENANT_B].broker.submitted == []
    assert decision not in response.text


def test_a_tenants_listing_shows_only_its_own_decisions(client, decision):
    mine = client.get("/v1/decisions", headers=bearer(TOKEN_A)).json()["decisions"]
    theirs = client.get("/v1/decisions", headers=bearer(TOKEN_B)).json()["decisions"]

    assert [d["decision_id"] for d in mine] == [decision]
    assert theirs == []


def test_an_error_body_carries_a_code_a_message_and_a_correlation_id_and_nothing_else(client):
    response = client.get(f"/v1/decisions/{UNKNOWN_ID}", headers=bearer(TOKEN_A))

    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "correlation_id", "reason_codes"}
    assert isinstance(body["error"]["correlation_id"], str) and body["error"]["correlation_id"]


@pytest.mark.parametrize(
    "leak",
    ["Traceback", "pydantic", "sqlite", "File \"", ".py", "self.", "SELECT ", "decision_records"],
)
def test_no_error_body_leaks_an_internal(client, leak):
    """F-14: whatever went wrong, the client learns a code and an id."""
    responses = [
        client.get(f"/v1/decisions/{UNKNOWN_ID}", headers=bearer(TOKEN_A)),
        client.get("/v1/decisions?limit=99999", headers=bearer(TOKEN_A)),
        client.post("/v1/proposals/evaluate", json={"nonsense": True}, headers=bearer(TOKEN_A)),
        client.post("/v1/proposals/evaluate", content=b"{not json", headers=bearer(TOKEN_A)),
        client.post("/v1/control/kill-switch", json={"active": "yes"}, headers=bearer(TOKEN_A)),
        client.get("/v1/decisions", headers={"Authorization": "Bearer nope-nope-nope-nope"}),
    ]

    for response in responses:
        assert response.status_code >= 400, response.text
        assert leak not in response.text, (response.status_code, response.text)


def test_an_unknown_field_is_rejected_rather_than_ignored(client):
    """The contract models are ``extra="forbid"`` and the API keeps them that way."""
    body = proposal_body()
    body["max_slippage_bps"] = 25

    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))

    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_FAILED"
    assert "max_slippage_bps" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"legs": []},
        {"symbol": "not a symbol"},
        {"legs": [{"leg_index": 0, "side": "buy", "quantity": 10, "order_type": "market"}]},
        {"expires_at": "2020-01-01T00:00:00.000000Z"},
    ],
)
def test_a_malformed_proposal_is_a_generic_422(client, body):
    payload = {**proposal_body(), **body}

    response = client.post("/v1/proposals/evaluate", json=payload, headers=bearer(TOKEN_A))

    assert response.status_code == 422
    assert _error(response)["message"] == "The proposal is not valid."


def test_a_json_number_where_a_decimal_string_belongs_is_refused(client):
    """Hard Rule A6 reaches the wire: a JSON number for a quantity never becomes a float."""
    body = proposal_body()
    body["legs"][0]["quantity"] = 10.5

    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))

    assert response.status_code == 422


def test_a_resolver_that_returns_another_tenants_pipeline_is_refused(build_client, pipelines):
    """A wiring bug must not become a silent cross-tenant read."""
    from tests.fixtures import TENANT_A

    wrong = build_client(resolver=lambda _tenant_id: pipelines[TENANT_A])

    response = wrong.get("/v1/decisions", headers=bearer(TOKEN_B))

    assert response.status_code in {403, 404, 500}
    assert "tenant-a" not in response.text


def test_a_resolver_with_no_pipeline_for_the_tenant_refuses(build_client):
    empty = build_client(resolver=lambda _tenant_id: None)

    response = empty.get("/v1/decisions", headers=bearer(TOKEN_A))

    assert response.status_code >= 400
    assert json.loads(response.text)["error"]["code"] in {
        "TENANT_FORBIDDEN",
        "CONFIGURATION_ERROR",
        "NOT_FOUND",
        "ENGINE_ERROR",
    }
