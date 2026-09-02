"""Reason codes, generated at import time from ``contracts/reason_codes.json`` (the frozen source of truth).

Every REJECT or REDUCE carries at least one code (Hard Rule A4). Lists of reason codes in contract objects are
sorted and de-duplicated. The enum is a ``str`` subclass, so a member compares equal to, hashes like, and
serialises as its code string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
REASON_CODES_PATH = CONTRACTS_DIR / "reason_codes.json"

REASON_CATEGORIES: tuple[str, ...] = (
    "SCHEMA",
    "POLICY",
    "TENANT",
    "MARKET_DATA",
    "PORTFOLIO",
    "ORDER",
    "OPTIONS",
    "ADVISORY",
    "AUTHORIZATION",
    "EXECUTION",
    "ENGINE",
    "AUDIT",
    "PATH",
    "AGGREGATE",
    "AGENT",
    "LIQUIDITY",
    "TIME",
    "TAIL",
    "FACTOR",
    "CONTROL",
)
SEVERITIES: tuple[str, ...] = ("blocking", "warning", "info")


@dataclass(frozen=True)
class ReasonCodeInfo:
    code: str
    category: str
    default_severity: str
    description: str
    check_id: str | None


def _load() -> tuple[str, dict[str, ReasonCodeInfo]]:
    if not REASON_CODES_PATH.is_file():
        raise ImportError(f"contracts/reason_codes.json not found at {REASON_CODES_PATH}")
    raw = json.loads(REASON_CODES_PATH.read_text(encoding="utf-8"))
    if set(raw) != {"version", "codes"}:
        raise ImportError("reason_codes.json must have exactly the keys 'version' and 'codes'")
    version = raw["version"]
    infos: dict[str, ReasonCodeInfo] = {}
    for code, entry in raw["codes"].items():
        if not isinstance(code, str) or not code or code.upper() != code:
            raise ImportError(f"reason code {code!r} must be an upper-case identifier")
        if set(entry) != {"category", "default_severity", "description", "check_id"}:
            raise ImportError(f"reason code {code}: unexpected keys {sorted(entry)}")
        if entry["category"] not in REASON_CATEGORIES:
            raise ImportError(f"reason code {code}: unknown category {entry['category']!r}")
        if entry["default_severity"] not in SEVERITIES:
            raise ImportError(f"reason code {code}: unknown severity {entry['default_severity']!r}")
        if entry["check_id"] is not None and not isinstance(entry["check_id"], str):
            raise ImportError(f"reason code {code}: check_id must be a string or null")
        infos[code] = ReasonCodeInfo(
            code=code,
            category=entry["category"],
            default_severity=entry["default_severity"],
            description=entry["description"],
            check_id=entry["check_id"],
        )
    return version, infos


REASON_CODE_VERSION, _INFO_BY_STRING = _load()

ReasonCode = StrEnum("ReasonCode", {code: code for code in _INFO_BY_STRING}, module=__name__)
ReasonCode.__doc__ = "Machine-readable reason codes from contracts/reason_codes.json (version REASON_CODE_VERSION)."

REASON_CODE_INFO: dict[ReasonCode, ReasonCodeInfo] = {ReasonCode(code): info for code, info in _INFO_BY_STRING.items()}


def reason_code_info(code: ReasonCode | str) -> ReasonCodeInfo:
    """Category, default severity, description and originating check for a code."""
    return REASON_CODE_INFO[ReasonCode(code)]


def coerce_reason_code(value: Any) -> Any:
    """Accept a ``ReasonCode`` member or its exact string; reject anything else (used before strict validation)."""
    if isinstance(value, ReasonCode):
        return value
    if isinstance(value, str):
        try:
            return ReasonCode(value)
        except ValueError as exc:
            raise ValueError(f"unknown reason code {value!r}") from exc
    raise ValueError(f"reason code must be a string, got {type(value).__name__}")


def _sorted_unique(codes: list[ReasonCode]) -> list[ReasonCode]:
    expected = sorted(set(codes), key=lambda code: code.value)
    if list(codes) != expected:
        raise ValueError("reason codes must be sorted and de-duplicated")
    return codes


def sorted_reason_codes(codes: Any) -> list[ReasonCode]:
    """Normalise an iterable of codes/strings into the sorted, de-duplicated list form the contracts require."""
    return sorted({coerce_reason_code(code) for code in codes}, key=lambda code: code.value)


ReasonCodeField = Annotated[ReasonCode, BeforeValidator(coerce_reason_code)]
ReasonCodeList = Annotated[list[ReasonCodeField], AfterValidator(_sorted_unique)]

__all__ = [
    "CONTRACTS_DIR",
    "REASON_CATEGORIES",
    "REASON_CODES_PATH",
    "REASON_CODE_INFO",
    "REASON_CODE_VERSION",
    "SEVERITIES",
    "ReasonCode",
    "ReasonCodeField",
    "ReasonCodeInfo",
    "ReasonCodeList",
    "coerce_reason_code",
    "reason_code_info",
    "sorted_reason_codes",
]
