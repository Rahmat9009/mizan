"""Transport hardening: headers on every response, a body ceiling, and CORS that cannot be widened.

The pure helpers in ``mizan.api.hardening`` are tested directly as well as through the app, because the
policy they encode is worth reading on its own — and because a header that is only asserted on a happy
path is a header that is missing from the 401.
"""

from __future__ import annotations

import pytest

from mizan.api import ApiConfig
from mizan.api.hardening import (
    MAX_BODY_BYTES,
    SECURITY_HEADERS,
    merge_security_headers,
    over_limit,
    too_large_payload,
)
from mizan.contracts.errors import ConfigurationError
from tests.api.conftest import TOKEN_A, bearer, proposal_body

REQUIRED = (
    "x-content-type-options",
    "x-frame-options",
    "strict-transport-security",
    "content-security-policy",
    "referrer-policy",
    "cache-control",
)


# --------------------------------------------------------------------------------------------------
# the policy, on its own
# --------------------------------------------------------------------------------------------------
def test_the_header_set_says_the_things_an_api_must_say():
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert "max-age=" in SECURITY_HEADERS["Strict-Transport-Security"]
    assert "includeSubDomains" in SECURITY_HEADERS["Strict-Transport-Security"]
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "*" not in csp
    assert SECURITY_HEADERS["Cache-Control"] == "no-store"


def test_merging_replaces_an_application_supplied_header_rather_than_duplicating_it():
    merged = merge_security_headers(
        [(b"content-type", b"application/json"), (b"X-Frame-Options", b"ALLOWALL")]
    )
    names = [name.lower() for name, _ in merged]

    assert names.count(b"x-frame-options") == 1
    assert (b"X-Frame-Options", b"DENY") in merged
    assert (b"content-type", b"application/json") in merged


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (None, False),
        (b"0", False),
        (b"100", False),
        (str(MAX_BODY_BYTES).encode(), False),
        (str(MAX_BODY_BYTES + 1).encode(), True),
        (b"not-a-number", False),
        (b"  99999999  ", True),
    ],
)
def test_a_declared_content_length_is_judged_before_a_byte_is_read(declared, expected):
    assert over_limit(declared) is expected


def test_the_refusal_body_names_the_limit_and_nothing_about_the_request():
    body = too_large_payload(max_bytes=1234)["error"]

    assert body["code"] == "VALIDATION_FAILED"
    assert "1234" in body["message"]
    assert body["reason_codes"] == []
    assert body["correlation_id"]


# --------------------------------------------------------------------------------------------------
# through the app
# --------------------------------------------------------------------------------------------------
def test_every_response_carries_the_security_headers(client):
    responses = [
        client.get("/v1/health"),
        client.get("/v1/decisions", headers=bearer(TOKEN_A)),
        client.get("/v1/decisions", headers={"Authorization": "Bearer wrong-wrong-wrong-wrong"}),
        client.get("/v1/decisions/01a00000-0000-7000-8000-000000000000", headers=bearer(TOKEN_A)),
        client.post("/v1/proposals/evaluate", json={"nope": 1}, headers=bearer(TOKEN_A)),
        client.get("/v1/no-such-route"),
    ]

    for response in responses:
        for header in REQUIRED:
            assert header in response.headers, (response.status_code, header)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"


def test_an_oversized_body_is_refused_before_the_engine_runs(client, pipelines):
    from tests.fixtures import TENANT_A

    body = proposal_body()
    body["reasoning"] = "x" * (MAX_BODY_BYTES + 1000)

    response = client.post("/v1/proposals/evaluate", json=body, headers=bearer(TOKEN_A))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert str(MAX_BODY_BYTES) in response.json()["error"]["message"]
    assert "x-content-type-options" in response.headers
    # nothing was evaluated, so nothing was chained
    assert pipelines[TENANT_A].list_decisions() == []


def test_the_ceiling_is_configurable_and_still_enforced(build_client):
    client = build_client(config=ApiConfig(max_body_bytes=512))

    small = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A))

    assert small.status_code == 413
    assert "512" in small.json()["error"]["message"]


def test_a_body_at_the_limit_is_still_served(client):
    """The ceiling is a ceiling, not an off-by-one that refuses ordinary traffic."""
    response = client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A))

    assert response.status_code == 200


@pytest.mark.parametrize("origin", ["*", " * ", "null", "", "   "])
def test_a_wildcard_cors_origin_is_refused_at_configuration_time(origin):
    with pytest.raises(ConfigurationError):
        ApiConfig(cors_origins=(origin,))


def test_credentialed_cors_without_an_explicit_origin_list_is_refused():
    with pytest.raises(ConfigurationError):
        ApiConfig(cors_allow_credentials=True)


def test_an_explicit_origin_is_allowed_and_others_are_not(build_client):
    client = build_client(
        config=ApiConfig(cors_origins=("https://console.example.test",), cors_allow_credentials=True)
    )

    allowed = client.get(
        "/v1/health", headers={"Origin": "https://console.example.test"}
    )
    denied = client.get("/v1/health", headers={"Origin": "https://evil.example.test"})

    assert allowed.headers.get("access-control-allow-origin") == "https://console.example.test"
    assert denied.headers.get("access-control-allow-origin") is None


def test_no_cors_configuration_means_no_cors_header_at_all(client):
    response = client.get("/v1/health", headers={"Origin": "https://anything.example.test"})

    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_evaluation_is_rate_limited(build_client, pipelines):
    from mizan.api.ratelimit import RateLimit

    client = build_client(config=ApiConfig(evaluate_rate_limit=RateLimit(max_requests=2, window_seconds=60)))
    statuses = [
        client.post("/v1/proposals/evaluate", json=proposal_body(), headers=bearer(TOKEN_A)).status_code
        for _ in range(4)
    ]

    assert statuses[:2] == [200, 200]
    assert statuses[2:] == [429, 429]
    # a throttled call is a call that never reached the engine
    from tests.fixtures import TENANT_A

    assert len(pipelines[TENANT_A].list_decisions()) == 2


def test_the_interactive_docs_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
