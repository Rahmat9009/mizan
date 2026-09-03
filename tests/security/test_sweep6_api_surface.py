"""L5 Sweep 6.9 and Sweep 7.1 — the ``/v1`` surface and the console, attacked.

Answers the legacy findings the new surface exists to not repeat:

* **F-3** (no authentication anywhere, and agent identity taken from the body) - every ``/v1`` route
  refuses an anonymous caller, refuses a token without the scope, and refuses a body that names
  another agent;
* **F-1/F-2** (caller-supplied valuation) - the evaluate route accepts a proposal and nothing else;
  a market or portfolio snapshot in the body is a validation error, not an input;
* **F-14** (internal exception text forwarded) - every error body is a code, a generic sentence and
  a correlation id;
* **F-15** (health disclosing control-plane state) - anonymous callers get liveness only;
* **F-17** (no tenant concept) - another tenant's decision id is ``NotFound``, never ``Forbidden``;
* **F-8** (stored XSS through LLM-authored text) - the console escapes, never normalises, and has no
  unsafe escape hatch.

Finding raised here: F-34 (anonymous ``/v1/health`` probes consume the authentication-attempt
budget for the caller's address).

Self-contained by design (ESC-3).
"""

from __future__ import annotations

from typing import Any

import pytest

from mizan.adapters import MockBroker
from mizan.api import ROUTES, ApiConfig, Principal, StaticTokenStore, create_app, token_digest
from mizan.api.hardening import MAX_BODY_BYTES, SECURITY_HEADERS
from mizan.api.ratelimit import RateLimit
from mizan.audit import InMemoryLedger
from mizan.console.escaping import (
    BLOCKED_URL,
    FORBIDDEN_TAGS,
    el,
    escape_attr,
    escape_text,
    render,
    safe_url,
    taint_flags,
)
from mizan.execution import ExecutionConfig
from mizan.sdk import Mizan
from tests.fixtures import (
    FIXED_NOW,
    injection_reasoning,
    make_agent,
    make_market_snapshot,
    make_policy,
    make_portfolio_snapshot,
    make_proposal,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.security

TOKEN_A = "token-a-aaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "token-b-bbbbbbbbbbbbbbbbbbbbbb"
TOKEN_NO_SCOPE = "token-noscope-cccccccccccccc"
FULL_SCOPES = frozenset({"read", "evaluate", "execute", "control"})


def a_pipeline(policy: Any) -> Mizan:
    return Mizan(
        tenant_id=policy.tenant_id,
        agent=make_agent(),
        policy=policy,
        broker=MockBroker(
            portfolio_snapshot=make_portfolio_snapshot(), market_snapshot=make_market_snapshot()
        ),
        ledger=InMemoryLedger(),
        config=ExecutionConfig(enabled=True, dry_run=True),
        clock=lambda: FIXED_NOW,
    )


def a_client(config: ApiConfig | None = None) -> tuple[TestClient, dict[str, Mizan]]:
    policy_a, policy_b = make_policy(), make_policy(tenant_id="tenant-b")
    pipelines = {policy_a.tenant_id: a_pipeline(policy_a), policy_b.tenant_id: a_pipeline(policy_b)}
    store = StaticTokenStore(
        {
            TOKEN_A: Principal(
                token_id="a", tenant_id=policy_a.tenant_id, agent=make_agent(), scopes=FULL_SCOPES
            ),
            TOKEN_B: Principal(
                token_id="b", tenant_id=policy_b.tenant_id, agent=make_agent(), scopes=FULL_SCOPES
            ),
            TOKEN_NO_SCOPE: Principal(
                token_id="n", tenant_id=policy_a.tenant_id, agent=make_agent(), scopes=frozenset()
            ),
        }
    )
    app = create_app(lambda tenant: pipelines.get(tenant), tokens=store, config=config or ApiConfig())
    return TestClient(app, raise_server_exceptions=False), pipelines


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def proposal_body() -> dict[str, Any]:
    body = make_proposal().model_dump(mode="json")
    body.pop("proposal_id")
    body.pop("schema_version", None)
    return body


# =============================================================================================
# Authentication and authorization (F-3)
# =============================================================================================
@pytest.mark.parametrize(("method", "path"), [route for route in ROUTES if route[1] != "/v1/health"])
def test_no_v1_route_is_reachable_without_a_credential(method: str, path: str) -> None:
    client, _ = a_client()
    response = client.request(method, path.replace("{decision_id}", "anything"), json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_FORBIDDEN"


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": f"bearer {TOKEN_A}x"},
        {"Authorization": TOKEN_A},
        {"Authorization": f"Bearer {TOKEN_A} extra"},
    ],
)
def test_a_malformed_or_wrong_credential_is_refused_identically(header: dict[str, str]) -> None:
    """One message for every failure mode: telling them apart tells an attacker which guess was real."""
    client, _ = a_client()
    response = client.get("/v1/policy", headers=header)
    assert response.status_code in {403, 429}
    body = response.json()["error"]
    assert "unknown" not in body["message"].lower()
    assert "expired" not in body["message"].lower()


