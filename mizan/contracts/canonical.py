"""Canonical JSON, hashing, identifiers and redaction.

This module is the executable form of ``contracts/CANONICAL.md``. A customer must be able to re-derive every
hash in a decision record with no Mizan code (Hard Rule A5), so the rules here are deliberately small:

* canonical JSON = ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` over a structure
  containing only objects, arrays, strings, integers, booleans and null (binary floats are a ``TypeError``);
* object hash = lowercase hex SHA-256 of the UTF-8 bytes of the canonical JSON;
* derived identifiers hash the object's canonical form *without* the field that carries the identifier.

Nothing here touches binary floating point.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import secrets
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from enum import Enum
from typing import Any

from pydantic import BaseModel

from mizan import __version__
from mizan.contracts.types import normalize_decimal_str

ZERO_HASH = "0" * 64
ENGINE_VERSION = f"mizan-core/{__version__}"
DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
IDEMPOTENCY_KEY_PREFIX = "mz1-"

REDACTED = "[REDACTED]"
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "apikey",
    "api_key",
    "secret",
    "token",
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "authorization",
    "www_authenticate",
    "bearer",
    "credential",
    "credentials",
    "header",
    "headers",
    "cookie",
    "session",
    "session_id",
    "sessionid",
    "jwt",
    "signature",
    "private_key",
    "connection_string",
    "dsn",
    "database_url",
    "db_url",
    "account_id",
    "account_number",
    "ssn",
)
# Value shapes that are credentials wherever they appear, including inside free text under a harmless key.
# Key-based redaction cannot see these: every contract model is ``extra="forbid"``, so the only way a
# credential reaches a record at all is pasted into a free-text field (``reasoning``, a broker message).
# Each pattern replaces only the span it matched, so the surrounding prose stays auditable.
SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\brc_[A-Za-z0-9]{20,}"),
    re.compile(r"\b(?:PK|AK)[A-Z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@"),
    re.compile(r"(?i)[?&](?:api_?key|token|secret|password|passwd)=[^&\s\"']+"),
)
# Typed decimal maps (``exposure_by_signal_source``, ``exposure_by_model_provider``, ``factor_exposures``)
# are keyed by DATA, not by field name, and a signal source may legitimately be called "vendor:secret-feed".
# Redacting its value would write "[REDACTED]" where a DecimalStr belongs and make the record unbuildable.
#
# The exemption is deliberately narrow: a numeric value survives only under a key that carries a namespace
# separator, which is how those data keys are written ("vendor:polygon", "model:featherless/qwen3"). A plain
# field name never has one, so ``account_number`` and ``ssn`` are still redacted even though they are digits.
_NUMERIC_VALUE = re.compile(r"^-?\d+(?:\.\d+)?$")
_DATA_KEY = re.compile(r"[:/]")
# Keys whose value is a bag of headers or cookies. These are replaced WHOLESALE rather than
# recursed into: the individual header names are themselves revealing, and there is no structure
# inside worth preserving. This is the one place a sensitive key still swallows its whole value -
# and it is why `Policy.authorization` (a contract sub-section, not a header bag) is not in the list.
_COLLECTION_KEYS: frozenset[str] = frozenset({
    "header", "headers", "request_header", "request_headers", "response_header", "response_headers",
    "http_header", "http_headers", "cookie", "cookies", "set_cookie", "raw_headers",
})


def _is_collection_key(key: Any) -> bool:
    """True when a key names a header/cookie bag, whose value is replaced whole."""
    if not isinstance(key, str):
        return False
    return "_".join(_key_tokens(key)) in _COLLECTION_KEYS
# Contract field names that would otherwise match ``authorization`` but carry no secret: an authorization hash
# and the timestamp at which an authorization was validated. The ``authorization`` *object* itself is a contract
# object (it carries ``schema_version``) and is recursed into rather than replaced.
REDACTION_EXEMPT_KEYS: frozenset[str] = frozenset({"authorization_hash", "authorization_validated_at"})

# --------------------------------------------------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------------------------------------------------


def _normalize(obj: Any, path: str = "$") -> Any:
    """Reduce ``obj`` to plain JSON values. Anything outside the JSON data model is a ``TypeError``."""
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="json"), path)
    if obj is None or obj is True or obj is False:
        return obj
    if isinstance(obj, Enum):
        return _normalize(obj.value, path)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, Decimal):
        # Decimal is the engine's arithmetic type; its canonical JSON form is the normalised decimal
        # STRING, never a JSON number. That is what makes canonical_json(Decimal("2.40")) and
        # canonical_json("2.4") produce the same bytes, so a hash cannot depend on how a value was
        # spelled or on which side of the boundary it was built.
        if not obj.is_finite():
            raise TypeError(f"canonical_json: non-finite Decimal at {path}")
        return normalize_decimal_str(format(obj, "f"))
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical_json: object keys must be strings at {path}, got {type(key).__name__}")
            out[key] = _normalize(value, f"{path}.{key}")
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalize(item, f"{path}[{index}]") for index, item in enumerate(obj)]
    raise TypeError(f"canonical_json: unsupported type {type(obj).__name__} at {path}")


def canonical_json(obj: Any) -> str:
    """Serialise ``obj`` canonically: sorted keys (code-point order), no whitespace, UTF-8, no binary floats."""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    """Lowercase hex SHA-256; strings are hashed as UTF-8."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"sha256_hex() expects str or bytes, got {type(data).__name__}")
    return hashlib.sha256(bytes(data)).hexdigest()


