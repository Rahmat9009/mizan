"""Hard Rule A3 and security findings F-6 / F-7: what the ledger writes carries no credential.

F-7 (the legacy mistake): redaction was applied at ONE of four audit entry points, so risk reports,
governor decisions and verbatim model output were persisted in the clear. Here redaction is applied to
the WHOLE record, once, BEFORE the hash is computed - so the stored bytes and the ``audit_hash`` cover
the same content and a verifier re-hashing what it was given agrees without ever being shown a secret.

F-6 (the gaps): closed by L0 in ``mizan.contracts.canonical.redact``. These were 24 ``xfail(strict=True)``
pins and are now ordinary assertions. The one that mattered most is the last section: every contract
model is ``extra="forbid"``, so an unexpected KEY can never be persisted, which made a secret pasted
into ``reasoning`` or a broker message the only way one could reach a record at all - and key-based
redaction could not see it. Value-pattern scrubbing closes that.

Everything here goes through ``mizan.audit.redact_for_persistence`` - the one place the ledger redacts -
rather than through ``redact`` directly, so these assert what actually reaches storage.
"""

from __future__ import annotations

import inspect
import json
import typing

import pytest
from pydantic import BaseModel, ValidationError

import mizan.contracts as contracts
from mizan.audit import InMemoryLedger, SqliteLedger, redact_for_persistence
from mizan.contracts.canonical import REDACTED, canonical_json, is_sensitive_key, record_hash_for
from tests.audit._helpers import append_record, chain_parts
from tests.fixtures import (
    FIXED_NOW,
    TENANT_A,
    make_advisory,
    make_aggregate_state,
    make_context,
    make_institutional_context,
    make_institutional_policy,
    make_policy,
    make_proposal,
)

#: Credential-SHAPED, so the value-pattern tests below test what they claim to.
SECRET = "sk-live-fixture-0123456789abcdef-do-not-persist"  # noqa: S105 - a fixture  # secret-scan: allow
#: Deliberately NOT credential-shaped: the key-based tests must pass because of the KEY, never because
#: value scrubbing happened to catch the value as well.
OPAQUE = "correct-horse-battery-staple-9f2c1a"

#: Every key spelling F-6 asked for. Each is asserted at five nesting depths, not at the top level.
COVERED_KEYS = (
    # covered before the F-6 fix
    "api_key", "apiKey", "API_KEY", "X-Api-Key", "apikey",
    "secret", "secret_key", "clientSecret", "SECRET",
    "token", "access_token", "refreshToken", "id_token",
    "password", "PASSWORD", "passwd",
    "credential", "credentials", "aws_credentials",
    "authorization", "Authorization", "authorizationHeader",
    "proxy-authorization", "Proxy-Authorization",
    "header", "headers", "request_headers", "response_headers", "http_headers", "raw_headers",
    "cookie", "Cookie", "set-cookie", "Set-Cookie", "cookies",
    "private_key", "privateKey", "PRIVATE_KEY",
    "connection_string", "connectionString", "dsn", "DSN",
    # closed by the F-6 fix
    "www-authenticate", "bearer", "Bearer",
    "session_id", "sessionid", "sessionId",
    "jwt", "JWT",
    "signature", "x-signature", "X-Signature",
    "passphrase", "pwd", "pass",
    "auth",
    "database_url", "db_url", "databaseUrl",
    "account_id", "account_number", "accountId", "accountNumber", "ssn", "SSN",
    # homoglyphs: NFKC folds the fullwidth form, an explicit table folds the Cyrillic one
    "аpi_key",  # Cyrillic 'а'
    "ａｐｉ＿ｋｅｙ",  # fullwidth
    "аpiKey",  # Cyrillic 'а', camelCase
)

#: Header and cookie bags: replaced WHOLESALE, names and all, because header names are revealing too.
COLLECTION_KEYS = (
    "header", "headers", "Header", "HTTP_HEADERS", "request_headers", "response_headers",
    "http_headers", "raw_headers", "X-Request-Headers", "cookie", "Cookie", "cookies", "set-cookie",
)


def _nested(key: str, value: str = OPAQUE) -> dict:
    """The same credential at five depths: in a mapping, in a list, inside a contract-shaped object."""
    return {
        "top": {key: value},
        "deep": {"a": {"b": {"c": {key: value}}}},
        "in_a_list": [{"x": 1}, {key: value}],
        "in_a_contract_object": {"schema_version": "1.0.0", "inner": {key: value}},
        "beside_business_data": {"symbol": "AAPL", key: value, "quantity": "10"},
    }


# ------------------------------------------------------------------------------------------------
# Key-based redaction
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", COVERED_KEYS)
def test_the_ledger_redaction_strips_a_credential_at_every_nesting_depth(key):
    cleaned = redact_for_persistence(_nested(key))
    assert OPAQUE not in json.dumps(cleaned), f"{key!r} survived at some depth: {cleaned}"
    assert cleaned["beside_business_data"]["symbol"] == "AAPL", "business data must survive untouched"
    assert cleaned["beside_business_data"]["quantity"] == "10"