def test_a_token_without_the_scope_cannot_execute_or_control() -> None:
    client, _ = a_client()
    for method, path in (
        ("POST", "/v1/proposals/evaluate"),
        ("POST", "/v1/decisions/x/execute"),
        ("POST", "/v1/control/kill-switch"),
        ("GET", "/v1/policy"),
    ):
        response = client.request(method, path, json={"active": True}, headers=bearer(TOKEN_NO_SCOPE))
        assert response.status_code == 403, f"{method} {path} was reachable without a scope"


def test_the_token_itself_is_never_stored() -> None:
    store = StaticTokenStore(
        {TOKEN_A: Principal(token_id="a", tenant_id="tenant-a", agent=make_agent(), scopes=FULL_SCOPES)}
    )
    stored = str(store.__dict__)
    assert TOKEN_A not in stored
    assert token_digest(TOKEN_A) in stored


def test_a_short_token_is_refused_at_registration() -> None:
    with pytest.raises(ValueError):
        StaticTokenStore().add("short", Principal(token_id="a", tenant_id="t", agent=make_agent()))


# =============================================================================================
# Tenant isolation (F-17) and agent identity (F-3)
# =============================================================================================
def test_another_tenants_decision_is_not_found_never_forbidden() -> None:
    client, pipelines = a_client()
    record = pipelines["tenant-a"].evaluate(make_proposal())
    own = client.get(f"/v1/decisions/{record.decision_id}", headers=bearer(TOKEN_A))
    assert own.status_code == 200
    other = client.get(f"/v1/decisions/{record.decision_id}", headers=bearer(TOKEN_B))
    assert other.status_code == 404, "existence must not leak across the tenant boundary"
    assert other.json()["error"]["code"] == "NOT_FOUND"
    execute = client.post(f"/v1/decisions/{record.decision_id}/execute", headers=bearer(TOKEN_B))
    assert execute.status_code == 404
    replay = client.post(f"/v1/decisions/{record.decision_id}/replay", headers=bearer(TOKEN_B))
    assert replay.status_code == 404


def test_a_body_naming_another_agent_is_refused_outright() -> None:
    client, _ = a_client()
    body = proposal_body()
    body["agent"] = {**body["agent"], "agent_id": "agent-someone-else"}
    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))
    assert response.status_code == 403
    assert response.json()["error"]["reason_codes"] == ["TENANT_ACCESS_DENIED"]


def test_the_body_cannot_choose_the_proposal_id() -> None:
    client, _ = a_client()
    honest = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A))
    forged = client.post(
        "/v1/proposals/evaluate",
        json={**proposal_body(), "proposal_id": "0" * 64},
        headers=bearer(TOKEN_A),
    )
    assert honest.status_code == forged.status_code == 200
    assert forged.json()["proposal_id"] == honest.json()["proposal_id"] != "0" * 64


