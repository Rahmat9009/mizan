"""Error taxonomy: ``MizanError`` and its subclasses, generated from ``contracts/error_codes.json``.

An error carries a machine code, the HTTP status the API maps it to, a *generic, safe* message, a correlation id
and the reason codes that explain it. Anything specific (exception text, stack frames, broker payloads) belongs in
``detail``, which is for logs only and is never part of an API payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from mizan.contracts.canonical import uuid7
from mizan.contracts.reason_codes import CONTRACTS_DIR, ReasonCode, sorted_reason_codes

ERROR_CODES_PATH = CONTRACTS_DIR / "error_codes.json"


@dataclass(frozen=True)
class ErrorCodeInfo:
    code: str
    http_status: int
    message: str


def _load() -> tuple[str, dict[str, ErrorCodeInfo]]:
    if not ERROR_CODES_PATH.is_file():
        raise ImportError(f"contracts/error_codes.json not found at {ERROR_CODES_PATH}")
    raw = json.loads(ERROR_CODES_PATH.read_text(encoding="utf-8"))
    if set(raw) != {"version", "codes"}:
        raise ImportError("error_codes.json must have exactly the keys 'version' and 'codes'")
    infos: dict[str, ErrorCodeInfo] = {}
    for code, entry in raw["codes"].items():
        if set(entry) != {"http_status", "message"}:
            raise ImportError(f"error code {code}: unexpected keys {sorted(entry)}")
        status = entry["http_status"]
        if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
            raise ImportError(f"error code {code}: http_status must be an integer 400..599")
        infos[code] = ErrorCodeInfo(code=code, http_status=status, message=str(entry["message"]))
    return raw["version"], infos


ERROR_CODE_VERSION, _INFO_BY_STRING = _load()
ErrorCode = StrEnum("ErrorCode", {code: code for code in _INFO_BY_STRING}, module=__name__)
ERROR_CODE_INFO: dict[ErrorCode, ErrorCodeInfo] = {ErrorCode(code): info for code, info in _INFO_BY_STRING.items()}


class MizanError(Exception):
    """Base class. ``code`` defaults per subclass; ``message`` defaults to the generic text from error_codes.json."""

    default_code: ClassVar[ErrorCode] = ErrorCode.ENGINE_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        reason_codes: Any = (),
        correlation_id: str | None = None,
        detail: str = "",
        code: ErrorCode | str | None = None,
    ) -> None:
        self.code: ErrorCode = ErrorCode(code) if code is not None else type(self).default_code
        info = ERROR_CODE_INFO[self.code]
        self.http_status: int = info.http_status
        self.message: str = message if message else info.message
        self.correlation_id: str = correlation_id if correlation_id else uuid7()
        self.reason_codes: list[ReasonCode] = sorted_reason_codes(reason_codes)
        self.detail: str = detail
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """The wire form the API returns: ``{"error": {"code", "message", "correlation_id", "reason_codes"}}``."""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "correlation_id": self.correlation_id,
                "reason_codes": [code.value for code in self.reason_codes],
            }
        }

    def __str__(self) -> str:
        codes = ",".join(code.value for code in self.reason_codes)
        suffix = f" [{codes}]" if codes else ""
        return f"{self.code.value}: {self.message}{suffix} (correlation_id={self.correlation_id})"


class ValidationFailed(MizanError):
    default_code = ErrorCode.VALIDATION_FAILED


class NotFound(MizanError):
    default_code = ErrorCode.NOT_FOUND


class TenantForbidden(MizanError):
    default_code = ErrorCode.TENANT_FORBIDDEN


class PolicyError(MizanError):
    default_code = ErrorCode.POLICY_ERROR


class EngineError(MizanError):
    default_code = ErrorCode.ENGINE_ERROR


class LedgerError(MizanError):
    default_code = ErrorCode.LEDGER_ERROR


class ChainIntegrityError(MizanError):
    default_code = ErrorCode.CHAIN_INTEGRITY_ERROR


class AuthorizationError(MizanError):
    default_code = ErrorCode.AUTHORIZATION_ERROR


class ExecutionBlocked(MizanError):
    default_code = ErrorCode.EXECUTION_BLOCKED


class BrokerError(MizanError):
    default_code = ErrorCode.BROKER_ERROR


class KillSwitchActive(MizanError):
    default_code = ErrorCode.KILL_SWITCH_ACTIVE


class LiveTradingForbidden(MizanError):
    default_code = ErrorCode.LIVE_TRADING_FORBIDDEN


class ConfigurationError(MizanError):
    default_code = ErrorCode.CONFIGURATION_ERROR


class RateLimited(MizanError):
    default_code = ErrorCode.RATE_LIMITED


ERROR_CLASSES: dict[ErrorCode, type[MizanError]] = {
    cls.default_code: cls
    for cls in (
        ValidationFailed,
        NotFound,
        TenantForbidden,
        PolicyError,
        EngineError,
        LedgerError,
        ChainIntegrityError,
        AuthorizationError,
        ExecutionBlocked,
        BrokerError,
        KillSwitchActive,
        LiveTradingForbidden,
        ConfigurationError,
        RateLimited,
    )
}

__all__ = [
    "ERROR_CLASSES",
    "ERROR_CODES_PATH",
    "ERROR_CODE_INFO",
    "ERROR_CODE_VERSION",
    "AuthorizationError",
    "BrokerError",
    "ChainIntegrityError",
    "ConfigurationError",
    "EngineError",
    "ErrorCode",
    "ErrorCodeInfo",
    "ExecutionBlocked",
    "KillSwitchActive",
    "LedgerError",
    "LiveTradingForbidden",
    "MizanError",
    "NotFound",
    "PolicyError",
    "RateLimited",
    "TenantForbidden",
    "ValidationFailed",
]