@pytest.mark.parametrize("key", COLLECTION_KEYS)
def test_a_header_or_cookie_bag_is_replaced_wholesale_not_key_by_key(key):
    """The bag goes as a unit: redacting it key by key leaks whatever spelling nobody anticipated."""
    assert redact_for_persistence({key: {"X-Trace": "abc", "Authorization": OPAQUE}})[key] == REDACTED
    assert redact_for_persistence({key: ["Authorization: Bearer " + OPAQUE]})[key] == REDACTED


def test_a_flattened_header_pair_list_loses_its_values():
    """``[("Authorization", "Bearer ..."), ...]`` is how a multidict spells a bag once flattened."""
    cleaned = redact_for_persistence(
        {"raw": [("Authorization", "Bearer " + OPAQUE), ["set-cookie", OPAQUE], ["symbol", "AAPL"]]}
    )
    assert OPAQUE not in json.dumps(cleaned)
    assert cleaned["raw"] == [["Authorization", REDACTED], ["set-cookie", REDACTED], ["symbol", "AAPL"]]


def test_the_generic_words_are_sensitive_without_catching_the_contract_fields_beside_them():
    """``auth`` and ``pass`` are sensitive, which is only safe because neither swallows a real field.

    ``ExecutionAuthorization.auth_id`` tokenises to ``["auth", "id"]`` and WOULD match, so it is named in
    ``REDACTION_EXEMPT_KEYS``: it is a uuid7 lookup key the record's own validators cross-check, and
    redacting it would make every authorized decision unrecordable. ``CheckResult.passed`` is safe for a
    duller reason - it is one token, ``passed``, and the matcher compares whole tokens, so ``pass`` does
    not reach it. Both are asserted because both would be silent, record-breaking regressions.
    """
    assert is_sensitive_key("auth") is True and is_sensitive_key("pass") is True
    assert is_sensitive_key("auth_id") is False, "redacting auth_id would break every authorization"
    assert is_sensitive_key("passed") is False, "redacting CheckResult.passed would break every record"


def test_a_set_is_recursed_into_and_comes_back_serialisable():
    """F-6: the legacy redactor left sets both unsanitised and unserialisable."""
    cleaned = redact_for_persistence({"tags": {"momentum", "large-cap"}, "api_key": {OPAQUE}})
    assert cleaned["tags"] == ["large-cap", "momentum"], "set members keep a deterministic order"
    assert cleaned["api_key"] == REDACTED
    json.dumps(cleaned)


def test_a_nested_pydantic_model_is_recursed_into_rather_than_stopped_at():
    """F-6: the legacy redactor stopped at the first model, so everything below it was persisted raw."""
    cleaned = redact_for_persistence({"proposal": make_proposal(reasoning="paste: Bearer " + SECRET)})
    assert SECRET not in json.dumps(cleaned)
    assert cleaned["proposal"]["symbol"] == make_proposal().symbol


# ------------------------------------------------------------------------------------------------
# Value-pattern scrubbing - the gap that mattered, now closed
# ------------------------------------------------------------------------------------------------

# Deliberately credential-SHAPED fixtures - that is the whole point of them. Each carries the inline
# marker scripts/secret_scan.py looks for, so the repository scanner reads them as test shapes rather
# than as a leak. None is a real credential and none has ever been issued.
_ANTHROPIC_KEY = "sk-ant-api03-" + "A" * 40  # secret-scan: allow
_ALPACA_KEY_ID = "PKTEST0123456789ABCDEF"  # secret-scan: allow
_AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"  # secret-scan: allow
_GITHUB_TOKEN = "ghp_" + "a" * 24  # secret-scan: allow
_SLACK_TOKEN = "xoxb-123456789012-abcdefghijkl"  # secret-scan: allow
_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."  # secret-scan: allow
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlF"  # secret-scan: allow
)
_PEM_HEADER = "-----BEGIN RSA PRIVATE KEY-----"  # secret-scan: allow
_DB_PASSWORD = "hunter2"  # noqa: S105 - a fixture  # secret-scan: allow
_DB_URL = f"postgres://mizan:{_DB_PASSWORD}@db.example/ledger"  # secret-scan: allow

VALUE_CASES = (
    ("bearer token", "called the broker with Authorization: Bearer " + SECRET),
    ("openai-style key", "leaked " + SECRET + " in a log line"),
    ("anthropic-style key", _ANTHROPIC_KEY),
    ("alpaca key id", _ALPACA_KEY_ID),
    ("aws access key", _AWS_KEY_ID),
    ("github token", _GITHUB_TOKEN),
    ("slack token", _SLACK_TOKEN),
    ("jwt", _JWT),
    ("pem private key", _PEM_HEADER),
    ("url with credentials", _DB_URL),
    ("api_key query parameter", "https://broker.example/orders?api_key=" + SECRET),
    ("token query parameter", "https://broker.example/orders?token=" + SECRET),
)