def object_hash(obj: Any) -> str:
    """``sha256_hex(canonical_json(obj))``: the hash of any contract object or plain JSON structure."""
    return sha256_hex(canonical_json(obj))


# --------------------------------------------------------------------------------------------------------------------
# Derived identifiers
# --------------------------------------------------------------------------------------------------------------------


def _payload(payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, Mapping):
        return dict(payload)
    raise TypeError(f"expected a mapping or contract model, got {type(payload).__name__}")


def _hash_without(payload: Mapping[str, Any] | BaseModel, *excluded: str) -> str:
    data = _payload(payload)
    return object_hash({key: value for key, value in data.items() if key not in excluded})


def proposal_id_for(payload: Mapping[str, Any] | BaseModel) -> str:
    """``sha256_hex(canonical_json(proposal without "proposal_id" and "reasoning"))``.

    ``reasoning`` is excluded so that free text -- the only field an agent could use to smuggle instructions --
    never influences identity, idempotency or authorization scope.
    """
    return _hash_without(payload, "proposal_id", "reasoning")


def policy_hash_for(payload: Mapping[str, Any] | BaseModel) -> str:
    """``sha256_hex(canonical_json(policy without "policy_hash"))``."""
    return _hash_without(payload, "policy_hash")


def record_hash_for(payload: Mapping[str, Any] | BaseModel) -> str:
    """``sha256_hex(canonical_json(record without "audit_hash"))`` -- for DecisionRecord and ControlEvent alike."""
    return _hash_without(payload, "audit_hash")


def authorization_hash_for(payload: Mapping[str, Any] | BaseModel) -> str:
    """``sha256_hex(canonical_json(authorization without "authorization_hash"))``."""
    return _hash_without(payload, "authorization_hash")


def evaluation_id_for(payload: Mapping[str, Any] | BaseModel) -> str:
    """``sha256_hex(canonical_json(evaluation without "evaluation_id"))``."""
    return _hash_without(payload, "evaluation_id")


def _code_str(code: Any) -> str:
    if isinstance(code, Enum):
        return str(code.value)
    if isinstance(code, str):
        return str(code)
    raise TypeError(f"reason code must be a string, got {type(code).__name__}")


def _leg_quantity(leg: Any) -> dict[str, Any]:
    data = leg.model_dump(mode="json") if isinstance(leg, BaseModel) else dict(leg)
    return {"leg_index": int(data["leg_index"]), "quantity": normalize_decimal_str(str(data["quantity"]))}


def verdict_hash_for(
    verdict: str,
    reason_codes: Iterable[Any],
    authorized_total_quantity: str,
    authorized_legs: Iterable[Any],
    evaluation_id: str,
) -> str:
    """Hash of the decision *outcome*, independent of ids and timestamps, so replay can compare verdicts.

    Payload (then canonical JSON, then SHA-256)::

        {"authorized_legs": [{"leg_index": int, "quantity": DecimalStr}, ...]   # sorted by leg_index
         "authorized_total_quantity": DecimalStr,
         "evaluation_id": Sha256Hex,
         "reason_codes": [str, ...],                                              # sorted, de-duplicated
         "verdict": "APPROVE" | "REDUCE" | "REJECT"}
    """
    legs = sorted((_leg_quantity(leg) for leg in authorized_legs), key=lambda item: item["leg_index"])
    payload = {
        "authorized_legs": legs,
        "authorized_total_quantity": normalize_decimal_str(str(authorized_total_quantity)),
        "evaluation_id": str(evaluation_id),
        "reason_codes": sorted({_code_str(code) for code in reason_codes}),
        "verdict": str(verdict),
    }
    return object_hash(payload)


