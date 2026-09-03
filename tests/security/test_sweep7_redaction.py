"""L5 Sweep 7.5 — the redaction attack, in both directions.

**Forward:** get a credential into the persisted record. Key spellings, homoglyphs, header
pair-lists, secrets inside string values, nested contract models.

**Reverse (the one that bites):** find a benign contract value whose *shape* collides with a
credential pattern, so redaction destroys a field the contract requires and the record becomes
unbuildable. Two collisions already cost this sprint a record each — ``CalendarState.session``
(a market session, not a login session) and ``occ_symbol`` (F-26: the Alpaca ``PK``/``AK`` key-id
pattern also matched OCC symbols for Akamai and Packaging Corp).

**This sweep found a third: F-27.** The F-26 fix excludes OCC symbols with a purely alphabetic
root, but the OCC root of an *adjusted* contract carries a numeric suffix (``AKAM1``, ``PKG1``) -
which is exactly what OCC assigns after a corporate action, and exactly what the broker reports for
a position in one. Those symbols still match the key-id pattern, so a single adjusted option
position anywhere in the portfolio snapshot makes **every** DecisionRecord for that tenant
unbuildable, equity decisions included.

Self-contained by design (ESC-3).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from mizan.audit import redact_for_persistence
from mizan.contracts import DecisionRecord, Position
from mizan.contracts.canonical import (
    REDACTED,
    SENSITIVE_KEY_PATTERNS,
    is_sensitive_key,
    redact,
)
from mizan.contracts._base import ContractModel
from mizan.contracts.trade_proposal import occ_symbol_for
from mizan.contracts.types import OCC_SYMBOL_PATTERN
from tests.fixtures import (
    make_context,
    make_decision_record,
    make_portfolio_snapshot,
)

pytestmark = pytest.mark.security

SECRET = "sk-ThisLooksExactlyLikeAnApiKey0123456789"  # secret-scan: allow


def contract_models() -> set[type[ContractModel]]:
    models: set[type[ContractModel]] = set()
    pending: list[type[Any]] = [ContractModel]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass not in models:
                models.add(subclass)
                pending.append(subclass)
    return models


# =============================================================================================
# FORWARD - can a credential reach the persisted record?
# =============================================================================================
KEY_SPELLINGS = (
    "api_key",
    "apiKey",
    "API-KEY",
    "X-Api-Key",
    "secret",
    "client_secret",
    "accessToken",
    "refresh_token",
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "authorization",
    "Proxy-Authorization",
    "www_authenticate",
    "bearer",
    "cookie",
    "Set-Cookie",
    "session_id",
    "sessionid",
    "jwt",
    "signature",
    "x-signature",
    "private_key",
    "connection_string",
    "connectionString",
    "dsn",
    "database_url",
    "db_url",
    "account_number",
    "ssn",
    "credentials",
    "аpi_key",  # Cyrillic a
    "ａｐｉ＿ｋｅｙ",  # fullwidth api_key
)


@pytest.mark.parametrize("key", KEY_SPELLINGS)
def test_every_credential_key_spelling_is_redacted(key: str) -> None:
    assert is_sensitive_key(key), f"{key!r} is not recognised as a credential key"
    assert redact({key: SECRET})[key] == REDACTED


def test_a_header_collection_is_replaced_wholesale_in_every_shape() -> None:
    shapes: tuple[Any, ...] = (
        {"headers": {"Authorization": f"Bearer {SECRET}", "X-Trace": "ok"}},
        {"request_headers": [("Authorization", f"Bearer {SECRET}")]},
        {"response_headers": {"Set-Cookie": "session=abc"}},
        {"http_headers": [["Proxy-Authorization", "Basic YWRtaW46aHVudGVyMg=="]]},
    )
    for shape in shapes:
        assert SECRET not in str(redact(shape))
        assert "hunter" not in str(redact(shape))


def test_a_header_pair_list_keeps_the_name_and_loses_the_value() -> None:
    out = redact([("Authorization", f"Bearer {SECRET}"), ("X-Trace", "keep-me")])
    assert out == [["Authorization", REDACTED], ["X-Trace", "keep-me"]]


VALUE_SHAPES = (
    f"Bearer {SECRET}",
    "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaa",
    "rc_aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # secret-scan: allow
    "xoxb-1111111111-aaaaaaaaaaaa",  # secret-scan: allow
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",  # secret-scan: allow
    "-----BEGIN RSA PRIVATE KEY-----",  # secret-scan: allow
    "postgresql://admin:hunter2@db.internal:5432/mizan",  # secret-scan: allow
    "https://api.example.com/v1/quote?api_key=aaaaaaaaaaaaaaaa&symbol=AAPL",
)


@pytest.mark.parametrize("value", VALUE_SHAPES)
def test_a_credential_pasted_into_free_text_is_scrubbed_in_place(value: str) -> None:
    """Every contract model is ``extra="forbid"``, so free text is the ONLY way one can arrive."""
    text = f"The agent's thesis. Debug: {value} -- end of note."
    out = redact({"reasoning": text})["reasoning"]
    assert REDACTED in out
    assert "The agent's thesis." in out, "surrounding prose must stay auditable"
    for fragment in (value.split("?")[0] if "?" in value else value,):
        if fragment.startswith(("Bearer", "sk-", "rc_", "AKIA", "ghp_", "xoxb-", "eyJ", "-----")):
            assert fragment not in out


def test_a_secret_survives_nowhere_in_a_deeply_nested_structure() -> None:
    payload = {
        "level_a": [{"level_b": {"level_c": [{"api_key": SECRET}, {"note": f"Bearer {SECRET}"}]}}],
        "sets": {frozenset({"harmless"})},
    }
    assert SECRET not in str(redact(payload))


def test_redaction_runs_over_the_whole_record_before_the_hash_is_taken() -> None:
    """F-7 must not repeat: not per event kind, and never after hashing."""
    record = make_decision_record()
    payload = record.model_dump(mode="json")
    payload["proposal"]["reasoning"] = f"leaked: sk-{'a' * 40}"
    payload.pop("audit_hash")
    redacted = redact_for_persistence(payload)
    assert "sk-" + "a" * 40 not in str(redacted)
    rebuilt = DecisionRecord.build(**redacted)
    assert REDACTED in rebuilt.proposal.reasoning
    # The stored bytes and the hash cover the same redacted content.
    assert rebuilt.audit_hash == DecisionRecord.build(**redacted).audit_hash


# =============================================================================================
# REVERSE - can a benign contract value be destroyed by the redactor?
# =============================================================================================
def test_only_three_contract_field_names_match_a_sensitive_pattern() -> None:
    """The name-collision surface, enumerated. A new one appearing here needs a decision."""
    flagged = sorted(
        f"{model.__name__}.{name}"
        for model in contract_models()
        for name in model.model_fields
        if is_sensitive_key(name)
    )
    assert flagged == [
        "CalendarState.session",
        "DecisionRecord.authorization",
        "Policy.authorization",
    ], f"a new contract field name collides with {SENSITIVE_KEY_PATTERNS}: {flagged}"


def test_the_two_known_name_collisions_still_survive_redaction() -> None:
    """``CalendarState.session`` and ``Policy.authorization``: the sprint's first two outages."""
    record = make_decision_record()
    payload = record.model_dump(mode="json")
    payload.pop("audit_hash")
    redacted = redact_for_persistence(payload)
    assert redacted["policy_snapshot"]["authorization"]["ttl_seconds"] == 15
    for session in ("pre", "open", "close", "after", "closed"):
        assert redact({"session": session})["session"] == session
    # ...and a session TOKEN under the same key is still destroyed.
    assert redact({"session": "eyJhbGciOi.session.token"})["session"] == REDACTED