# =============================================================================================
# The caller never supplies valuation (F-1 / F-2)
# =============================================================================================
@pytest.mark.parametrize(
    "field",
    ["market_snapshot", "portfolio_snapshot", "estimated_price", "market_risk", "buying_power",
     "tenant_id", "policy", "policy_hash"],
)
def test_the_evaluate_route_accepts_no_valuation_or_authority_field(field: str) -> None:
    client, _ = a_client()
    body = {**proposal_body(), field: "1"}
    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# =============================================================================================
# Disclosure (F-14 / F-15) and hardening
# =============================================================================================
def test_no_error_body_carries_internal_detail() -> None:
    client, _ = a_client()
    responses = [
        client.get("/v1/decisions/x"),
        client.get("/v1/decisions/x", headers=bearer(TOKEN_A)),
        client.post("/v1/proposals/evaluate", json={"bad": 1}, headers=bearer(TOKEN_A)),
        client.post("/v1/control/kill-switch", json={"active": "yes"}, headers=bearer(TOKEN_A)),
        client.get("/v1/decisions?limit=9999", headers=bearer(TOKEN_A)),
    ]
    for response in responses:
        body = response.text
        assert set(response.json()["error"]) == {"code", "message", "correlation_id", "reason_codes"}
        for leak in ("Traceback", ".py", "sqlite", "SQLite", "pydantic", "site-packages", "ValidationError"):
            assert leak not in body, f"{leak!r} leaked in {body[:200]}"


def test_health_discloses_nothing_to_an_anonymous_caller() -> None:
    client, _ = a_client()
    anonymous = client.get("/v1/health").json()
    assert anonymous == {"status": "ok"}
    authenticated = client.get("/v1/health", headers=bearer(TOKEN_A)).json()
    assert authenticated["environment"] == "paper"
    assert "execution" in authenticated


def test_every_response_carries_the_security_headers() -> None:
    client, _ = a_client()
    for response in (
        client.get("/v1/health"),
        client.get("/v1/decisions/x"),
        client.get("/v1/policy", headers=bearer(TOKEN_A)),
        client.get("/no-such-route"),
    ):
        missing = [name for name in SECURITY_HEADERS if name not in response.headers]
        assert missing == [], f"{response.status_code} response missing {missing}"
        assert response.headers["Cache-Control"] == "no-store"


def test_an_oversized_body_is_refused_before_the_router_sees_it() -> None:
    client, _ = a_client()
    response = client.post(
        "/v1/proposals/evaluate",
        json={"reasoning": "x" * (MAX_BODY_BYTES + 1024)},
        headers=bearer(TOKEN_A),
    )
    assert response.status_code == 413
    body = response.json()["error"]
    assert str(MAX_BODY_BYTES) in body["message"]
    assert "x" * 100 not in response.text


@pytest.mark.parametrize("depth", [500, 5_000, 20_000])
def test_a_deeply_nested_body_is_refused_without_a_traceback(depth: int) -> None:
    client, _ = a_client()
    raw = ('{"a":' * depth) + "null" + ("}" * depth)
    response = client.post(
        "/v1/proposals/evaluate",
        content=raw.encode(),
        headers={**bearer(TOKEN_A), "Content-Type": "application/json"},
    )
    assert response.status_code in {413, 422, 500}
    for leak in ("Traceback", "RecursionError", "recursion", ".py"):
        assert leak not in response.text


def test_a_wildcard_cors_origin_is_refused_at_configuration_time() -> None:
    from mizan.contracts.errors import ConfigurationError

    for origin in ("*", "null", " "):
        with pytest.raises(ConfigurationError):
            ApiConfig(cors_origins=(origin,))
    with pytest.raises(ConfigurationError):
        ApiConfig(cors_allow_credentials=True)


