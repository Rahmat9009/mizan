"""Invariant 16 - Hard Rule B1: paper and live are separate boundaries; no live trading path exists in this code.

Pass criterion: a scan of every .py under mizan/ finds no `paper=False` / `paper = False`, no non-docstring string
constant "live", no `environment` Literal other than ["paper"], and no "api.alpaca.markets" host other than
"paper-api.alpaca.markets" (offenders listed as file:line); every "environment" property in contracts/*.schema.json
is exactly enum ["paper"] (and at least one such property exists); the contracts refuse environment="live";
ExecutionConfig.from_environment() raises LiveTradingForbidden for ALPACA_PAPER in {false, 0, no, "", live} and
when unset, and ExecutionConfig(paper=False) is an error; AlpacaPaperBroker.from_environment() with ALPACA_PAPER=false
raises LiveTradingForbidden before any network access; and the BrokerAdapter Protocol has no cancel/replace/close
method (B4).
"""
from __future__ import annotations

import ast
import re
import socket
import typing

import pytest
from pydantic import ValidationError

from mizan.adapters import AlpacaPaperBroker, BrokerAdapter, OrderRequest
from mizan.contracts import BrokerRef, ExecutionAuthorization
from mizan.contracts.errors import LiveTradingForbidden, MizanError
from mizan.execution import ExecutionConfig

from tests.fixtures import make_authorization
from tests.invariants._support import (
    CONTRACTS_DIR,
    docstring_ids,
    offenders_message,
    parse,
    python_files,
    read_json,
    rel,
)

LIVE_HOST_RE = re.compile(r"(?<!paper-)api\.alpaca\.markets")
PAPER_FALSE_RE = re.compile(r"\bpaper\b\s*(?::[^=\n]*)?=\s*False\b")
FORBIDDEN_ENV_VALUES = ("false", "0", "no", "", "live", "False", "FALSE", "off")
MUTATION_METHODS = (
    "cancel_order", "cancel_orders", "cancel_all_orders", "replace_order", "modify_order",
    "close_position", "close_all_positions", "liquidate",
)


def _literal_values(node: ast.AST) -> list:
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Literal":
        inner = node.slice
        elements = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [e.value for e in elements if isinstance(e, ast.Constant)]
    return []


def _live_offenders(path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if LIVE_HOST_RE.search(line):
            found.append(f"{rel(path)}:{number}: live Alpaca host")
        if PAPER_FALSE_RE.search(line):
            found.append(f"{rel(path)}:{number}: paper=False")
    tree = parse(path)
    prose = docstring_ids(tree)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().lower() == "live" and id(node) not in prose:
                found.append(f"{rel(path)}:{line}: string constant {node.value!r}")
        elif isinstance(node, ast.keyword) and node.arg == "paper":
            if isinstance(node.value, ast.Constant) and node.value.value is False:
                found.append(f"{rel(path)}:{line}: keyword paper=False")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)} | {
                t.attr for t in targets if isinstance(t, ast.Attribute)
            }
            if "paper" in names and isinstance(node.value, ast.Constant) and node.value.value is False:
                found.append(f"{rel(path)}:{line}: assignment paper = False")
            if isinstance(node, ast.AnnAssign) and "environment" in names:
                values = _literal_values(node.annotation)
                if values and values != ["paper"]:
                    found.append(f"{rel(path)}:{line}: environment Literal {values!r}")
    return found


def _environment_schemas(node, path="$"):
    """Yield (json path, subschema) for every property named 'environment' anywhere in a schema."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict) and "environment" in properties:
            yield f"{path}.properties.environment", properties["environment"]
        for key, value in node.items():
            yield from _environment_schemas(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _environment_schemas(value, f"{path}[{index}]")


def _block_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted before the paper check")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _dummy_credentials(monkeypatch):
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.setenv(name, "invariant-dummy")


def test_no_live_trading_path_exists(monkeypatch):
    # static: no live host, no paper=False, no "live" literal anywhere under mizan/
    offenders: list[str] = []
    for path in python_files(""):
        offenders.extend(_live_offenders(path))
    assert not offenders, offenders_message("live-trading artefacts under mizan/", offenders)

    # schemas: every environment property is exactly ["paper"]
    schema_files = sorted(CONTRACTS_DIR.glob("*.schema.json"))
    assert schema_files, f"no schemas found under {CONTRACTS_DIR}"
    seen = 0
    bad: list[str] = []
    for schema_file in schema_files:
        for json_path, subschema in _environment_schemas(read_json(schema_file)):
            seen += 1
            enum = subschema.get("enum")
            const = subschema.get("const")
            if not (enum == ["paper"] or (enum is None and const == "paper")):
                bad.append(f"{schema_file.name}:{json_path}: {subschema}")
    assert seen >= 1, "no contract schema declares an environment property"
    assert not bad, offenders_message("environment enums that are not exactly [\"paper\"]", bad)

    # contracts: "live" is unrepresentable
    with pytest.raises(ValidationError):
        BrokerRef(name="alpaca", environment="live")
    auth_payload = make_authorization().model_dump(mode="json")
    with pytest.raises(ValidationError):
        ExecutionAuthorization.model_validate({**auth_payload, "environment": "live"})
    with pytest.raises(ValidationError):
        OrderRequest(
            client_order_id="mz1-test",
            symbol="SPY",
            asset_class="equity",
            intent="open",
            legs=[],
            environment="live",
        )

    # configuration: anything but ALPACA_PAPER=true is forbidden
    monkeypatch.setenv("MIZAN_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("MIZAN_EXECUTION_DRY_RUN", "true")
    for value in FORBIDDEN_ENV_VALUES:
        monkeypatch.setenv("ALPACA_PAPER", value)
        with pytest.raises(LiveTradingForbidden):
            ExecutionConfig.from_environment()
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()
    monkeypatch.setenv("ALPACA_PAPER", "true")
    assert ExecutionConfig.from_environment().paper is True

    with pytest.raises((TypeError, ValueError, ValidationError, MizanError)):
        ExecutionConfig(paper=False)
    assert ExecutionConfig().paper is True

    # broker adapter: the paper check happens before any network access
    _block_network(monkeypatch)
    _dummy_credentials(monkeypatch)
    for value in FORBIDDEN_ENV_VALUES:
        monkeypatch.setenv("ALPACA_PAPER", value)
        with pytest.raises(LiveTradingForbidden):
            AlpacaPaperBroker.from_environment()
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    with pytest.raises(LiveTradingForbidden):
        AlpacaPaperBroker.from_environment()


def test_environment_literals_are_paper_only_at_the_type_level():
    for model, field in ((BrokerRef, "environment"), (ExecutionAuthorization, "environment"),
                         (OrderRequest, "environment")):
        annotation = model.model_fields[field].annotation
        assert typing.get_origin(annotation) is typing.Literal, (model.__name__, annotation)
        assert set(typing.get_args(annotation)) == {"paper"}, (model.__name__, annotation)


def test_broker_adapter_has_no_cancel_or_replace_path():
    for method in MUTATION_METHODS:
        assert not hasattr(BrokerAdapter, method), method
    offenders: list[str] = []
    for path in python_files("adapters"):
        for node in ast.walk(parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in MUTATION_METHODS:
                offenders.append(f"{rel(path)}:{node.lineno}: def {node.name}")
    assert not offenders, offenders_message("cancel/replace automation in adapters (B4)", offenders)
