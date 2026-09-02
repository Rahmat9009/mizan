"""The freeze guard: contracts/ and mizan/contracts must never disagree, and never quietly loosen.

Every test here is a property the whole system leans on. If one fails, the fix is the code, not the
test — a contract that has drifted is a contract nobody can rely on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError

from mizan.contracts import (
    SCHEMA_VERSION,
    TOP_LEVEL_CONTRACTS,
    AdvisoryOpinion,
    BrokerRef,
    FailClosed,
    Leg,
    ReasonCode,
)
from mizan.contracts.canonical import canonical_json, redact, sha256_hex, uuid7

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
SCHEMA_FILES = sorted(CONTRACTS_DIR.glob("*.schema.json"))


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def walk(node, path="$"):
    """Yield (json path, subschema) for every dict node in a schema."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


# ----------------------------------------------------------------------------------------------
# The schemas exist, are valid, and match the models
# ----------------------------------------------------------------------------------------------
def test_every_contract_has_a_schema():
    names = {path.name.removesuffix(".schema.json") for path in SCHEMA_FILES}
    assert names == set(TOP_LEVEL_CONTRACTS), (
        f"schema files {sorted(names)} do not match contracts {sorted(TOP_LEVEL_CONTRACTS)}"
    )
    assert len(SCHEMA_FILES) == 9, [p.name for p in SCHEMA_FILES]


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.name)
def test_schema_is_valid_draft_2020_12(path: Path):
    schema = load(path)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == (
        f"https://mizan.dev/contracts/{SCHEMA_VERSION}/{path.name}"
    ), schema["$id"]