def idempotency_key_for(tenant_id: str, proposal_id: str, legs: Iterable[Any]) -> str:
    """``"mz1-" + sha256_hex(canonical_json({"legs": [...], "proposal_id": ..., "tenant_id": ...}))[:40]``.

    ``legs`` are the authorization scope's legs (``AuthorizedLeg`` objects or their JSON form), in order.
    """
    leg_payload = [leg.model_dump(mode="json") if isinstance(leg, BaseModel) else dict(leg) for leg in legs]
    digest = object_hash({"legs": leg_payload, "proposal_id": str(proposal_id), "tenant_id": str(tenant_id)})
    return IDEMPOTENCY_KEY_PREFIX + digest[:40]


# --------------------------------------------------------------------------------------------------------------------
# UUID v7 (RFC 9562) -- local implementation, monotonic within a process
# --------------------------------------------------------------------------------------------------------------------

_UUID_LOCK = threading.Lock()
_uuid_last_ms = 0
_uuid_counter = 0


def uuid7() -> str:
    """RFC 9562 version 7 UUID, lowercase, strictly increasing within this process.

    Layout: 48-bit Unix milliseconds | version 7 | 12-bit counter | variant 10 | 62 random bits. The counter is
    re-seeded with 11 random bits whenever the millisecond advances and incremented within a millisecond; if it
    overflows, or the wall clock steps backwards, the timestamp is advanced by one millisecond instead of ever
    producing a smaller value.
    """
    global _uuid_last_ms, _uuid_counter
    with _UUID_LOCK:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _uuid_last_ms:
            _uuid_last_ms = now_ms
            _uuid_counter = secrets.randbits(11)
        else:
            _uuid_counter += 1
            if _uuid_counter > 0xFFF:
                _uuid_last_ms += 1
                _uuid_counter = secrets.randbits(11)
        milliseconds = _uuid_last_ms & 0xFFFF_FFFF_FFFF
        counter = _uuid_counter
    rand_b = secrets.randbits(62)
    value = (milliseconds << 80) | (0x7 << 76) | (counter << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


# --------------------------------------------------------------------------------------------------------------------
# Redaction (Hard Rule A3)
# --------------------------------------------------------------------------------------------------------------------

_CAMEL_BOUNDARY = ("abcdefghijklmnopqrstuvwxyz0123456789", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
# Cyrillic and Greek letters that render identically to ASCII. NFKC leaves them alone because they
# are genuinely different letters, not compatibility forms - so a key spelled with them looks exactly
# like "api_key" to a human reviewer and nothing like it to a matcher. Mapped explicitly.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "і": "i", "ѕ": "s", "һ": "h", "ԁ": "d", "ԛ": "q",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X", "ο": "o", "ρ": "p", "υ": "u",
})


def _key_tokens(key: str) -> list[str]:
    """``"X-Api-Key"`` -> ``["x", "api", "key"]``; ``"accessToken"`` -> ``["access", "token"]``."""
    # NFKC folds fullwidth and other compatibility forms onto ASCII, so "ａpi_key" is seen as "api_key".
    # It does NOT fold Cyrillic homoglyphs (Cyrillic "а" is a distinct letter, not a compatibility form),
    # so those are mapped explicitly below - a key nobody can read as anything but "api_key" must not slip
    # through because two of its letters came from another alphabet.
    key = unicodedata.normalize("NFKC", key)
    key = key.translate(_HOMOGLYPHS)
    pieces: list[str] = []
    for index, char in enumerate(key):
        if index and char in _CAMEL_BOUNDARY[1] and key[index - 1] in _CAMEL_BOUNDARY[0]:
            pieces.append("_")
        pieces.append(char)
    normalised = "".join(pieces).casefold()
    for separator in ("-", " ", ".", ":", "/"):
        normalised = normalised.replace(separator, "_")
    return [token for token in normalised.split("_") if token]


