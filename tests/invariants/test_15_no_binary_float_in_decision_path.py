"""Invariant 15 - Hard Rule A6: money and quantity are decimal/fixed-point; never binary floats in the decision path.

Pass criterion: an AST scan of every .py file under mizan/contracts, mizan/policy, mizan/risk, mizan/governor,
mizan/authorization, mizan/audit and mizan/replay finds no Name or Attribute `float`, no float literal, no `math`
import and no "float" string annotation - every offender is listed as file:line in the assertion message. At
runtime the contracts reject JSON numbers in DecimalStr fields (TradeProposal leg quantity, Policy max_notional,
Quote price - as int and as float, in Python and in JSON mode), canonical_json raises TypeError for float, NaN and
Infinity, and DECIMAL_CONTEXT traps InvalidOperation/DivisionByZero/Overflow at precision 28.
"""
from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow

import pytest
from pydantic import ValidationError

from mizan.contracts import Policy, Quote, TradeProposal
from mizan.contracts.canonical import DECIMAL_CONTEXT, canonical_json, policy_hash_for, proposal_id_for
from mizan.contracts.types import dec, dstr

from tests.fixtures import FIXED_NOW_STR, make_policy, make_proposal
from tests.invariants._support import docstring_ids, offenders_message, parse, python_files, rel

DECISION_PATH = ("contracts", "policy", "risk", "governor", "authorization", "audit", "replay")


def _float_offenders(path) -> list[str]:
    tree = parse(path)
    prose = docstring_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Name) and node.id == "float":
            found.append(f"{rel(path)}:{line}: name `float`")
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            found.append(f"{rel(path)}:{line}: attribute `.float`")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append(f"{rel(path)}:{line}: float literal {node.value!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, complex):
            found.append(f"{rel(path)}:{line}: complex literal {node.value!r}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == "float"
            and id(node) not in prose
        ):
            found.append(f"{rel(path)}:{line}: string annotation/reference \"float\"")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "math" or alias.name.startswith("math."):
                    found.append(f"{rel(path)}:{line}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "math" or node.module.startswith("math."):
                found.append(f"{rel(path)}:{line}: from {node.module} import ...")
    return found


def test_no_binary_float_in_decision_path():
    files = python_files(*DECISION_PATH)
    assert files, "no files scanned"
    offenders: list[str] = []
    for path in files:
        offenders.extend(_float_offenders(path))
    assert not offenders, offenders_message("binary float usage in the decision path", offenders)

    # runtime: a JSON number in a money/quantity field is rejected, whatever its type
    proposal_payload = make_proposal().model_dump(mode="json")
    as_int = {**proposal_payload}
    as_int["legs"] = [{**leg, "quantity": 10} for leg in proposal_payload["legs"]]
    as_int["proposal_id"] = proposal_id_for(as_int)  # hash consistent -> the failure is the type
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(as_int)
    as_float = {**proposal_payload}
    as_float["legs"] = [{**leg, "quantity": 10.0} for leg in proposal_payload["legs"]]
    with pytest.raises((ValidationError, TypeError)):
        TradeProposal.model_validate(as_float)
    with pytest.raises(ValidationError):
        TradeProposal.model_validate_json(canonical_json(as_int))

    policy_payload = make_policy().model_dump(mode="json")
    policy_number = {**policy_payload, "order": {**policy_payload["order"], "max_notional": 10000}}
    policy_number["policy_hash"] = policy_hash_for(policy_number)
    with pytest.raises(ValidationError):
        Policy.model_validate(policy_number)
    policy_float = {**policy_payload, "order": {**policy_payload["order"], "max_notional": 10000.0}}
    with pytest.raises((ValidationError, TypeError)):
        Policy.model_validate(policy_float)

    for price in (100, 100.5):
        with pytest.raises(ValidationError):
            Quote(symbol="SPY", price=price, bid=None, ask=None, as_of=FIXED_NOW_STR, source="test")


def test_canonical_json_refuses_floats_nan_and_infinity():
    for value in (1.5, float("nan"), float("inf"), float("-inf"), {"x": 0.1}, [1, 2.0]):
        with pytest.raises(TypeError):
            canonical_json(value)
    assert canonical_json({"b": "1.5", "a": 1}) == '{"a":1,"b":"1.5"}'
    assert canonical_json(Decimal("1.5")) == '"1.5"'


def test_decimal_context_traps_and_normalisation():
    assert DECIMAL_CONTEXT.prec == 28
    for trap in (InvalidOperation, DivisionByZero, Overflow):
        assert DECIMAL_CONTEXT.traps[trap] is True
    assert dstr(dec("0.1") + dec("0.2")) == "0.3"
    assert dstr(dec("10.50")) == "10.5"
    assert dstr(Decimal("-0")) == "0"
    assert dstr(Decimal("1E+2")) == "100"
