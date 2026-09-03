"""Two tenants sharing one deployment, and every route across the boundary refused.

Two arrangements are exercised, because a boundary that only holds in one of them is not a boundary:

* **one ledger object, two tenants** — the arrangement a cross-tenant read would have to exploit;
* **one SQLite root directory, two tenants** — where isolation is a file boundary, and the test can
  therefore assert the stronger fact that the rows are not merely filtered but not present.

Both the SDK and the ``/v1`` HTTP surface are driven, with real bearer tokens and a real ASGI client.
The answer to another tenant's decision id is 404 and not 403: "you may not see this" and "this does
not exist" have to be indistinguishable, or the status code enumerates other tenants' decisions.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from mizan.api import ApiConfig, Principal, StaticTokenStore, create_app
from mizan.audit import InMemoryLedger, SqliteLedger
from mizan.contracts.errors import NotFound, ValidationFailed
from tests.integration._world import (
    AGENT_A,
    AGENT_B,
    NOW,
    TENANT_A,
    TENANT_B,
    build_world,
    proposal,
)

ALL_SCOPES = frozenset({"read", "evaluate", "execute", "control"})
# Fixtures, not secrets: the store keeps SHA-256 digests and these strings are inert.
TOKEN_A = "integration-token-tenant-a-0001"  # secret-scan: allow
TOKEN_B = "integration-token-tenant-b-0002"  # secret-scan: allow


def _two_tenants(tmp_path, *, on_disk: bool):
    ledger = SqliteLedger(root_dir=tmp_path) if on_disk else InMemoryLedger()
    a = build_world(tenant_id=TENANT_A, agent=AGENT_A, ledger=ledger, dry_run=True)
    b = build_world(tenant_id=TENANT_B, agent=AGENT_B, ledger=ledger, dry_run=True)
    return a, b


def test_one_tenant_cannot_read_another_tenants_decision_through_the_sdk(tmp_path):
    a, b = _two_tenants(tmp_path, on_disk=False)
    record = a.mizan.evaluate(proposal("10"))

    with pytest.raises(NotFound):
        b.mizan.get_decision(record.decision_id)

    assert b.mizan.list_decisions(limit=50) == []
    assert [r.decision_id for r in a.mizan.list_decisions(limit=50)] == [record.decision_id]
    assert a.mizan.verify_chain().length == 1
    assert b.mizan.verify_chain().length == 0


def test_an_unknown_id_and_another_tenants_id_are_indistinguishable(tmp_path):
    """The failure must not be an oracle: same exception type, same code, same message."""
    a, b = _two_tenants(tmp_path, on_disk=False)
    record = a.mizan.evaluate(proposal("10"))

    with pytest.raises(NotFound) as foreign:
        b.mizan.get_decision(record.decision_id)
    with pytest.raises(NotFound) as invented:
        b.mizan.get_decision("01a00000-0000-7000-8000-000000000000")

    assert type(foreign.value) is type(invented.value)
    assert foreign.value.code == invented.value.code
    assert str(foreign.value).split("(correlation_id")[0] == str(invented.value).split("(correlation_id")[0]


def test_replay_and_execute_are_refused_across_the_boundary_too(tmp_path):
    """Reads are not the only route in: a decision id is also an execute and a replay argument."""
    a, b = _two_tenants(tmp_path, on_disk=False)
    record = a.mizan.evaluate(proposal("10"))
    assert record.authorization is not None

    with pytest.raises(NotFound):
        b.mizan.replay(record.decision_id)
    with pytest.raises(NotFound):
        b.mizan.execute(record.decision_id)
    with pytest.raises(NotFound):
        b.mizan.get_execution(record.decision_id)

    assert b.broker.submitted == []
    assert a.broker.submitted == []


def test_an_agent_cannot_propose_as_another_agent(tmp_path):
    """Identity comes from the instance, never from the payload."""
    a, _b = _two_tenants(tmp_path, on_disk=False)

    with pytest.raises(ValidationFailed):
        a.mizan.evaluate(proposal("10", agent=AGENT_B))

    assert a.mizan.list_decisions(limit=50) == [], "an impersonation attempt is refused, not governed"


def test_on_disk_the_other_tenants_rows_are_not_merely_filtered_they_are_absent(tmp_path):
    """The strongest form of the claim: a query cannot reach rows in a different database file."""
    a, b = _two_tenants(tmp_path, on_disk=True)
    record_a = a.mizan.evaluate(proposal("10"))
    record_b = b.mizan.evaluate(proposal("10", agent=AGENT_B))

    root = SqliteLedger(root_dir=tmp_path)
    assert root.path_for(TENANT_A) != root.path_for(TENANT_B)
    assert root.path_for(TENANT_A).exists() and root.path_for(TENANT_B).exists()

    with sqlite3.connect(root.path_for(TENANT_B)) as connection:
        rows = connection.execute(
            "SELECT sequence, tenant_id, record_json FROM decision_records"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == TENANT_B
    stored = json.loads(rows[0][2])
    assert stored["decision_id"] == record_b.decision_id
    assert record_a.decision_id not in rows[0][2], "tenant A's id appears in tenant B's database file"

    # both chains start from ZERO_HASH and are sequenced independently
    assert record_a.sequence == record_b.sequence == 1
    assert record_a.audit_prev_hash == record_b.audit_prev_hash == "0" * 64
    assert record_a.audit_hash != record_b.audit_hash


# ------------------------------------------------------------------------------------------------
# The same boundary over HTTP, through the real /v1 surface
# ------------------------------------------------------------------------------------------------


def _client(tmp_path):
    a, b = _two_tenants(tmp_path, on_disk=True)
    pipelines = {TENANT_A: a.mizan, TENANT_B: b.mizan}
    tokens = StaticTokenStore(
        {
            TOKEN_A: Principal(token_id="a", tenant_id=TENANT_A, agent=AGENT_A, scopes=ALL_SCOPES),
            TOKEN_B: Principal(token_id="b", tenant_id=TENANT_B, agent=AGENT_B, scopes=ALL_SCOPES),
        }
    )
    app = create_app(pipelines.get, tokens=tokens, config=ApiConfig())
    return TestClient(app), a, b


def _body(quantity: str) -> dict:
    payload = proposal(quantity).model_dump(mode="json")
    payload.pop("proposal_id")
    payload.pop("agent")
    return payload


def test_the_v1_surface_refuses_a_cross_tenant_read_with_404_not_403(tmp_path):
    client, _a, _b = _client(tmp_path)

    created = client.post(
        "/v1/proposals/evaluate",
        json=_body("10"),
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert created.status_code == 200, created.text
    decision_id = created.json()["decision_id"]

    mine = client.get(f"/v1/decisions/{decision_id}", headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert mine.status_code == 200

    theirs = client.get(f"/v1/decisions/{decision_id}", headers={"Authorization": f"Bearer {TOKEN_B}"})
    invented = client.get(
        "/v1/decisions/01a00000-0000-7000-8000-000000000000",
        headers={"Authorization": f"Bearer {TOKEN_B}"},
    )

    assert theirs.status_code == 404, theirs.text
    assert theirs.json()["error"]["code"] == invented.json()["error"]["code"]
    assert theirs.json()["error"]["message"] == invented.json()["error"]["message"]

    listed = client.get("/v1/decisions", headers={"Authorization": f"Bearer {TOKEN_B}"})
    assert listed.status_code == 200
    assert listed.json()["decisions"] == []


def test_the_v1_surface_refuses_a_cross_tenant_execute_and_replay(tmp_path):
    client, a, b = _client(tmp_path)
    created = client.post(
        "/v1/proposals/evaluate", json=_body("10"), headers={"Authorization": f"Bearer {TOKEN_A}"}
    )
    decision_id = created.json()["decision_id"]

    for path in (f"/v1/decisions/{decision_id}/execute", f"/v1/decisions/{decision_id}/replay"):
        response = client.post(path, headers={"Authorization": f"Bearer {TOKEN_B}"})
        assert response.status_code == 404, (path, response.text)

    assert a.broker.submitted == [] and b.broker.submitted == []


def test_an_unauthenticated_caller_reaches_nothing_and_the_body_never_chooses_the_agent(tmp_path):
    client, _a, _b = _client(tmp_path)

    assert client.get("/v1/decisions").status_code == 403
    assert client.post("/v1/proposals/evaluate", json=_body("10")).status_code == 403

    impersonation = dict(_body("10"))
    impersonation["agent"] = AGENT_B.model_dump(mode="json")
    refused = client.post(
        "/v1/proposals/evaluate", json=impersonation, headers={"Authorization": f"Bearer {TOKEN_A}"}
    )
    assert refused.status_code == 403, refused.text
    assert AGENT_B.agent_id not in refused.text


def test_a_kill_switch_flip_is_scoped_to_the_tenant_that_flipped_it(tmp_path):
    """Stopping one tenant must not stop, or fail to stop, another."""
    client, a, b = _client(tmp_path)

    flipped = client.post(
        "/v1/control/kill-switch", json={"active": True}, headers={"Authorization": f"Bearer {TOKEN_A}"}
    )
    assert flipped.status_code == 200 and flipped.json()["kill_switch"]["active"] is True

    assert a.mizan.kill_switch.is_active() is True
    assert b.mizan.kill_switch.is_active() is False, "one tenant's stop must not stop another"

    # and the flip is itself a chained, per-tenant control event
    events = a.mizan.ledger.for_tenant(TENANT_A).list_control_events(limit=10)
    assert [event.event_type for event in events] == ["kill_switch_activated"]
    assert events[0].recorded_at.startswith("2026-09-02T17:40")
    assert a.mizan.verify_chain().ok is True
    assert b.mizan.ledger.for_tenant(TENANT_B).list_control_events(limit=10) == []
    assert NOW.year == 2026  # the clock under all of this is the fixed one, not a wall clock