def test_schemas_are_not_stale():
    """Regenerating must produce no diff. This is what makes drift impossible rather than unlikely."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_schemas.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        "contracts/ is out of date with mizan/contracts.\n"
        "Run: python scripts/generate_schemas.py\n"
        f"{result.stdout}\n{result.stderr}"
    )


# ----------------------------------------------------------------------------------------------
# Hard Rules, expressed at the schema level
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.name)
def test_no_object_accepts_arbitrary_untyped_content(path: Path):
    """Every object is either a closed record or an open map with typed values. Never a free-for-all.

    Three legitimate shapes exist and they need different rules:

    * a **record** declares ``properties`` (a TradeProposal, a Policy) and must set
      ``additionalProperties: false``, so an unknown field is an error rather than a shrug;
    * a **pattern map** declares ``patternProperties`` (quotes keyed by symbol, option quotes keyed by
      OCC symbol) — keys must match the pattern, values are typed, and ``additionalProperties: false``
      rejects any key that does not match. This is the strictest shape of the three;
    * an **open map** declares its value schema in ``additionalProperties`` (checks by check id, exposure
      by model provider), so keys are free but every value is still typed.

    What is forbidden in all three is the same thing: content that nobody validates.
    """
    offenders: list[str] = []
    for json_path, node in walk(load(path)):
        if not (node.get("type") == "object" or "properties" in node):
            continue
        extra = node.get("additionalProperties")
        if "properties" in node:
            if extra is not False:
                offenders.append(
                    f"{json_path}: record accepts unknown fields (additionalProperties={extra!r})"
                )
        elif "patternProperties" in node:
            patterns = node["patternProperties"]
            if extra is not False:
                offenders.append(f"{json_path}: pattern map accepts non-matching keys ({extra!r})")
            if not patterns or any(not isinstance(v, dict) or not v for v in patterns.values()):
                offenders.append(f"{json_path}: pattern map has an untyped value schema")
        elif not isinstance(extra, dict) or not extra:
            offenders.append(f"{json_path}: map values are untyped (additionalProperties={extra!r})")
    assert not offenders, f"{path.name}: unvalidated content:\n  " + "\n  ".join(offenders)


def test_every_environment_enum_is_paper_only():
    """Hard Rule B1 at the schema level: no schema can describe a live order."""
    seen = 0
    for path in SCHEMA_FILES:
        for json_path, node in walk(load(path)):
            properties = node.get("properties")
            if isinstance(properties, dict) and "environment" in properties:
                seen += 1
                assert properties["environment"].get("enum") == ["paper"], (
                    f"{path.name}:{json_path} environment is {properties['environment']}"
                )
    assert seen >= 3, f"expected several environment properties, found {seen}"


def test_schema_version_is_pinned_everywhere():
    for path in SCHEMA_FILES:
        schema = load(path)
        assert schema["x-mizan-schema-version"] == SCHEMA_VERSION
        properties = schema.get("properties", {})
        assert "schema_version" in properties, path.name


# ----------------------------------------------------------------------------------------------
# Round-tripping: model -> JSON -> schema validation -> model, byte-identical
# ----------------------------------------------------------------------------------------------
def _example_objects():
    from tests import fixtures

    return {
        "trade_proposal": fixtures.make_proposal(),
        "risk_context": fixtures.make_context(),
        "policy": fixtures.make_policy(),
        "risk_evaluation": fixtures.make_evaluation(),
        "governor_decision": fixtures.make_decision(),
        "execution_authorization": fixtures.make_authorization(),
    }


@pytest.mark.parametrize("name", sorted(_example_objects()))
def test_model_instances_validate_against_their_schema_and_round_trip(name: str):
    obj = _example_objects()[name]
    payload = obj.model_dump(mode="json")

    Draft202012Validator(load(CONTRACTS_DIR / f"{name}.schema.json")).validate(payload)

    restored = type(obj).model_validate(payload)
    assert canonical_json(restored) == canonical_json(obj), f"{name} did not round-trip byte-identically"


# ----------------------------------------------------------------------------------------------
# Canonical JSON
# ----------------------------------------------------------------------------------------------
def test_canonical_json_sorts_keys_and_strips_whitespace():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'
    assert canonical_json([1, {"b": 1, "a": 2}]) == '[1,{"a":2,"b":1}]'


def test_canonical_json_is_invariant_under_key_insertion_order():
    first = {"a": 1, "b": {"x": "1", "y": "2"}, "c": [1, 2]}
    second = {"c": [1, 2], "b": {"y": "2", "x": "1"}, "a": 1}
    assert canonical_json(first) == canonical_json(second)


def test_canonical_json_rejects_floats_and_non_finite_values():
    for value in (1.5, float("nan"), float("inf"), {"x": 0.1}, [2.0]):
        with pytest.raises(TypeError):
            canonical_json(value)


def test_canonical_json_renders_decimal_as_a_normalised_string():
    assert canonical_json(Decimal("1.5")) == '"1.5"'
    assert canonical_json(Decimal("2.400")) == '"2.4"'
    assert canonical_json(Decimal("100.00")) == '"100"'
    with pytest.raises(TypeError):
        canonical_json(Decimal("NaN"))


def test_decimal_strings_normalise_so_equal_money_hashes_equally():
    """The property replay depends on: how a price was spelled cannot change a hash."""

    def leg(limit_price: str):
        return Leg(
            leg_index=0,
            side="buy",
            contract_type=None,
            strike=None,
            expiry=None,
            quantity="10",
            limit_price=limit_price,
            order_type="limit",
        )

    assert leg("2.40").limit_price == "2.4"
    assert leg("100.00").limit_price == "100"
    assert canonical_json(leg("2.40")) == canonical_json(leg("2.4"))


# ----------------------------------------------------------------------------------------------
# Type-level Hard Rules
# ----------------------------------------------------------------------------------------------
def test_advisory_has_no_vocabulary_for_increasing_size():
    """Hard Rule E1: the type cannot express 'approve more', so no code path can."""
    import typing

    annotation = AdvisoryOpinion.model_fields["recommendation"].annotation
    values = set()
    for arg in typing.get_args(annotation):
        if typing.get_origin(arg) is typing.Literal:
            values.update(typing.get_args(arg))
        elif arg is not type(None):
            values.update(typing.get_args(arg))
    assert values == {"CONCUR", "REDUCE", "REJECT"}, values
    for forbidden in ("APPROVE", "INCREASE", "UPSIZE", "ALLOW", "OVERRIDE"):
        assert forbidden not in values


def test_fail_closed_flags_cannot_be_switched_off():
    """Hard Rule E2: the contract has no representation for 'treat missing data as safe'."""
    with pytest.raises(ValidationError):
        FailClosed(on_missing_market_data=False)
    with pytest.raises(ValidationError):
        FailClosed(on_missing_portfolio_state=False)
    with pytest.raises(ValidationError):
        FailClosed(on_engine_degraded=False)
    assert FailClosed().on_missing_market_data is True


def test_environment_cannot_be_live_at_the_type_level():
    with pytest.raises(ValidationError):
        BrokerRef(name="alpaca", environment="live")
    assert BrokerRef(name="alpaca", environment="paper").environment == "paper"


def test_money_fields_reject_json_numbers():
    """Hard Rule A6: a float or int in a money field is an error, not a coercion."""
    base = dict(
        leg_index=0,
        side="buy",
        contract_type=None,
        strike=None,
        expiry=None,
        order_type="limit",
        limit_price="2.40",
    )
    with pytest.raises(ValidationError):
        Leg(**{**base, "quantity": 10})
    with pytest.raises(ValidationError):
        Leg(**{**base, "quantity": 10.0})
    assert Leg(**{**base, "quantity": "10"}).quantity == "10"


# ----------------------------------------------------------------------------------------------
# Taxonomies
# ----------------------------------------------------------------------------------------------
def test_reason_code_enum_matches_the_json_taxonomy():
    catalogue = json.loads((CONTRACTS_DIR / "reason_codes.json").read_text(encoding="utf-8"))
    assert set(catalogue["codes"]) == {member.value for member in ReasonCode}
    for code, info in catalogue["codes"].items():
        assert info["default_severity"] in {"blocking", "warning", "info"}, code
        assert info["description"].strip(), code
        assert info["category"].strip(), code


def test_error_codes_have_safe_generic_messages():
    catalogue = json.loads((CONTRACTS_DIR / "error_codes.json").read_text(encoding="utf-8"))
    codes = catalogue["codes"] if "codes" in catalogue else catalogue
    assert codes, "error taxonomy is empty"
    for code, info in codes.items():
        status = info["http_status"]
        assert 400 <= status <= 599, (code, status)
        message = info["message"]
        # A client-facing message must not leak internals (Sweep 3).
        lowered = message.lower()
        for leak in ("traceback", "sqlite", "postgres", "select ", "c:\\", "/home/", "stack"):
            assert leak not in lowered, (code, message)


# ----------------------------------------------------------------------------------------------
# Redaction and identifiers
# ----------------------------------------------------------------------------------------------
def test_redaction_is_recursive_and_case_insensitive():
    payload = {
        "API_KEY": "secret-value",
        "nested": {"Authorization": "Bearer abc", "safe": "keep"},
        "headers": [{"X-Api-Key": "abc"}, {"cookie": "s=1"}],
        "list_of_lists": [[{"password": "p"}]],
        "authorization_hash": "a" * 64,
        "count": 3,
    }
    cleaned = redact(payload)
    flat = canonical_json(cleaned)
    for leak in ("secret-value", "Bearer abc", "s=1"):
        assert leak not in flat, flat
    assert cleaned["nested"]["safe"] == "keep"
    assert cleaned["count"] == 3
    # Exempt: a hash of an authorization is not a credential.
    assert cleaned["authorization_hash"] == "a" * 64


def test_uuid7_is_well_formed_and_time_ordered():
    values = [uuid7() for _ in range(1000)]
    assert len(set(values)) == 1000, "uuid7 produced a collision"
    for value in values[:20]:
        assert len(value) == 36 and value.count("-") == 4, value
        assert value[14] == "7", f"version nibble is not 7: {value}"
        assert value[19] in "89ab", f"variant nibble is wrong: {value}"
    assert values == sorted(values), "uuid7 values are not monotonically ordered"


def test_sha256_hex_matches_a_known_digest():
    assert sha256_hex("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_hex(canonical_json({"b": "1.50", "a": 1, "nested": {"z": True, "y": None}})) == (
        "41e883160fa7262424b2a2580c2e2db06c491405e1d6e92dda8e0b800694577b"
    )