def test_an_ordinary_occ_symbol_survives_redaction() -> None:
    """F-26's fix, pinned: Akamai and Packaging Corp options are instruments, not credentials."""
    for root in ("AKAM", "PKG", "AKRO", "PKOH", "PKX", "AAPL", "MSFT"):
        occ = occ_symbol_for(root, "call", "2026-09-25", "230.0")
        assert re.fullmatch(OCC_SYMBOL_PATTERN, occ)
        assert redact({"occ_symbol": occ})["occ_symbol"] == occ


# ---------------------------------------------------------------------------------------------
# FINDING F-27 - the third collision: adjusted OCC roots beginning AK or PK
# ---------------------------------------------------------------------------------------------
#: What OCC assigns after a corporate action: the root gains a numeric suffix. These are valid
#: ``OccSymbol`` values by the contract's own pattern and are what a broker reports for a position.
ADJUSTED_OCC_ROOTS = ("AKAM1", "AKAM2", "PKG1", "AKRO1", "PKOH1")


@pytest.mark.parametrize("root", ADJUSTED_OCC_ROOTS)
def test_f27_an_adjusted_occ_root_is_a_valid_contract_value(root: str) -> None:
    occ = occ_symbol_for(root, "call", "2026-09-25", "230.0")
    assert re.fullmatch(OCC_SYMBOL_PATTERN, occ), f"{occ} is not a valid OccSymbol"
    Position(
        symbol=root,
        asset_class="equity_option",
        quantity="1",
        market_value="100",
        sector=None,
        occ_symbol=occ,
        delta=None,
        gamma=None,
        vega=None,
    )


