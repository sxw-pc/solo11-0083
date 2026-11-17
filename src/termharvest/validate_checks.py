"""Record-level consistency and batch-level validation checks."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import E_BATCH_COUNT_MISMATCH, E_BATCH_DUPLICATE_KEY, E_BATCH_DUPLICATE_RECORD, E_BATCH_EMPTY, E_BATCH_SUM_MISMATCH, E_RECORD_CONSISTENCY
from .validate_config import BatchCheck, RecordCheck, ValidationConfig
from .validate_fields import compare, convert_for_compare, issue

def run_record_checks(records: list[dict[str, Any]], config: ValidationConfig, checks: tuple[RecordCheck, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check in checks:
        for record in records:
            if record.get("screen_id") != check.screen_id:
                continue
            values = record.get("normalized_values", {})
            left = convert_for_compare(config, check.screen_id, check.left, values.get(check.left))
            if check.right_field is not None:
                right = convert_for_compare(config, check.screen_id, check.right_field, values.get(check.right_field))
            else:
                right = convert_for_compare(config, check.screen_id, check.left, check.right_value)
            if left is None or right is None:
                continue
            if not compare(left, check.op, right):
                errors.append(issue(E_RECORD_CONSISTENCY, rule_id=check.check_id, screen_id=check.screen_id, record_key=record.get("record_key"), field_name=check.left, message=check.message))
    return errors

def records_for(records: list[dict[str, Any]], screen_id: str) -> list[dict[str, Any]]:
    return [item for item in records if item.get("screen_id") == screen_id]

def _duplicate_key_errors(grouped: list[dict[str, Any]], check: BatchCheck) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    errors = []
    for record in grouped:
        values = record.get("normalized_values", {})
        key = tuple(values.get(name) for name in check.key)
        if key in seen:
            errors.append(
                issue(
                    E_BATCH_DUPLICATE_KEY,
                    rule_id=check.check_id,
                    screen_id=check.screen_id,
                    record_key=record.get("record_key"),
                    field_name=",".join(check.key),
                    message=f"duplicate business key {key!r}",
                )
            )
        seen.add(key)
    return errors

def _duplicate_record_errors(grouped: list[dict[str, Any]], check: BatchCheck) -> list[dict[str, Any]]:
    seen: set[str] = set()
    errors = []
    for record in grouped:
        canonical = json.dumps(record.get("normalized_values", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical in seen:
            errors.append(
                issue(E_BATCH_DUPLICATE_RECORD, rule_id=check.check_id, screen_id=check.screen_id, record_key=record.get("record_key"), field_name=None, message="duplicate normalized record")
            )
        seen.add(canonical)
    return errors

def _sum_errors(grouped: list[dict[str, Any]], check: BatchCheck, facts: dict[str, Any]) -> list[dict[str, Any]]:
    if check.fact not in facts:
        return [issue(E_BATCH_SUM_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=check.field_name, message=f"fact {check.fact!r} is missing")]
    total = Decimal("0")
    for record in grouped:
        value = record.get("normalized_values", {}).get(check.field_name)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            continue
        if number.is_finite():
            total += number
    try:
        expected = Decimal(str(facts[check.fact]))
    except InvalidOperation:
        expected = Decimal("0")
    if abs(total - expected) > Decimal(str(check.tolerance)):
        return [
            issue(E_BATCH_SUM_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=check.field_name, message=f"sum {total} does not equal {check.fact}={expected}")
        ]
    return []

def _count_errors(grouped: list[dict[str, Any]], check: BatchCheck, facts: dict[str, Any]) -> list[dict[str, Any]]:
    if check.fact not in facts:
        return [issue(E_BATCH_COUNT_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=f"fact {check.fact!r} is missing")]
    try:
        expected = int(str(facts[check.fact]))
    except ValueError:
        expected = -1
    if len(grouped) != expected:
        return [
            issue(E_BATCH_COUNT_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=f"count {len(grouped)} does not equal {check.fact}={expected}")
        ]
    return []

def run_batch_checks(records: list[dict[str, Any]], facts_by_screen: dict[str, dict[str, Any]], checks: tuple[BatchCheck, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check in checks:
        grouped = records_for(records, check.screen_id)
        if check.check == "unique_key":
            errors.extend(_duplicate_key_errors(grouped, check))
        elif check.check == "duplicate_record":
            errors.extend(_duplicate_record_errors(grouped, check))
        elif check.check == "non_empty":
            if not grouped:
                errors.append(issue(E_BATCH_EMPTY, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=None, message=check.message or "no records extracted"))
        elif check.check == "sum_equals":
            errors.extend(_sum_errors(grouped, check, facts_by_screen.get(check.screen_id, {})))
        elif check.check == "count_equals":
            errors.extend(_count_errors(grouped, check, facts_by_screen.get(check.screen_id, {})))
    return errors
