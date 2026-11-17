"""Validation engine orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .validate_checks import UNKNOWN, run_batch_checks, run_record_checks
from .validate_config import POLICY_STRICT, ValidationConfig
from .validate_fields import validate_field

@dataclass
class ValidationResult:
    """Outcome of one validation round.

    ``errors`` holds proven violations.  ``evidence`` holds the per-rule,
    per-record (or per-screen) proof state: ``proven_pass``, ``proven_fail``,
    ``unknown`` or ``not_applicable``.  A round is only ``ok`` when nothing
    failed *and* nothing is unproven — a skipped rule is never a pass.
    """

    errors: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    policy: str = POLICY_STRICT

    @property
    def unknowns(self) -> list[dict[str, Any]]:
        return [item for item in self.evidence if item.get("status") == UNKNOWN]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unknowns

    def run_status(self) -> str:
        """Final round status under the configured contract policy.

        Proven violations always fail the round.  Unproven (``unknown``)
        evidence makes a strict compliance round ``inconclusive`` and an
        investigation round ``diagnostic`` — neither is published as
        validated.  Only fully proven rounds are ``success``.
        """
        if self.errors:
            return "failed"
        if self.unknowns:
            return "inconclusive" if self.policy == POLICY_STRICT else "diagnostic"
        return "success"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "run_status": self.run_status(), "policy": self.policy, "errors": self.errors, "evidence": self.evidence, "stats": self.stats}

def validate_records(
    records: list[dict[str, Any]],
    config: ValidationConfig,
    *,
    facts_by_screen: dict[str, dict[str, Any]] | None = None,
    screen_completeness: dict[str, dict[str, Any]] | None = None,
) -> ValidationResult:
    facts_by_screen = facts_by_screen or {}
    errors: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for record in records:
        screen_id = str(record.get("screen_id"))
        for field_name, rule in config.fields.get(screen_id, {}).items():
            errors.extend(validate_field(screen_id, record, field_name, rule))
    check_errors, check_evidence = run_record_checks(records, config, config.record_checks)
    errors.extend(check_errors)
    evidence.extend(check_evidence)
    batch_errors, batch_evidence = run_batch_checks(records, facts_by_screen, config.batch_checks, screen_completeness)
    errors.extend(batch_errors)
    evidence.extend(batch_evidence)
    invalid = {item["record_key"] for item in errors if item.get("record_key")}
    stats = {
        "records_in": len(records),
        "records_valid": sum(1 for record in records if record.get("record_key") not in invalid),
        "records_invalid": sum(1 for record in records if record.get("record_key") in invalid),
        "error_count": len(errors),
        "error_codes": sorted({item["code"] for item in errors}),
    }
    return ValidationResult(errors=errors, evidence=evidence, stats=stats, policy=config.policy)
