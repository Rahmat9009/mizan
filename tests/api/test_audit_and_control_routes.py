"""The routes that exist so a human can check the machine: decision replay, chain verification, control.

These are the ones a regulator or an operator reaches for, and each has a property the happy path alone
would not prove: a decision replay through HTTP must reproduce the *recorded* verdict hash, chain
verification must walk what is stored rather than what is remembered, and flipping the kill switch must
leave a hash-chained trace of who flipped it (Addendum 1 section B.6, R-GRAD-2).
"""

from __future__ import annotations

from tests.api.conftest import TOKEN_A, TOKEN_READONLY, bearer, proposal_body
from tests.fixtures import TENANT_A


def evaluate(client, **overrides) -> dict:
    response = client.post(
        "/v1/proposals/evaluate", json=proposal_body(**overrides), headers=bearer(TOKEN_A)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_decision_replays_through_http_to_the_recorded_verdict(client, pipelines):
    decision = evaluate(client)
    recorded = pipelines[TENANT_A].get_decision(decision["decision_id"])

    response = client.post(
        f"/v1/decisions/{decision['decision_id']}/replay", headers=bearer(TOKEN_A)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "exact"
    assert body["identical"] is True
    assert body["replayed_verdict"] == recorded.verdict
    assert body["replayed_verdict_hash"] == recorded.governor_decision.verdict_hash


def test_chain_verification_reports_the_whole_stored_chain(client):
    empty = client.get("/v1/audit/verify", headers=bearer(TOKEN_A)).json()
    assert empty["ok"] is True
    assert empty["length"] == 0

    evaluate(client)
    evaluate(client, symbol="MSFT", legs=[
        {
            "leg_index": 0,
            "side": "buy",
            "contract_type": None,
            "strike": None,
            "expiry": None,
            "quantity": "3",
            "limit_price": "412.10",
            "order_type": "limit",
        }
    ])

    verified = client.get("/v1/audit/verify", headers=bearer(TOKEN_A)).json()

    assert verified["ok"] is True
    assert verified["length"] == 2
    assert verified["first_bad_sequence"] is None


def test_the_decision_listing_pages_newest_first_with_a_cursor(client):
    first = evaluate(client)["decision_id"]
    second = evaluate(client, reasoning="a second, differently reasoned order")["decision_id"]

    page = client.get("/v1/decisions?limit=1", headers=bearer(TOKEN_A)).json()["decisions"]
    assert [d["decision_id"] for d in page] == [second]

    cursor = page[0]["sequence"]
    older = client.get(
        f"/v1/decisions?limit=10&before_sequence={cursor}", headers=bearer(TOKEN_A)
    ).json()["decisions"]
    assert [d["decision_id"] for d in older] == [first]


def test_an_out_of_range_page_size_is_refused(client):
    for limit in (0, -1, 201, 100000):
        response = client.get(f"/v1/decisions?limit={limit}", headers=bearer(TOKEN_A))
        assert response.status_code == 422, limit


def test_flipping_the_kill_switch_leaves_a_hash_chained_trace(client, pipelines):
    pipeline = pipelines[TENANT_A]
    assert pipeline.kill_switch.is_active() is False

    activated = client.post("/v1/control/kill-switch", json={"active": True}, headers=bearer(TOKEN_A))

    assert activated.status_code == 200
    assert activated.json() == {"kill_switch": {"active": True}}
    assert pipeline.kill_switch.is_active() is True

    ledger = pipeline.ledger.for_tenant(TENANT_A)
    events = ledger.list_control_events(limit=10)
    assert [event.event_type for event in events] == ["kill_switch_activated"]
    assert events[0].actor.type == "human"
    assert ledger.verify_chain().ok is True


def test_deactivating_records_a_human_actor_and_keeps_the_chain_verifiable(client, pipelines):
    client.post("/v1/control/kill-switch", json={"active": True}, headers=bearer(TOKEN_A))
    response = client.post("/v1/control/kill-switch", json={"active": False}, headers=bearer(TOKEN_A))

    assert response.status_code == 200
    assert pipelines[TENANT_A].kill_switch.is_active() is False
    ledger = pipelines[TENANT_A].ledger.for_tenant(TENANT_A)
    events = [event.event_type for event in ledger.list_control_events(limit=10)]
    assert "kill_switch_deactivated" in events
    assert ledger.verify_chain().ok is True


def test_the_control_route_needs_the_control_scope_not_merely_a_valid_token(client, pipelines):
    response = client.post(
        "/v1/control/kill-switch", json={"active": True}, headers=bearer(TOKEN_READONLY)
    )

    assert response.status_code == 403
    assert pipelines[TENANT_A].kill_switch.is_active() is False


def test_the_control_route_refuses_a_body_that_does_not_say_which_way(client, pipelines):
    for body in ({}, {"active": "true"}, {"active": 1}, {"active": None}):
        response = client.post("/v1/control/kill-switch", json=body, headers=bearer(TOKEN_A))
        assert response.status_code == 422, body
    assert pipelines[TENANT_A].kill_switch.is_active() is False


def test_the_decision_detail_route_serves_the_whole_recorded_state_to_its_owner(client, pipelines):
    decision = evaluate(client)

    response = client.get(f"/v1/decisions/{decision['decision_id']}", headers=bearer(TOKEN_A))

    assert response.status_code == 200
    body = response.json()
    assert body["decision_id"] == decision["decision_id"]
    assert body["tenant_id"] == TENANT_A
    assert body["risk_evaluation"]["evaluation_id"]
    assert body["audit_hash"] == pipelines[TENANT_A].get_decision(decision["decision_id"]).audit_hash
