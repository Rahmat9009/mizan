"""Scalar contract types: validated, normalised strings.

Every money, price, quantity, ratio, greek and notional value in Mizan is a ``DecimalStr`` -- a JSON *string*
matching ``^-?(0|[1-9]\\d*)(\\.\\d+)?$`` and normalised (no exponent, no trailing zeros, ``-0`` -> ``0``).
JSON numbers are never accepted where a DecimalStr is required (Hard Rule A6); the models are strict, so an
``int`` or a binary fraction in a money field is a validation error, not a coercion.

Timestamps are ``Rfc3339`` strings in the canonical form ``YYYY-MM-DDTHH:MM:SS.ssssssZ`` (UTC, six fractional
digits). Other RFC 3339 forms are accepted on input and normalised; sub-microsecond digits must be zero.

Nothing in this module reads a clock, performs I/O, or touches binary floating point.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, StringConstraints

# --------------------------------------------------------------------------------------------------------------------
# Patterns (the JSON Schemas under contracts/ carry the same expressions verbatim)
# --------------------------------------------------------------------------------------------------------------------

DECIMAL_STR_PATTERN = r"^-?(0|[1-9]\d*)(\.\d+)?$"
RFC3339_CANONICAL_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
RFC3339_INPUT_PATTERN = (
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})$"
)
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.-]{0,15}$"
OCC_SYMBOL_PATTERN = r"^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$"
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"
POLICY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

_DECIMAL_STR_RE = re.compile(DECIMAL_STR_PATTERN)
_RFC3339_INPUT_RE = re.compile(RFC3339_INPUT_PATTERN)

SCHEMA_VERSION = "1.0.0"
SchemaVersion = Literal["1.0.0"]
Environment = Literal["paper"]

# --------------------------------------------------------------------------------------------------------------------
# Decimal strings
# --------------------------------------------------------------------------------------------------------------------


def dec(value: str) -> Decimal:
    """Parse a DecimalStr into a ``Decimal``. Only strings matching the DecimalStr pattern are accepted."""
    if not isinstance(value, str):
        raise TypeError(f"dec() expects a DecimalStr, got {type(value).__name__}")
    if _DECIMAL_STR_RE.fullmatch(value) is None:
        raise ValueError(f"not a DecimalStr: {value!r}")
    return Decimal(value)


def dstr(value: Decimal) -> str:
    """Format a ``Decimal`` as a normalised DecimalStr (no exponent, no trailing zeros, ``-0`` -> ``0``)."""
    if not isinstance(value, Decimal):
        raise TypeError(f"dstr() expects a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError("DecimalStr cannot represent NaN or Infinity")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def normalize_decimal_str(value: str) -> str:
    """Validate a DecimalStr and return its canonical spelling (``"2.40"`` -> ``"2.4"``, ``"-0.0"`` -> ``"0"``)."""
    return dstr(dec(value))


def _positive(value: str) -> str:
    if dec(value) <= 0:
        raise ValueError(f"must be > 0, got {value!r}")
    return value


def _non_negative(value: str) -> str:
    if dec(value) < 0:
        raise ValueError(f"must be >= 0, got {value!r}")
    return value


def _ratio(value: str) -> str:
    number = dec(value)
    if number < 0 or number > 1:
        raise ValueError(f"must be between 0 and 1 inclusive, got {value!r}")
    return value


DecimalStr = Annotated[
    str,
    StringConstraints(strict=True, pattern=DECIMAL_STR_PATTERN),
    AfterValidator(normalize_decimal_str),
]
PositiveDecimalStr = Annotated[DecimalStr, AfterValidator(_positive)]
NonNegativeDecimalStr = Annotated[DecimalStr, AfterValidator(_non_negative)]
RatioStr = Annotated[DecimalStr, AfterValidator(_ratio)]

# --------------------------------------------------------------------------------------------------------------------
# Timestamps and dates
# --------------------------------------------------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    """Parse an RFC 3339 timestamp into a timezone-aware ``datetime`` in UTC.

    Accepts ``Z``/``z`` or a numeric offset, one to nine fractional digits (digits beyond the sixth must be zero:
    nothing is silently truncated), and a lowercase ``t`` separator. Leap seconds are not representable and are
    rejected.
    """
    if not isinstance(value, str):
        raise TypeError(f"parse_ts() expects a string, got {type(value).__name__}")
    match = _RFC3339_INPUT_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"not an RFC 3339 timestamp: {value!r}")
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if fraction is None:
        microsecond = 0
    else:
        if len(fraction) > 6 and fraction[6:].strip("0"):
            raise ValueError(f"sub-microsecond precision cannot be represented: {value!r}")
        microsecond = int(fraction[:6].ljust(6, "0"))
    if offset in ("Z", "z"):
        tzinfo = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        offset_hours, offset_minutes = int(offset[1:3]), int(offset[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"invalid UTC offset in {value!r}")
        tzinfo = timezone(sign * timedelta(hours=offset_hours, minutes=offset_minutes))
    parsed = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), microsecond, tzinfo=tzinfo
    )
    return parsed.astimezone(timezone.utc)


def format_ts(value: datetime) -> str:
    """Format a timezone-aware ``datetime`` in the canonical form ``YYYY-MM-DDTHH:MM:SS.ssssssZ``."""
    if not isinstance(value, datetime):
        raise TypeError(f"format_ts() expects a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("format_ts() requires a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_ts(value: str) -> str:
    """Validate an RFC 3339 timestamp and return its canonical UTC spelling."""
    return format_ts(parse_ts(value))


def _check_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a calendar date: {value!r}") from exc
    return value


Rfc3339 = Annotated[
    str,
    StringConstraints(strict=True, pattern=RFC3339_INPUT_PATTERN),
    AfterValidator(normalize_ts),
]
DateStr = Annotated[str, StringConstraints(strict=True, pattern=DATE_PATTERN), AfterValidator(_check_date)]

# --------------------------------------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------------------------------------

TenantId = Annotated[str, StringConstraints(strict=True, pattern=TENANT_ID_PATTERN)]
AgentId = Annotated[str, StringConstraints(strict=True, pattern=AGENT_ID_PATTERN)]
Symbol = Annotated[str, StringConstraints(strict=True, pattern=SYMBOL_PATTERN)]
OccSymbol = Annotated[str, StringConstraints(strict=True, pattern=OCC_SYMBOL_PATTERN)]
Sha256Hex = Annotated[str, StringConstraints(strict=True, pattern=SHA256_HEX_PATTERN)]
PolicyId = Annotated[str, StringConstraints(strict=True, pattern=POLICY_ID_PATTERN)]
SemVer = Annotated[str, StringConstraints(strict=True, pattern=SEMVER_PATTERN)]
Uuid7Str = Annotated[str, StringConstraints(strict=True, pattern=UUID7_PATTERN)]
HHMM = Annotated[str, StringConstraints(strict=True, pattern=HHMM_PATTERN)]

# Free-text with bounds. ``NonEmptyStr`` is for identifiers, refs and source labels; ``Text`` for messages.
NonEmptyStr = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
Text = Annotated[str, StringConstraints(strict=True, max_length=4000)]


def _must_be_bool(value: object) -> object:
    # ``Literal[True]`` alone accepts the integer 1 (Python equality); the contract wants a JSON boolean only.
    if not isinstance(value, bool):
        raise ValueError("must be the JSON boolean true")
    return value


StrictTrue = Annotated[Literal[True], BeforeValidator(_must_be_bool)]
StrictBool = Annotated[bool, BeforeValidator(_must_be_bool)]

__all__ = [
    "AGENT_ID_PATTERN",
    "AgentId",
    "DATE_PATTERN",
    "DECIMAL_STR_PATTERN",
    "DateStr",
    "DecimalStr",
    "Environment",
    "HHMM",
    "HHMM_PATTERN",
    "NonEmptyStr",
    "NonNegativeDecimalStr",
    "OCC_SYMBOL_PATTERN",
    "OccSymbol",
    "POLICY_ID_PATTERN",
    "PolicyId",
    "PositiveDecimalStr",
    "RFC3339_CANONICAL_PATTERN",
    "RFC3339_INPUT_PATTERN",
    "RatioStr",
    "Rfc3339",
    "SCHEMA_VERSION",
    "SEMVER_PATTERN",
    "SHA256_HEX_PATTERN",
    "SYMBOL_PATTERN",
    "SchemaVersion",
    "SemVer",
    "Sha256Hex",
    "StrictBool",
    "StrictTrue",
    "Symbol",
    "TENANT_ID_PATTERN",
    "TenantId",
    "Text",
    "UUID7_PATTERN",
    "Uuid7Str",
    "dec",
    "dstr",
    "format_ts",
    "normalize_decimal_str",
    "normalize_ts",
    "parse_ts",
]
