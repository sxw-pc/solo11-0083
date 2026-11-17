"""Validation engine orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .validate_checks import run_batch_checks, run_ownership_checks, run_record_checks
from .validate_config import ValidationConfig
from .validate_fields import validate_field

@dataclass
class ValidationResult:
    errors: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    ownership_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "stats": self.stats, "ownership_results": self.ownership_results}

def validate_records(
    records: list[dict[str, Any]],
    config: ValidationConfig,
    *,
    facts_by_screen: dict[str, dict[str, Any]] | None = None,
    fact_sources_by_screen: dict[str, dict[str, Any]] | None = None,
) -> ValidationResult:
    facts_by_screen = facts_by_screen or {}
    errors: list[dict[str, Any]] = []
    for record in records:
        screen_id = str(record.get("screen_id"))
        for field_name, rule in config.fields.get(screen_id, {}).items():
            errors.extend(validate_field(screen_id, record, field_name, rule))
    errors.extend(run_record_checks(records, config, config.record_checks))
    errors.extend(run_batch_checks(records, facts_by_screen, config.batch_checks))
    ownership_errors, ownership_results = run_ownership_checks(
        records,
        config,
        config.ownership_checks,
        facts_by_screen=facts_by_screen,
        fact_sources_by_screen=fact_sources_by_screen or {},
    )
    errors.extend(ownership_errors)
    invalid = {item["record_key"] for item in errors if item.get("record_key")}
    stats = {
        "records_in": len(records),
        "records_valid": sum(1 for record in records if record.get("record_key") not in invalid),
        "records_invalid": sum(1 for record in records if record.get("record_key") in invalid),
        "error_count": len(errors),
        "error_codes": sorted({item["code"] for item in errors}),
    }
    return ValidationResult(errors=errors, stats=stats, ownership_results=ownership_results)