@pytest.mark.parametrize("label,text", VALUE_CASES, ids=[case[0] for case in VALUE_CASES])
def test_a_credential_shaped_span_is_scrubbed_out_of_free_text(label, text):
    """Under a harmless key - ``reasoning`` - because that is where a pasted secret actually lands."""
    cleaned = redact_for_persistence({"reasoning": text})["reasoning"]
    assert REDACTED in cleaned, f"{label}: nothing was scrubbed from {text!r}"
    for needle in (SECRET, _DB_PASSWORD, _AWS_KEY_ID):
        if needle in text:
            assert needle not in cleaned, f"{label}: {needle!r} survived"


def test_scrubbing_replaces_only_the_span_so_the_prose_stays_auditable():
    prose = "Momentum continuation above the 20-day mean; invalidated below 224."
    assert redact_for_persistence({"reasoning": prose})["reasoning"] == prose

    mixed = f"Broker call failed. Authorization: Bearer {SECRET}. Retrying in 30s."
    cleaned = redact_for_persistence({"reasoning": mixed})["reasoning"]
    assert SECRET not in cleaned
    assert cleaned.startswith("Broker call failed.")
    assert cleaned.endswith("Retrying in 30s.")


# ------------------------------------------------------------------------------------------------
# Contract structure survives the redactor (REQ-3, closed)
# ------------------------------------------------------------------------------------------------


def test_a_contract_section_that_merely_shares_a_headers_name_keeps_its_structure():
    """``Policy.authorization`` is the TTL section, not an ``Authorization:`` header.

    This lane carries no workaround for it any more. ``redact`` recognises a nested contract model from
    the models' OWN field declarations, so the section survives - and a mapping under the same name that
    is not one is still replaced wholesale.
    """
    policy = make_policy()
    assert redact_for_persistence(policy.model_dump(mode="json"))["authorization"] == {"ttl_seconds": 15}

    assert redact_for_persistence({"authorization": OPAQUE})["authorization"] == REDACTED
    blob = redact_for_persistence({"authorization": {"scheme": "Basic", "value": OPAQUE}})
    assert blob["authorization"] == REDACTED, "a credentials blob under the same name is not a section"


def test_a_numeric_value_survives_only_under_a_namespaced_data_key():
    """The one deliberate hole, both halves of it.

    Typed decimal maps are keyed by DATA ("vendor:secret-feed"), and writing ``"[REDACTED]"`` where a
    DecimalStr belongs makes the record unbuildable. A plain field name never carries a separator, so
    ``account_number`` and ``ssn`` stay redacted even though their values are digits.
    """
    survives = redact_for_persistence(
        {"vendor:secret-feed": "1000", "model:featherless/token": "2.5", "news:reuters": "0.4"}
    )
    assert survives == {"vendor:secret-feed": "1000", "model:featherless/token": "2.5", "news:reuters": "0.4"}

    redacted = redact_for_persistence(
        {"account_number": "123456789", "ssn": "123456789", "secret": "42", "api_key": "1000"}
    )
    assert redacted == dict.fromkeys(("account_number", "ssn", "secret", "api_key"), REDACTED)

    # a namespaced key with a NON-numeric value is still a credential
    assert redact_for_persistence({"vendor:secret-feed": OPAQUE})["vendor:secret-feed"] == REDACTED


def _contract_field_names() -> dict[str, set[str]]:
    """Every field name of every contract model reachable from the top-level contracts."""
    seen: set[type] = set()
    names: dict[str, set[str]] = {}

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for field_name, field in model.model_fields.items():
            names.setdefault(field_name, set()).add(model.__name__)
            for annotation in (field.annotation, *typing.get_args(field.annotation)):
                for inner in (annotation, *typing.get_args(annotation)):
                    if inspect.isclass(inner) and issubclass(inner, BaseModel):
                        walk(inner)

    for model in contracts.TOP_LEVEL_CONTRACTS.values():
        walk(model)
    return names


