"""Hard Rule A3 and security findings F-6 / F-7: what the ledger writes carries no credential.

F-7 (the legacy mistake): redaction was applied at ONE of four audit entry points, so risk reports,
governor decisions and verbatim model output were persisted in the clear. Here redaction is applied to
the WHOLE record, once, BEFORE the hash is computed - so the stored bytes and the ``audit_hash`` cover
the same content and a verifier re-hashing what it was given agrees without ever being shown a secret.

F-6 (the known gaps): ``mizan.contracts.canonical.redact`` is FROZEN L0 code and this lane does not edit
it. Every gap it still has is pinned below as ``xfail(strict=True)`` naming the request that asks L0 to
close it, so the day it is fixed these tests fail loudly and the pins come out.
"""

from __future__ import annotations

import json

import pytest

from mizan.audit import STRUCTURAL_SECTION_KEYS, InMemoryLedger, SqliteLedger, redact_for_persistence
from mizan.contracts.canonical import REDACTED, canonical_json, record_hash_for
from tests.audit._helpers import append_record, chain_parts
from tests.fixtures import FIXED_NOW, TENANT_A, make_advisory, make_proposal

SECRET = "sk-live-fixture-0123456789abcdef-do-not-persist"  # noqa: S105 - a fixture, not a credential  # secret-scan: allow
REQ = "ledger/requests.md REQ-2 (L2b -> L0: close the remaining redact() gaps)"

#: Key spellings ``redact`` already strips. Each is asserted at a nested position, not at the top level.
COVERED_KEYS = (
    "api_key", "apiKey", "API_KEY", "X-Api-Key", "apikey",
    "secret", "secret_key", "clientSecret", "SECRET",
    "token", "access_token", "refreshToken", "id_token",
    "password", "PASSWORD",
    "credential", "credentials", "aws_credentials",
    "authorization", "Authorization", "authorizationHeader", "proxy-authorization", "Proxy-Authorization",
    "header", "headers", "request_headers", "response_headers", "http_headers",
    "cookie", "Cookie", "set-cookie", "Set-Cookie",
    "private_key", "privateKey", "PRIVATE_KEY",
    "connection_string", "connectionString", "dsn", "DSN",
    "passwd",
)

#: Key spellings F-6 lists that ``redact`` still lets through. Pinned, not fixed here.
GAP_KEYS = (
    "www-authenticate", "bearer",
    "session", "session_id", "sessionid", "jwt", "auth",
    "signature", "x-signature",
    "passphrase", "pwd", "pass",
    "database_url", "db_url",
    "account_id", "account_number", "accountId", "ssn",
    "аpi_key",  # Cyrillic 'a' homoglyph
    "ａｐｉ＿ｋｅｙ",  # fullwidth api_key
)


def _nested(key: str) -> dict:
    """The same credential at five depths, in a mapping, in a list and inside a contract-shaped object."""
    return {
        "top": {key: SECRET},
        "deep": {"a": {"b": {"c": {key: SECRET}}}},
        "in_a_list": [{"x": 1}, {key: SECRET}],
        "in_a_contract_object": {"schema_version": "1.0.0", "inner": {key: SECRET}},
        "beside_business_data": {"symbol": "AAPL", key: SECRET, "quantity": "10"},
    }


@pytest.mark.parametrize("key", COVERED_KEYS)
def test_the_ledger_redaction_strips_a_credential_at_every_nesting_depth(key):
    cleaned = redact_for_persistence(_nested(key))
    assert SECRET not in json.dumps(cleaned), f"{key!r} survived at some depth: {cleaned}"
    assert cleaned["beside_business_data"]["symbol"] == "AAPL", "business data must survive untouched"
    assert cleaned["beside_business_data"]["quantity"] == "10"


@pytest.mark.parametrize(
    "key",
    [pytest.param(k, id=repr(k), marks=pytest.mark.xfail(strict=True, reason=REQ)) for k in GAP_KEYS],
)
def test_gap_the_ledger_redaction_should_strip_this_key_too(key):
    assert SECRET not in json.dumps(redact_for_persistence(_nested(key)))


@pytest.mark.xfail(strict=True, reason=REQ + " - header pair-lists")
def test_gap_a_header_pair_list_should_be_redacted():
    payload = {"raw": [("Authorization", "Bearer " + SECRET), ["cookie", SECRET]]}
    assert SECRET not in json.dumps(redact_for_persistence(payload))


