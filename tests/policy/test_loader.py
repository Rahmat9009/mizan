"""The loader's one job: a policy's numbers survive the parser exactly as they were written.

PyYAML's default resolver turns ``10000.00`` into a C double, and a double cannot hold most decimal
money values. That loss happens before any validator runs, so no amount of downstream checking can
recover it -- which is why the parser itself is the place this is fixed, and why these tests probe the
parser rather than the contract.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from mizan.contracts.canonical import canonical_json, policy_hash_for
from mizan.contracts.errors import PolicyError
from mizan.policy import load_policy, validate_policy
from mizan.policy.loader import DecimalPreservingLoader, parse_document
from tests.fixtures import make_policy

MINIMAL = """
policy_id: loader-test
policy_version: "1.0.0"
tenant_id: tenant-a
order:
  max_notional: {max_notional}
  max_quantity: 20
  max_legs: 4
portfolio:
  max_single_symbol_pct: 0.15
  max_sector_concentration_pct: 0.25
  max_drawdown_pct: 0.20
  max_buying_power_utilization: 0.80
options: null
restricted:
  symbols: []
  strategies: []
checks:
  duplicate_order: {{enabled: true, severity: blocking, window_seconds: 60}}
advisory:
  enabled: true
  profile: standard_advisory
  authority_ceiling: reduce_or_reject
authorization:
  ttl_seconds: 15
fail_closed:
  on_missing_market_data: true
  on_missing_portfolio_state: true
  on_engine_degraded: true
  on_advisory_unavailable: false
"""


def _document(max_notional: str = "10000.00") -> str:
    return MINIMAL.format(max_notional=max_notional)


def _walk(value, path="$"):
    """Every scalar in a loaded structure, with the path it was found at."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, value


def test_an_unquoted_decimal_keeps_every_digit_a_binary_double_would_lose():
    """1.0000000000000001 is 1.0 as a C double; as a decimal string it is itself."""
    policy = load_policy(_document("1.0000000000000001"))
    assert policy.order.max_notional == "1.0000000000000001"
    assert Decimal(policy.order.max_notional) != Decimal(1)


def test_unquoted_and_quoted_decimals_load_identically():
    unquoted = load_policy(_document("10000.00"))
    quoted = load_policy(_document('"10000.00"'))
    assert unquoted.order.max_notional == quoted.order.max_notional == "10000"
    assert unquoted.policy_hash == quoted.policy_hash


def test_no_value_anywhere_in_a_loaded_policy_is_a_binary_fraction():
    policy = load_policy(_document())
    offenders = [
        f"{path}: {type(value).__name__}"
        for path, value in _walk(policy.model_dump(mode="json"))
        if value is not None and not isinstance(value, (str, bool, int))
    ]
    assert not offenders, offenders


def test_the_parser_itself_never_produces_a_binary_fraction():
    raw = parse_document(_document("0.1"))
    offenders = [
        f"{path}: {type(value).__name__}"
        for path, value in _walk(raw)
        if value is not None and not isinstance(value, (str, bool, int))
    ]
    assert not offenders, offenders
    assert raw["portfolio"]["max_single_symbol_pct"] == "0.15"


def test_integers_stay_integers_where_the_contract_wants_a_count():
    policy = load_policy(_document())
    assert policy.order.max_legs == 4
    assert policy.authorization.ttl_seconds == 15
    assert policy.check_config("duplicate_order").window_seconds == 60


def test_an_unquoted_integer_in_a_money_field_becomes_its_exact_decimal_string():
    policy = load_policy(_document("10000"))
    assert policy.order.max_notional == "10000"
    assert policy.order.max_quantity == "20"


def test_a_clock_like_scalar_is_not_reinterpreted_as_sexagesimal():
    """Plain PyYAML reads 13:30 as the integer 810. A policy's active hours are not minutes past midnight."""
    raw = parse_document("active_hours_utc: [13:30, 20:00]")
    assert raw == {"active_hours_utc": ["13:30", "20:00"]}