def test_no_contract_field_is_mistaken_for_a_credential_except_where_it_is_handled():
    """The guard that keeps the ledger recordable as the sensitive-key set grows.

    A key-based redactor cannot tell ``CalendarState.session`` (one of five market sessions) from a
    login session. When it guesses wrong on a required field the record stops being buildable and the
    tenant stops being able to record decisions at all - a governance outage caused by a redaction rule.
    This enumerates every field of every contract model, so a future addition to SENSITIVE_KEY_PATTERNS
    shows up here rather than in production.
    """
    names = _contract_field_names()
    assert len(names) > 250, "the walk must actually reach the nested models"
    collisions = {name for name in names if is_sensitive_key(name)}
    assert collisions == {"authorization", "session"}, collisions

    # Neither is handled by a hand-written allow-list; both are read out of the models themselves.
    # ``authorization`` holds a contract object or a declared sub-section, so it is recursed into.
    assert redact_for_persistence(make_policy().model_dump(mode="json"))["authorization"] == {
        "ttl_seconds": 15
    }
    # ``session`` holds one of the five constants ``CalendarState`` declares, so THAT VALUE survives -
    # and only that value. Anything else under the same key is a credential like any other.
    assert redact_for_persistence({"session": "open"}) == {"session": "open"}
    assert redact_for_persistence({"session": OPAQUE}) == {"session": REDACTED}
    assert redact_for_persistence({"session": "Bearer " + SECRET}) == {"session": REDACTED}


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_record_carrying_every_addendum_input_still_records(tmp_path, storage):
    """The institutional context populates ``calendar``; a redacted ``session`` makes it unrecordable."""
    policy = make_institutional_policy()
    context = make_institutional_context()
    assert context.calendar is not None
    parts = chain_parts(policy=policy, context=context)

    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(policy.tenant_id)
    record = tenant_ledger.append(
        proposal=parts.proposal,
        risk_context=parts.context,
        risk_evaluation=parts.evaluation,
        governor_decision=parts.decision,
        policy_snapshot=parts.policy,
        recorded_at=FIXED_NOW,
    )
    assert record.risk_context.calendar is not None
    assert record.risk_context.calendar.session == context.calendar.session
    assert tenant_ledger.verify_chain().ok
    assert canonical_json(tenant_ledger.get(record.decision_id)) == canonical_json(record)


# ------------------------------------------------------------------------------------------------
# The ledger boundary
# ------------------------------------------------------------------------------------------------


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
    assert stored.audit_hash == record_hash_for(stored.model_dump(mode="json"))
    assert canonical_json(stored) == canonical_json(record)
    assert tenant_ledger.verify_chain().ok


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_secret_pasted_into_free_text_never_reaches_storage(tmp_path, storage):
    """The one way a credential can enter a record, now closed end to end (F-6 / F-7).

    Every contract model is ``extra="forbid"``, so no unexpected KEY can be persisted; prose is the only
    carrier left. Agent reasoning and advisory reasoning are both checked, and each is stored twice over
    (on the record and inside the embedded governor decision), so this covers four copies.
    """
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)
    record = append_record(
        tenant_ledger,
        proposal=make_proposal(reasoning=f"Called the broker with Authorization: Bearer {SECRET}."),
        advisory=make_advisory(reasoning=f"Provider echoed ?api_key={SECRET} in its error."),
        authorized=True,
    )

    stored = canonical_json(tenant_ledger.get(record.decision_id))
    assert SECRET not in stored
    assert REDACTED in stored
    assert "Called the broker with" in stored, "the surrounding prose must stay auditable"
    assert tenant_ledger.verify_chain().ok


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_a_credential_shaped_key_in_a_free_form_map_fails_the_append_closed(tmp_path, storage):
    """A plain sensitive name over a DecimalStr map is refused, not silently written.

    ``exposure_by_signal_source`` takes arbitrary KEYS. A namespaced data key keeps its number (the
    deliberate hole above); a bare ``api_key`` does not, so redaction writes ``"[REDACTED]"`` where a
    DecimalStr belongs and the record stops satisfying its contract. Refusing to record is the right way
    to fail: it is loud, and the value is never written.
    """
    ledger = InMemoryLedger() if storage == "memory" else SqliteLedger(root_dir=tmp_path)
    tenant_ledger = ledger.for_tenant(TENANT_A)

    def append_with(exposure: dict[str, str]):
        policy = make_policy()
        context = make_context(
            policy=policy.ref,
            aggregate_state=make_aggregate_state(exposure_by_signal_source=exposure),
        )
        parts = chain_parts(policy=policy, context=context)
        return tenant_ledger.append(
            proposal=parts.proposal,
            risk_context=parts.context,
            risk_evaluation=parts.evaluation,
            governor_decision=parts.decision,
            policy_snapshot=parts.policy,
            recorded_at=FIXED_NOW,
        )

    with pytest.raises(ValidationError):
        append_with({"api_key": "1000"})
    assert tenant_ledger.verify_chain().length == 0
    assert tenant_ledger.list(limit=50) == []

    # the namespaced form a real signal source uses records normally, number intact
    record = append_with({"vendor:secret-feed": "1000"})
    assert record.risk_context.aggregate_state is not None
    assert record.risk_context.aggregate_state.exposure_by_signal_source == {"vendor:secret-feed": "1000"}
    assert tenant_ledger.verify_chain().ok
