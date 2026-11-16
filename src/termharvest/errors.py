"""Stable error codes and one exception type used across the pipeline."""

from __future__ import annotations

from typing import Any

# Configuration
E_CONFIG_PARSE = "E_CONFIG_PARSE"
E_CONFIG_SCHEMA = "E_CONFIG_SCHEMA"
E_CONFIG_DUPLICATE_FIELD = "E_CONFIG_DUPLICATE_FIELD"
E_CONFIG_BAD_LOCATOR = "E_CONFIG_BAD_LOCATOR"
E_CONFIG_BAD_TARGET = "E_CONFIG_BAD_TARGET"

# Network and protocol
E_TARGET_NOT_ALLOWED = "E_TARGET_NOT_ALLOWED"
E_CONNECTION_FAILED = "E_CONNECTION_FAILED"
E_CONNECTION_CLOSED = "E_CONNECTION_CLOSED"
E_FRAME_TIMEOUT = "E_FRAME_TIMEOUT"
E_FRAME_TOO_LARGE = "E_FRAME_TOO_LARGE"
E_FRAME_MALFORMED = "E_FRAME_MALFORMED"

# Session
E_SCREEN_UNEXPECTED = "E_SCREEN_UNEXPECTED"
E_STEP_RETRY_EXHAUSTED = "E_STEP_RETRY_EXHAUSTED"
E_SCREEN_NOT_CAPTURED = "E_SCREEN_NOT_CAPTURED"

# Extraction
E_SCREEN_CONTRACT_MISMATCH = "E_SCREEN_CONTRACT_MISMATCH"
E_SCREEN_FINGERPRINT_MISMATCH = "E_SCREEN_FINGERPRINT_MISMATCH"
E_EXTRACT_LOCATOR_OUT_OF_BOUNDS = "E_EXTRACT_LOCATOR_OUT_OF_BOUNDS"
E_CASSETTE_PARSE = "E_CASSETTE_PARSE"
E_CASSETTE_MISMATCH = "E_CASSETTE_MISMATCH"
E_CASSETTE_INCOMPLETE = "E_CASSETTE_INCOMPLETE"

# Redaction package (shareable, sensitive-free evidence)
E_REDACT_LABEL_AMBIGUOUS = "E_REDACT_LABEL_AMBIGUOUS"
E_REDACT_LOCATOR_OUT_OF_BOUNDS = "E_REDACT_LOCATOR_OUT_OF_BOUNDS"
E_REDACT_OVERLAP = "E_REDACT_OVERLAP"
E_REDACT_RESIDUAL_VALUE = "E_REDACT_RESIDUAL_VALUE"
E_REDACT_CONTRACT_MISMATCH = "E_REDACT_CONTRACT_MISMATCH"
E_REDACT_PACKAGE_PARSE = "E_REDACT_PACKAGE_PARSE"
E_REDACT_PACKAGE_MISMATCH = "E_REDACT_PACKAGE_MISMATCH"
E_REDACT_PACKAGE_INCOMPLETE = "E_REDACT_PACKAGE_INCOMPLETE"

# Validation, storage, reporting, mock host
E_VALIDATION_CONFIG = "E_VALIDATION_CONFIG"
E_FIELD_REQUIRED = "E_FIELD_REQUIRED"
E_FIELD_TYPE = "E_FIELD_TYPE"
E_FIELD_PATTERN = "E_FIELD_PATTERN"
E_FIELD_ENUM = "E_FIELD_ENUM"
E_FIELD_RANGE = "E_FIELD_RANGE"
E_FIELD_FORMAT = "E_FIELD_FORMAT"
E_RECORD_CONSISTENCY = "E_RECORD_CONSISTENCY"
E_BATCH_EMPTY = "E_BATCH_EMPTY"
E_BATCH_DUPLICATE_KEY = "E_BATCH_DUPLICATE_KEY"
E_BATCH_DUPLICATE_RECORD = "E_BATCH_DUPLICATE_RECORD"
E_BATCH_SUM_MISMATCH = "E_BATCH_SUM_MISMATCH"
E_BATCH_COUNT_MISMATCH = "E_BATCH_COUNT_MISMATCH"
E_STORE_ERROR = "E_STORE_ERROR"
E_REPORT_ERROR = "E_REPORT_ERROR"
E_SCENARIO_INVALID = "E_SCENARIO_INVALID"
E_INTERNAL = "E_INTERNAL"

ERROR_CODES = frozenset(name for name, value in list(globals().items()) if name.startswith("E_") and isinstance(value, str))

class HarvestError(Exception):
    """A failure with a stable machine-readable code and structured context."""

    def __init__(self, code: str, message: str, *, context: dict[str, Any] | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message, "context": self.context}
        if self.cause is not None:
            data["cause"] = {"type": type(self.cause).__name__, "message": str(self.cause)}
        return data

    def __str__(self) -> str:
        location = ""
        for key in ("file", "field", "screen_id", "step_id"):
            if key in self.context:
                location += f" {key}={self.context[key]}"
        return f"[{self.code}]{location} {self.message}"

def wrap_internal(exc: BaseException, message: str, *, context: dict[str, Any] | None = None) -> HarvestError:
    """Convert an unexpected exception into a coded pipeline error."""
    if isinstance(exc, HarvestError):
        return exc
    return HarvestError(E_INTERNAL, message, context=context, cause=exc)