# ---------------------------------------------------------------------------------------------
# FINDING F-34 - anonymous /v1/health probes consume the authentication-attempt budget
# ---------------------------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=False,
    reason="F-34 OPEN (LOW, L3 api): GET /v1/health resolves the principal inside a try/except, so "
    "an anonymous probe charges the auth limiter for the caller's address. A load balancer "
    "polling health exhausts the brute-force counter for everyone behind that address, after "
    "which every genuine bad credential answers 429 instead of 403. Remove when F-34 is fixed.",
)
def test_f34_anonymous_health_probes_must_not_consume_the_auth_rate_limit() -> None:
    client, _ = a_client(ApiConfig(auth_rate_limit=RateLimit(max_requests=3, window_seconds=60)))
    for _ in range(5):
        assert client.get("/v1/health").status_code == 200
    refused = client.get("/v1/policy", headers=bearer("bogus-token-aaaaaaaaaaaaaaa"))
    assert refused.status_code == 403, (
        "health probes consumed the credential-guessing budget; a real bad token now gets 429"
    )


def test_f34_a_valid_credential_is_never_throttled_by_someone_elses_failures() -> None:
    """The half that holds, and the reason F-34 is LOW: a success consumes no budget."""
    client, _ = a_client(ApiConfig(auth_rate_limit=RateLimit(max_requests=2, window_seconds=60)))
    for _ in range(6):
        client.get("/v1/policy", headers=bearer("bogus-token-aaaaaaaaaaaaaaa"))
    assert client.get("/v1/policy", headers=bearer(TOKEN_A)).status_code == 200


# =============================================================================================
# Sweep 7.1 - the console (F-8)
# =============================================================================================
XSS_PAYLOADS = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "\"><svg/onload=alert(1)>",
    "javascript:alert(1)",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "＜script＞alert(1)＜/script＞",
    "</td></tr><script>alert(1)</script>",
    "`\"'><iframe src=javascript:alert(1)>",
    injection_reasoning(),
)


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_no_agent_authored_string_can_emit_markup(payload: str) -> None:
    text = escape_text(payload)
    for character in ("<", ">"):
        assert character not in text
    assert "&" not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "").replace(
        "&quot;", ""
    ).replace("&#x27;", "")
    rendered = render(el("p", payload))
    assert rendered.startswith("<p>") and rendered.endswith("</p>")
    assert "<script" not in rendered
    assert "onerror" not in rendered or "&" in rendered


def test_escaping_never_normalises_a_homoglyph_into_real_markup() -> None:
    """NFKC would MANUFACTURE a ``<`` out of a fullwidth one. It is flagged, never rewritten."""
    payload = "＜script＞alert(1)＜/script＞"
    assert "<" not in escape_text(payload)
    assert "normalises-to-markup" in taint_flags(payload)


def test_an_ampersand_is_escaped_first_so_a_payload_cannot_be_un_escaped() -> None:
    assert escape_text("&lt;script&gt;") == "&amp;lt;script&amp;gt;"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "java\tscript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//evil.invalid/steal",
    ],
)
def test_an_unsafe_url_never_reaches_an_attribute(url: str) -> None:
    assert safe_url(url) == BLOCKED_URL
    assert BLOCKED_URL in render(el("a", "click", href=url))


@pytest.mark.parametrize("tag", sorted(FORBIDDEN_TAGS))
def test_the_console_refuses_to_emit_an_executable_tag(tag: str) -> None:
    with pytest.raises(ValueError):
        el(tag, "payload")


@pytest.mark.parametrize("attribute", ["onclick", "onerror", "onload", "style", "srcdoc", "background"])
def test_the_console_refuses_an_event_handler_or_style_attribute(attribute: str) -> None:
    with pytest.raises(ValueError):
        el("div", "x", **{attribute: "alert(1)"})


def test_bidi_and_zero_width_controls_are_made_visible_rather_than_dropped() -> None:
    """U+202E reverses displayed text - one way free text is made to look like a verdict."""
    rendered = escape_text("REJECT‮EVORPPA")
    assert "[U+202E]" in rendered
    assert "‮" not in rendered
    assert "[U+200B]" in escape_text("a​b")


def test_an_attribute_value_escapes_the_backtick_too() -> None:
    assert "`" not in escape_attr("a`b")
