"""Decimal-preserving document loading for policies (Hard Rule A6).

PyYAML's default resolver turns ``10000.00`` into a C double before any validator runs, so a policy
written the way the Master Plan writes it would already have lost precision by the time the contract
saw it. This module removes that possibility rather than checking for it afterwards:

* the loader's implicit resolver for the binary-fraction tag is deleted, so such scalars stay strings;
* the integer resolver is replaced with a strict decimal-integer pattern, so YAML's sexagesimal form
  (``13:30`` -> ``810``) and its octal/hex forms cannot silently reinterpret a policy value;
* the timestamp resolver is deleted, so a date stays the string the author wrote;
* the explicit binary-fraction tag has a constructor that refuses to build anything at all;
* JSON documents are parsed with a hook that keeps a fractional literal as its exact source text.

What comes out is therefore JSON data made only of mappings, lists, strings, integers, booleans and
null. ``conform`` then converts the integers that belong in DecimalStr fields into their exact string
spelling, which is the one place a policy author's ``max_quantity: 20`` becomes ``"20"``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import UnionType
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel

from mizan.contracts import Policy, ReasonCode
from mizan.contracts.errors import PolicyError

BINARY_FRACTION_TAG = "tag:yaml.org,2002:float"
INTEGER_TAG = "tag:yaml.org,2002:int"
TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
DROPPED_TAGS: frozenset[str] = frozenset({BINARY_FRACTION_TAG, INTEGER_TAG, TIMESTAMP_TAG})
STRICT_INTEGER_PATTERN = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_INTEGER_FIRST_CHARACTERS = "-+0123456789"

__all__ = ["DecimalPreservingLoader", "conform", "parse_document"]


def _refuse_binary_scalar(loader: Any, node: Any) -> Any:
    raise PolicyError(
        message="policy documents must not carry binary fractional scalars; use a decimal string",
        reason_codes=[ReasonCode.POLICY_INVALID],
        detail=f"binary scalar tag at line {getattr(node.start_mark, 'line', -1) + 1}",
    )


class DecimalPreservingLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that cannot construct a binary fractional value, by construction."""


DecimalPreservingLoader.yaml_implicit_resolvers = {
    character: [(tag, pattern) for tag, pattern in mappings if tag not in DROPPED_TAGS]
    for character, mappings in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
DecimalPreservingLoader.add_implicit_resolver(
    INTEGER_TAG, STRICT_INTEGER_PATTERN, list(_INTEGER_FIRST_CHARACTERS)
)
DecimalPreservingLoader.add_constructor(BINARY_FRACTION_TAG, _refuse_binary_scalar)


def parse_document(text: str, *, fmt: str = "yaml") -> Any:
    """Parse ``text`` into plain JSON data without ever constructing a binary fractional value."""
    if not isinstance(text, str):
        raise PolicyError(
            message="a policy document must be text",
            reason_codes=[ReasonCode.POLICY_INVALID],
            detail=f"got {type(text).__name__}",
        )
    try:
        if fmt == "yaml":
            return yaml.load(text, Loader=DecimalPreservingLoader)  # noqa: S506 - hardened subclass
        if fmt == "json":
            # ``parse_float`` receives the literal source text, so "10000.00" stays exactly that.
            return json.loads(text, parse_float=str)
    except PolicyError:
        raise
    except (yaml.YAMLError, ValueError) as exc:
        raise PolicyError(
            message="the policy document could not be parsed",
            reason_codes=[ReasonCode.SCHEMA_INVALID],
            detail=str(exc),
        ) from exc
    raise PolicyError(
        message="unsupported policy document format",
        reason_codes=[ReasonCode.POLICY_INVALID],
        detail=f"fmt={fmt!r}",
    )


def _unwrap(annotation: Any) -> Any:
    """Strip ``Annotated[...]`` wrappers; the contract's scalar types are all annotated aliases."""
    while hasattr(annotation, "__metadata__"):
        annotation = get_args(annotation)[0]
    return annotation


def _members(annotation: Any) -> tuple[Any, ...]:
    """The concrete alternatives of a field annotation, ``None`` removed and aliases unwrapped."""
    annotation = _unwrap(annotation)
    if get_origin(annotation) in (Union, UnionType):
        return tuple(_unwrap(member) for member in get_args(annotation) if member is not type(None))
    return (annotation,)


def _expects_text(annotation: Any) -> bool:
    return any(member is str for member in _members(annotation))


def conform(value: Any, annotation: Any) -> Any:
    """Shape a parsed document to what the contract expects, without arithmetic.

    The only conversion is an exact one: an integer that landed in a DecimalStr field becomes its
    decimal spelling (``20`` -> ``"20"``). Nothing else is coerced; a value the contract would reject
    is left alone so that validation, not this function, reports it.
    """
    for member in _members(annotation):
        if isinstance(member, type) and issubclass(member, BaseModel) and isinstance(value, Mapping):
            fields = member.model_fields
            return {
                key: conform(item, fields[key].annotation) if key in fields else item
                for key, item in value.items()
            }
        origin = get_origin(member)
        if origin is dict and isinstance(value, Mapping):
            item_annotation = get_args(member)[1]
            return {key: conform(item, item_annotation) for key, item in value.items()}
        if origin is list and isinstance(value, list):
            item_annotation = get_args(member)[0]
            return [conform(item, item_annotation) for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and _expects_text(annotation):
        return str(value)
    return value


def conform_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``conform`` a whole policy payload against the ``Policy`` contract."""
    conformed = conform(dict(payload), Policy)
    if not isinstance(conformed, dict):  # pragma: no cover - conform preserves mappings
        raise PolicyError(
            message="a policy document must be a mapping",
            reason_codes=[ReasonCode.POLICY_INVALID],
        )
    return conformed
