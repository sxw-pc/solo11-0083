"""Validation engine orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .validate_checks import run_batch_checks, run_record_checks
from .validate_config import ValidationConfig
from .validate_fields import validate_field

#: Record-level validation statuses stored and reported by the pipeline.
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"


@dataclass
class ValidationResult:
    errors: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    unknown_record_keys: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "stats": self.stats, "unknown_record_keys": sorted(self.unknown_record_keys)}


def validate_records(
    records: list[dict[str, Any]],
    config: ValidationConfig,
    *,
    facts_by_screen: dict[str, dict[str, Any]] | None = None,
    pattern_policy: str | None = None,
) -> ValidationResult:
    facts_by_screen = facts_by_screen or {}
    policy = pattern_policy or config.pattern_policy
    errors: list[dict[str, Any]] = []
    unknown_keys: set[str] = set()
    for record in records:
        screen_id = str(record.get("screen_id"))
        for field_name, rule in config.fields.get(screen_id, {}).items():
            field_errors, undecidable = validate_field(
                screen_id,
                record,
                field_name,
                rule,
                pattern_policy=policy,
            )
            errors.extend(field_errors)
            if undecidable and record.get("record_key") is not None:
                unknown_keys.add(record["record_key"])
    errors.extend(run_record_checks(records, config, config.record_checks))
    errors.extend(run_batch_checks(records, facts_by_screen, config.batch_checks))
    invalid = {
        item["record_key"]
        for item in errors
        if item.get("record_key") and item["record_key"] not in unknown_keys
    }
    stats = {
        "records_in": len(records),
        "records_valid": sum(1 for record in records if record.get("record_key") not in invalid and record.get("record_key") not in unknown_keys),
        "records_invalid": sum(1 for record in records if record.get("record_key") in invalid),
        "records_unknown": sum(1 for record in records if record.get("record_key") in unknown_keys),
        "error_count": len(errors),
        "error_codes": sorted({item["code"] for item in errors}),
    }
    return ValidationResult(errors=errors, stats=stats, unknown_record_keys=unknown_keys)