def _token_matches(token: str, pattern_token: str) -> bool:
    return token == pattern_token or token == pattern_token + "s"


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key names a credential, secret, token, header collection or similar."""
    if key.lower() in REDACTION_EXEMPT_KEYS:
        return False
    tokens = _key_tokens(key)
    for pattern in SENSITIVE_KEY_PATTERNS:
        pattern_tokens = pattern.split("_")
        width = len(pattern_tokens)
        for start in range(0, len(tokens) - width + 1):
            window = tokens[start : start + width]
            if all(_token_matches(token, expected) for token, expected in zip(window, pattern_tokens, strict=True)):
                return True
        collapsed = pattern.replace("_", "")
        if width > 1 and any(_token_matches(token, collapsed) for token in tokens):
            return True
    return False


def _is_contract_object(value: Any) -> bool:
    return isinstance(value, Mapping) and "schema_version" in value


def _numeric_data_value(key: Any, value: Any, *, inherited: bool) -> bool:
    """True when a numeric value under a sensitive key is data rather than a credential.

    Only namespaced data keys qualify ("vendor:secret-feed" in a decimal map). A plain field name never
    carries a separator, so ``account_number`` and ``ssn`` stay redacted despite being digits.
    """
    if isinstance(value, bool):
        return False
    if not (isinstance(value, int) or (isinstance(value, str) and _NUMERIC_VALUE.fullmatch(value))):
        return False
    if inherited:
        return True
    return isinstance(key, str) and bool(_DATA_KEY.search(key))


def _scrub_value(value: str) -> str:
    """Replace credential-shaped spans inside a string, leaving the surrounding text intact."""
    for pattern in SENSITIVE_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact(obj: Any, *, inherited: bool = False) -> Any:
    """Redact ``obj``. ``inherited`` means an enclosing key was sensitive, so scalars here are too.

    Three cases need distinguishing, and conflating any two of them causes a real bug:

    * a sensitive key holding a SCALAR is a credential -> replace it (unless it is a number);
    * a sensitive key holding a CONTRACT OBJECT is a legitimate nested contract (the ``authorization``
      field of a DecisionRecord is an ExecutionAuthorization) -> recurse normally, no inheritance;
    * a sensitive key holding any OTHER structure is either a credentials blob or a contract sub-section
      that merely shares a name (``Policy.authorization`` is ``{"ttl_seconds": 15}``) -> recurse WITH
      inheritance, so strings inside are redacted while the structure and its numbers survive.

    Replacing that third case wholesale is what destroyed ``Policy.authorization``, breaking the policy
    hash and making the record unbuildable (ledger/requests.md REQ-3).
    """
    if isinstance(obj, Mapping):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            sensitive = inherited or (isinstance(key, str) and is_sensitive_key(key))
            if not sensitive or value is None:
                out[key] = _redact(value, inherited=False)
            elif _is_collection_key(key):
                out[key] = REDACTED
            elif _is_contract_object(value):
                out[key] = _redact(value, inherited=False)
            elif isinstance(value, (Mapping, list, tuple)):
                out[key] = _redact(value, inherited=True)
            elif _numeric_data_value(key, value, inherited=inherited):
                out[key] = value
            else:
                out[key] = REDACTED
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(item, inherited=inherited) for item in obj]
    if inherited and isinstance(obj, str) and not _NUMERIC_VALUE.fullmatch(obj):
        return REDACTED
    if inherited and isinstance(obj, str):
        return obj
    if isinstance(obj, str):
        return _scrub_value(obj)
    return obj


def redact(obj: Any) -> Any:
    """Recursively replace values under sensitive keys with ``"[REDACTED]"``.

    Keys are matched case-insensitively and across ``-``/``_``/camelCase spellings, as whole tokens (``secret_key``,
    ``X-Api-Key``, ``accessToken``, ``request_headers`` all match). Header collections are replaced wholesale.
    ``None`` is never redacted (there is nothing to hide), contract objects (mappings carrying ``schema_version``)
    are recursed into rather than replaced, and ``REDACTION_EXEMPT_KEYS`` names the contract fields that merely
    reference an authorization. Pydantic models are dumped to JSON form first; the result is always plain data.
    """
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return _redact(obj)


# --------------------------------------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------------------------------------

_LIBRARIES: tuple[tuple[str, str], ...] = (("pydantic", "pydantic"), ("jsonschema", "jsonschema"), ("pyyaml", "PyYAML"))


def library_versions() -> dict[str, str]:
    """Versions recorded in every DecisionRecord: python, pydantic, jsonschema, pyyaml."""
    versions = {"python": platform.python_version()}
    for name, distribution in _LIBRARIES:
        try:
            versions[name] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


__all__ = [
    "DECIMAL_CONTEXT",
    "ENGINE_VERSION",
    "IDEMPOTENCY_KEY_PREFIX",
    "REDACTED",
    "REDACTION_EXEMPT_KEYS",
    "SENSITIVE_KEY_PATTERNS",
    "ZERO_HASH",
    "authorization_hash_for",
    "canonical_json",
    "evaluation_id_for",
    "idempotency_key_for",
    "is_sensitive_key",
    "library_versions",
    "normalize_decimal_str",
    "object_hash",
    "policy_hash_for",
    "proposal_id_for",
    "record_hash_for",
    "redact",
    "sha256_hex",
    "uuid7",
    "verdict_hash_for",
]
