"""L5 security pins for the legacy recursive audit redaction.

Subject: ``app.audit._AuditEventFactory._sanitize`` (Rahmat's legacy governor).

Purpose
-------
``app/`` is a read-only salvage reference. These tests do two things:

1. PIN what the legacy redaction actually redacts (plain passing tests). The new
   core's ``mizan.contracts.canonical.redact`` must redact at least this set.
2. DOCUMENT what it misses, as ``xfail(strict=True)``. Each xfail is a gap that
   ``mizan.contracts.canonical.redact`` (L0) and ``TenantLedger.append`` (L2) MUST
   cover. ``strict=True`` means that if the legacy behaviour ever changes, the
   unexpected pass fails the suite and this file must be revisited.

Everything here is offline: no network, no broker, no LLM, no database.

Findings: security/findings.md F-6 (redaction gaps), F-7 (3 of 4 audit entry
points bypass ``_sanitize`` entirely).
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.audit import InMemoryAuditLog, _AuditEventFactory
from app.models import AIRiskAnalysis, Decision, GovernorDecision, RiskReport

SECRET = "value-that-must-never-persist"
REDACTED = "[REDACTED]"


def sanitize(payload):
    return _AuditEventFactory._sanitize(payload)


def is_redacted(key: str) -> bool:
    return sanitize({key: SECRET})[key] == REDACTED


# ---------------------------------------------------------------------------
# 1. What the legacy redaction DOES cover (baseline the new redact() must keep)
# ---------------------------------------------------------------------------

COVERED_KEYS = [
    # API keys, any case / separator
    "api_key",
    "apiKey",
    "API_KEY",
    "X-API-Key",
    "x-api-key",
    "api key",
    "api.key",
    "api/key",
    "ALPACA_API_KEY",
    "FEATHERLESS_API_KEY",
    "ANTHROPIC_API_KEY",
    # secrets
    "secret",
    "secret_key",
    "SecretKey",
    "client_secret",
    "ALPACA_SECRET_KEY",
    # authorization header, exact and suffixed variants
    "Authorization",
    "authorization",
    "AUTHORIZATION",
    " Authorization ",
    "authorizationHeader",
    "authorization_header",
    # tokens
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "x-auth-token",
    "next_token",
    "page_token",
    # passwords / credentials
    "password",
    "credential",
    "credentials",
    # header collections (redacted wholesale)
    "header",
    "headers",
    "request_headers",
    # zero-width characters are stripped by the [^a-z0-9] normaliser
    "api​_key",
]


@pytest.mark.parametrize("key", COVERED_KEYS, ids=lambda k: repr(k))
def test_legacy_redacts_key(key: str) -> None:
    assert is_redacted(key)


def test_legacy_redacts_inside_lists_of_dicts() -> None:
    out = sanitize({"outer": [{"api_key": SECRET}, {"x": {"password": SECRET}}]})
    assert out == {"outer": [{"api_key": REDACTED}, {"x": {"password": REDACTED}}]}


def test_legacy_redacts_inside_tuples_and_returns_lists() -> None:
    out = sanitize({"t": ({"token": SECRET}, "plain")})
    assert out == {"t": [{"token": REDACTED}, "plain"]}


def test_legacy_redacts_at_depth() -> None:
    out = sanitize({"a": {"b": {"c": {"d": {"e": {"api_key": SECRET}}}}}})
    assert out["a"]["b"]["c"]["d"]["e"]["api_key"] == REDACTED


def test_legacy_redacts_header_collection_wholesale() -> None:
    out = sanitize({"headers": {"Content-Type": "json", "Set-Cookie": SECRET}})
    assert out == {"headers": REDACTED}


def test_legacy_preserves_lookalike_business_keys() -> None:
    """Keys that merely contain a sensitive fragment must survive (no over-redaction)."""

    payload = {
        "authorization_created_at": "2026-09-02T00:00:00Z",
        "authorized_quantity": 5,
        "token_count": 12,
        "total_tokens": 180,
    }
    assert sanitize(payload) == payload


def test_legacy_stringifies_non_string_keys() -> None:
    out = sanitize({1: "a", None: "b", b"api_key": SECRET})
    assert out == {"1": "a", "None": "b", "b'api_key'": REDACTED}


def test_legacy_sanitized_payload_is_json_serialisable() -> None:
    out = sanitize({"headers": {"Authorization": SECRET}, "ok": [1, "two", {"three": 3}]})
    assert SECRET not in json.dumps(out)


# ---------------------------------------------------------------------------
# 2. What the legacy redaction MISSES (documented gaps; new redact() must cover)
# ---------------------------------------------------------------------------

def gap(key: str, reason: str):
    return pytest.param(key, id=repr(key), marks=pytest.mark.xfail(strict=True, reason=reason))


MISSED_KEYS = [
    # --- headers that carry credentials -------------------------------------
    gap("proxy-authorization", "legacy: 'authorization' is exact-match only, not a suffix; Proxy-Authorization survives"),
    gap("Proxy-Authorization", "legacy: mixed-case Proxy-Authorization survives"),
    gap("cookie", "legacy: cookie is not in the sensitive set"),
    gap("Cookie", "legacy: Cookie is not in the sensitive set"),
    gap("set-cookie", "legacy: set-cookie is not in the sensitive set"),
    gap("Set-Cookie", "legacy: Set-Cookie is not in the sensitive set"),
    gap("www-authenticate", "legacy: www-authenticate survives (may echo realm/challenge material)"),
    gap("bearer", "legacy: 'bearer' key survives"),
    # --- session / signing material ------------------------------------------
    gap("session", "legacy: session survives"),
    gap("session_id", "legacy: session_id survives"),
    gap("sessionid", "legacy: sessionid survives"),
    gap("jwt", "legacy: jwt survives"),
    gap("auth", "legacy: bare 'auth' survives"),
    gap("signature", "legacy: signature survives"),
    gap("x-signature", "legacy: x-signature survives"),
    gap("private_key", "legacy: private_key ends in 'key' but not 'apikey'; survives"),
    gap("privateKey", "legacy: privateKey survives"),
    gap("PRIVATE_KEY", "legacy: PRIVATE_KEY survives"),
    gap("passphrase", "legacy: passphrase survives"),
    # --- password aliases ----------------------------------------------------
    gap("passwd", "legacy: passwd survives (API-SURFACE SENSITIVE_KEY_PATTERNS lists passwd)"),
    gap("pwd", "legacy: pwd survives"),
    gap("pass", "legacy: pass survives"),
    # --- database / connection material --------------------------------------
    gap("connection_string", "legacy: connection_string survives (API-SURFACE lists connection_string)"),
    gap("connectionString", "legacy: connectionString survives"),
    gap("dsn", "legacy: dsn survives (API-SURFACE lists dsn)"),
    gap("DSN", "legacy: DSN survives"),
    gap("database_url", "legacy: database_url survives"),
    gap("db_url", "legacy: db_url survives"),
    # --- header collections not named exactly header/headers/requestheaders ---
    gap("response_headers", "legacy: only header/headers/request_headers are wholesale-redacted; response_headers is recursed, so Set-Cookie inside survives"),
    gap("http_headers", "legacy: http_headers is recursed, not wholesale-redacted"),
    # --- account identifiers (B3 / FINRA data-sensitivity; policy decision for L2) ---
    gap("account_id", "legacy: broker account identifiers are not redacted; new core must decide (tenant-scoped at minimum)"),
    gap("account_number", "legacy: account_number survives"),
    gap("accountId", "legacy: accountId survives"),
    gap("ssn", "legacy: ssn survives"),
    # --- homoglyph / unicode-normalisation bypass ------------------------------
    gap("аpi_key", "legacy: Cyrillic 'а' in api_key defeats the [a-z0-9] normaliser (no NFKC); LLM-authored keys can exploit this"),
    gap("ａｐｉ＿ｋｅｙ", "legacy: fullwidth 'api_key' defeats the normaliser (no NFKC)"),
]


@pytest.mark.parametrize("key", MISSED_KEYS)
def test_legacy_misses_key(key: str) -> None:
    assert is_redacted(key)


@pytest.mark.xfail(strict=True, reason="legacy: a list of (name, value) pairs (multidict/header list form) is not treated as a mapping")
def test_legacy_misses_header_pair_lists() -> None:
    out = sanitize({"raw": [("Authorization", "Bearer " + SECRET), ["cookie", SECRET]]})
    assert SECRET not in json.dumps(out)


@pytest.mark.xfail(strict=True, reason="legacy: key-based only; secrets embedded in string VALUES (Bearer tokens, ?api_key= in URLs, error text) survive")
def test_legacy_misses_secrets_in_values() -> None:
    out = sanitize({"message": "Authorization: Bearer " + SECRET, "url": "https://x?api_key=" + SECRET})
    assert SECRET not in json.dumps(out)


@pytest.mark.xfail(strict=True, reason="legacy: a nested pydantic model is returned untouched (not a dict), so its api_key field survives")
def test_legacy_misses_nested_pydantic_models() -> None:
    class Creds(BaseModel):
        api_key: str = SECRET

    out = sanitize({"m": Creds()})
    assert SECRET not in json.dumps(out, default=lambda o: o.model_dump())


@pytest.mark.xfail(strict=True, reason="legacy: sets are neither recursed nor converted; they survive unsanitised and are not JSON-serialisable")
def test_legacy_misses_sets() -> None:
    out = sanitize({"s": {SECRET}})
    assert SECRET not in json.dumps(out)


@pytest.mark.xfail(strict=True, reason="legacy: a bare string at the top level is returned verbatim")
def test_legacy_misses_top_level_string_values() -> None:
    assert SECRET not in json.dumps(sanitize("Bearer " + SECRET))


# ---------------------------------------------------------------------------
# 3. Coverage of the redaction: only ONE of four audit entry points sanitises
# ---------------------------------------------------------------------------

def _risk_report() -> RiskReport:
    return RiskReport(
        proposal_id="p1",
        symbol="AAPL",
        original_quantity=1,
        recommended_quantity=1,
        blocked=False,
        risk_score=0,
        reasons=["ok"],
        checks=[],
    )


def _ai() -> AIRiskAnalysis:
    return AIRiskAnalysis(
        proposal_id="p1",
        recommendation=Decision.APPROVE,
        confidence=0.5,
        recommended_quantity=1,
        risk_thesis="t",
        hidden_risks=[],
        reasoning=["r"],
        model_name="m",
    )


def _governor() -> GovernorDecision:
    return GovernorDecision(
        proposal_id="p1",
        symbol="AAPL",
        decision=Decision.APPROVE,
        original_quantity=1,
        approved_quantity=1,
        reason="ok",
        risk_score=0,
    )


def test_legacy_only_execution_events_pass_through_sanitize(monkeypatch) -> None:
    """Pins F-7: append_risk / append_ai_risk / append_governor never call _sanitize.

    In the new core, ``TenantLedger.append`` applies ``redact`` to the WHOLE
    record (API-SURFACE §3.6), not to one event type.
    """

    calls: list[str] = []
    original = _AuditEventFactory._sanitize.__func__

    def spy(cls, value):
        calls.append("sanitize")
        return original(cls, value)

    monkeypatch.setattr(_AuditEventFactory, "_sanitize", classmethod(spy))
    log = InMemoryAuditLog()

    log.append_risk(_risk_report())
    log.append_ai_risk(_ai())
    log.append_governor(_governor())
    assert calls == [], "legacy: non-execution audit events bypass redaction entirely"

    log.append_execution("p1", "X", {"api_key": SECRET})
    assert calls == ["sanitize"]
    assert log.list_for_proposal("p1")[-1].payload == {"api_key": REDACTED}