def test_octal_and_hexadecimal_forms_are_not_reinterpreted():
    raw = parse_document("a: 0o17\nb: 0x1f\nc: 010")
    assert raw == {"a": "0o17", "b": "0x1f", "c": "010"}


def test_a_date_stays_the_string_it_was_written_as():
    raw = parse_document("expiry: 2026-09-25")
    assert raw == {"expiry": "2026-09-25"}


def test_an_explicit_binary_fraction_tag_is_refused():
    with pytest.raises(PolicyError) as caught:
        parse_document("value: !!float 1.5")
    assert "POLICY_INVALID" in str(caught.value)


@pytest.mark.parametrize("literal", [".inf", "-.inf", ".nan"])
def test_infinity_and_not_a_number_cannot_reach_a_money_field(literal):
    with pytest.raises(PolicyError):
        load_policy(_document(literal))


def test_the_loader_class_is_a_hardened_subclass_not_a_patched_safe_loader():
    import yaml

    assert issubclass(DecimalPreservingLoader, yaml.SafeLoader)
    assert "tag:yaml.org,2002:float" in yaml.SafeLoader.yaml_constructors  # the stock loader is untouched
    resolvers = {
        tag
        for mappings in DecimalPreservingLoader.yaml_implicit_resolvers.values()
        for tag, _pattern in mappings
    }
    assert "tag:yaml.org,2002:float" not in resolvers
    assert "tag:yaml.org,2002:timestamp" not in resolvers


def test_json_documents_keep_their_fractional_literals_exactly():
    payload = json.loads(canonical_json(make_policy()))
    payload["order"]["max_notional"] = 10000.5  # a JSON number, the shape a naive exporter emits
    payload["checks"]["assignment_risk"] = {"enabled": False, "severity": "blocking"}
    payload["checks"]["pin_risk"] = {"enabled": False, "severity": "blocking"}
    del payload["policy_hash"]
    document = json.dumps(payload)
    policy = load_policy(document, fmt="json")
    assert policy.order.max_notional == "10000.5"


def test_an_unsupported_format_is_refused():
    with pytest.raises(PolicyError):
        load_policy(_document(), fmt="toml")  # type: ignore[arg-type]


def test_a_document_that_is_not_a_mapping_is_refused():
    with pytest.raises(PolicyError):
        load_policy("- one\n- two\n")


def test_unparseable_yaml_is_refused_with_a_schema_code():
    with pytest.raises(PolicyError) as caught:
        load_policy("policy_id: [unclosed\n")
    assert "SCHEMA_INVALID" in str(caught.value)


def test_the_hash_is_computed_when_the_document_does_not_carry_one():
    policy = load_policy(_document())
    assert policy.policy_hash == policy_hash_for(policy)


def test_a_matching_hash_in_the_document_is_accepted():
    policy = load_policy(_document())
    document = _document() + f"policy_hash: {policy.policy_hash}\n"
    assert load_policy(document).policy_hash == policy.policy_hash


def test_a_mismatched_hash_is_refused_with_policy_hash_mismatch():
    document = _document() + f"policy_hash: {'a' * 64}\n"
    with pytest.raises(PolicyError) as caught:
        load_policy(document)
    assert "POLICY_HASH_MISMATCH" in str(caught.value)


def test_editing_one_number_changes_the_hash():
    first = load_policy(_document("10000.00"))
    second = load_policy(_document("10000.01"))
    assert first.policy_hash != second.policy_hash


def test_a_payload_that_already_carries_normalised_values_round_trips():
    original = make_policy()
    payload = json.loads(canonical_json(original))
    payload.pop("policy_hash")
    payload["checks"]["assignment_risk"] = {"enabled": False, "severity": "blocking"}
    payload["checks"]["pin_risk"] = {"enabled": False, "severity": "blocking"}
    reloaded = validate_policy(payload)
    assert reloaded.order.max_notional == original.order.max_notional
    assert reloaded.policy_id == original.policy_id