@pytest.mark.xfail(
    strict=False,
    reason="F-27 OPEN (HIGH, L0 contracts/canonical.py): the Alpaca key-id value pattern's OCC "
    "negative lookahead is `[A-Z]{1,6}` and so misses an ADJUSTED OCC root, which carries a "
    "numeric suffix (AKAM1, PKG1). Those symbols are redacted, and one such position in the "
    "portfolio snapshot makes every DecisionRecord for the tenant unbuildable. Fix: widen the "
    "lookahead to the contract's own OCC_SYMBOL_PATTERN body. Remove this marker when fixed.",
)
@pytest.mark.parametrize("root", ADJUSTED_OCC_ROOTS)
def test_f27_an_adjusted_occ_symbol_must_survive_redaction(root: str) -> None:
    occ = occ_symbol_for(root, "call", "2026-09-25", "230.0")
    assert redact({"occ_symbol": occ})["occ_symbol"] == occ, (
        f"{occ} is an instrument identifier, not an API key"
    )


@pytest.mark.xfail(
    strict=False,
    reason="F-27 OPEN (HIGH, L0): one adjusted-option POSITION in the portfolio snapshot makes "
    "every DecisionRecord for that tenant unbuildable - an availability outage far wider than "
    "F-26's, because the portfolio snapshot is in the risk context of every decision, equity "
    "decisions included. Remove this marker when F-27 is fixed.",
)
def test_f27_one_adjusted_option_position_must_not_stop_the_tenant_recording_anything() -> None:
    occ = occ_symbol_for("AKAM1", "call", "2026-09-25", "230.0")
    position = Position(
        symbol="AKAM1",
        asset_class="equity_option",
        quantity="1",
        market_value="100",
        sector=None,
        occ_symbol=occ,
        delta=None,
        gamma=None,
        vega=None,
    )
    record = make_decision_record(
        risk_context=make_context(portfolio_snapshot=make_portfolio_snapshot(positions=[position]))
    )
    payload = record.model_dump(mode="json")
    payload.pop("audit_hash")
    redacted = redact_for_persistence(payload)
    stored = redacted["risk_context"]["portfolio_snapshot"]["positions"][0]["occ_symbol"]
    assert stored == occ, f"the position's OCC symbol was redacted to {stored!r}"
    DecisionRecord.build(**redacted)


def test_f27_the_failure_is_closed_not_silent() -> None:
    """The one comfort: it refuses the append rather than persisting something wrong."""
    occ = occ_symbol_for("AKAM1", "call", "2026-09-25", "230.0")
    position = Position(
        symbol="AKAM1",
        asset_class="equity_option",
        quantity="1",
        market_value="100",
        sector=None,
        occ_symbol=occ,
        delta=None,
        gamma=None,
        vega=None,
    )
    record = make_decision_record(
        risk_context=make_context(portfolio_snapshot=make_portfolio_snapshot(positions=[position]))
    )
    payload = record.model_dump(mode="json")
    payload.pop("audit_hash")
    redacted = redact_for_persistence(payload)
    with pytest.raises(Exception, match="occ_symbol"):
        DecisionRecord.build(**redacted)


def test_no_other_pattern_constrained_contract_value_collides_with_a_value_shape() -> None:
    """Sweep the value patterns across every realistic pattern-validated field this build emits.

    A hash, a uuid7, an RFC3339 stamp, a DecimalStr, an idempotency key and an ordinary symbol are
    all safe. The OCC symbol is the one shape long enough and uppercase enough to collide, which is
    why F-26 and F-27 are both about it.
    """
    from mizan.contracts.canonical import idempotency_key_for

    samples = {
        "sha256": "a" * 64,
        "uuid7": "01a06400-0000-7000-8000-0123456789ab",
        "rfc3339": "2026-09-03T10:00:00.123456Z",
        "date": "2026-09-25",
        "decimal": "228.5",
        "ratio": "0.15",
        "symbol": "AAPL",
        "symbol_class": "AAPL-B",
        "symbol_dot": "BRK.B",
        "tenant": "tenant-a",
        "policy_id": "policy-standard",
        "semver": "1.0.0",
        "idempotency": idempotency_key_for("tenant-a", "b" * 64, []),
        "occ_plain": occ_symbol_for("AAPL", "call", "2026-09-25", "230.0"),
    }
    for name, value in samples.items():
        assert redact({name: value})[name] == value, f"{name}={value!r} was redacted"