@pytest.mark.xfail(strict=True, reason=REQ + " - secrets embedded in string values")
def test_gap_a_secret_inside_a_string_value_should_be_redacted():
    payload = {
        "message": "broker rejected: Authorization: Bearer " + SECRET,
        "url": "https://broker.example/orders?api_key=" + SECRET,
    }
    assert SECRET not in json.dumps(redact_for_persistence(payload))


def test_a_header_collection_is_replaced_wholesale_not_key_by_key():
    cleaned = redact_for_persistence({"request_headers": {"X-Trace": "abc", "Authorization": SECRET}})
    assert cleaned["request_headers"] == REDACTED


def test_a_contract_section_whose_name_collides_with_a_header_keeps_its_structure():
    """``Policy.authorization`` is the TTL section, not an ``Authorization:`` header.

    Redacting it wholesale would destroy the policy hash and make the record unbuildable, so the ledger
    recurses into it - and still redacts every scalar inside.
    """
    assert "authorization" in STRUCTURAL_SECTION_KEYS
    cleaned = redact_for_persistence(
        {"policy_snapshot": {"schema_version": "1.0.0", "authorization": {"ttl_seconds": 15}}}
    )
    assert cleaned["policy_snapshot"]["authorization"] == {"ttl_seconds": 15}

    # a *scalar* under that name is still a credential and is still replaced
    assert redact_for_persistence({"authorization": SECRET})["authorization"] == REDACTED
    # ... and so is anything sensitive nested inside the section
    nested = redact_for_persistence({"authorization": {"ttl_seconds": 15, "api_key": SECRET}})
    assert nested["authorization"]["api_key"] == REDACTED
    assert nested["authorization"]["ttl_seconds"] == 15


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_redaction_happens_before_hashing_so_the_stored_bytes_and_the_hash_agree(tmp_path, storage):
    """F-7: one redaction pass over the whole record, then the hash. Never the other way round."""
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    record = append_record(
        tenant_ledger,
        proposal=make_proposal(reasoning="Momentum continuation; nothing sensitive here."),
        advisory=make_advisory(),
        authorized=True,
    )

    stored = tenant_ledger.get(record.decision_id)
    dump = stored.model_dump(mode="json")
    assert stored.audit_hash == record_hash_for(dump)
    assert canonical_json(stored) == canonical_json(record)
    assert tenant_ledger.verify_chain().ok


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
@pytest.mark.xfail(strict=True, reason=REQ + " - secrets embedded in string values")
def test_gap_a_secret_pasted_into_free_text_should_not_reach_storage(tmp_path, storage):
    """The record's free-text fields are the only place a credential can still enter.

    Every contract model is ``extra="forbid"``, so no unexpected KEY can be persisted at all; what can
    is a secret pasted into prose - agent reasoning, advisory reasoning, an execution message. Key-based
    redaction cannot see those, which is exactly why F-6 asks L0 for value patterns.
    """
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    record = append_record(
        tenant_ledger,
        proposal=make_proposal(reasoning=f"Called the broker with Authorization: Bearer {SECRET}."),
        advisory=make_advisory(reasoning=f"see ?api_key={SECRET}"),
        authorized=True,
    )
    assert SECRET not in canonical_json(tenant_ledger.get(record.decision_id))


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_credential_shaped_key_in_a_free_form_map_fails_the_append_closed(tmp_path, storage):
    """Free-form maps in the contracts (``exposure_by_signal_source``) take arbitrary KEYS.

    A credential-shaped key there means redaction replaces a DecimalStr value with ``"[REDACTED]"`` and
    the record no longer satisfies its contract, so the append is refused. Refusing to record is the
    right way to fail: it is loud, and it never writes the value.
    """
    from pydantic import ValidationError

    from tests.fixtures import make_aggregate_state, make_context, make_policy

    policy = make_policy()
    context = make_context(
        policy=policy.ref,
        aggregate_state=make_aggregate_state(exposure_by_signal_source={"api_key": "1000"}),
    )
    parts = chain_parts(policy=policy, context=context)

    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    with pytest.raises(ValidationError):
        tenant_ledger.append(
            proposal=parts.proposal,
            risk_context=parts.context,
            risk_evaluation=parts.evaluation,
            governor_decision=parts.decision,
            policy_snapshot=parts.policy,
            recorded_at=FIXED_NOW,
        )
    assert tenant_ledger.verify_chain().length == 0
    assert tenant_ledger.list(limit=50) == []
