"""Record-level consistency and batch-level validation checks.

Every ``record_check`` produces one evidence entry per record of its screen
and every ``sum_equals`` produces one evidence entry per run for its screen.
Evidence states are reviewable: ``proven_pass`` and ``proven_fail`` mean the
rule was evaluated on complete, valid operands; ``unknown`` means a required
operand, footer fact, or the record list itself could not be proven complete;
``not_applicable`` means the contract's explicit ``when`` condition was
determined and did not match.  A skip is never reported as a pass.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import E_BATCH_COUNT_MISMATCH, E_BATCH_DUPLICATE_KEY, E_BATCH_DUPLICATE_RECORD, E_BATCH_EMPTY, E_BATCH_SUM_MISMATCH, E_RECORD_CONSISTENCY
from .validate_config import BatchCheck, RecordCheck, ValidationConfig
from .validate_fields import coerce, compare, issue

PROVEN_PASS = "proven_pass"
PROVEN_FAIL = "proven_fail"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
EVIDENCE_STATUSES = frozenset({PROVEN_PASS, PROVEN_FAIL, UNKNOWN, NOT_APPLICABLE})

def _evidence(*, rule_id: str, kind: str, screen_id: str, record_key: str | None, status: str, message: str, depends_on: list[str], **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "rule_id": rule_id,
        "kind": kind,
        "screen_id": screen_id,
        "record_key": record_key,
        "status": status,
        "message": message,
        "depends_on": depends_on,
    }
    entry.update(extra)
    return entry

def _resolve_operand(config: ValidationConfig, screen_id: str, name: str, values: dict[str, Any]) -> tuple[Any, str | None]:
    """Convert a record field for comparison, explaining any failure."""
    if name not in values or values.get(name) is None:
        return None, f"field {name!r} is missing"
    raw = values.get(name)
    rule = config.fields.get(screen_id, {}).get(name)
    if rule is None:
        return str(raw), None
    valid, converted = coerce(rule, raw)
    if not valid:
        return None, f"field {name!r} value {raw!r} is not a valid {rule.type}"
    return converted, None

def _resolve_literal(config: ValidationConfig, check: RecordCheck) -> tuple[Any, str | None]:
    """Convert the declared ``right`` literal using the left field's rule."""
    rule = config.fields.get(check.screen_id, {}).get(check.left)
    if rule is None:
        return str(check.right_value), None
    valid, converted = coerce(rule, check.right_value)
    if not valid:
        return None, f"right literal {check.right_value!r} is not a valid {rule.type}"
    return converted, None

def _when_state(config: ValidationConfig, check: RecordCheck, values: dict[str, Any]) -> tuple[str, str | None]:
    """Evaluate the explicit applicability condition of a record check."""
    condition = check.when
    assert condition is not None
    converted, reason = _resolve_operand(config, check.screen_id, condition.field, values)
    if reason is not None:
        return UNKNOWN, f"applicability condition undetermined: {reason}"
    rule = config.fields.get(check.screen_id, {}).get(condition.field)
    for accepted in condition.accepted:
        if rule is None:
            matched = str(accepted) == converted
        else:
            valid, candidate = coerce(rule, accepted)
            matched = valid and candidate == converted
        if matched:
            return "satisfied", None
    return NOT_APPLICABLE, f"condition {condition.field}={converted!r} is not in {list(condition.accepted)!r}"

def run_record_checks(records: list[dict[str, Any]], config: ValidationConfig, checks: tuple[RecordCheck, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for check in checks:
        for record in records:
            if record.get("screen_id") != check.screen_id:
                continue
            record_key = record.get("record_key")
            values = record.get("normalized_values", {})
            base = {"rule_id": check.check_id, "kind": "record_check", "screen_id": check.screen_id, "record_key": record_key}
            depends_on = [check.when.field] if check.when is not None else []
            depends_on += [check.left, check.right_field] if check.right_field is not None else [check.left]
            depends_on = [name for name in depends_on if name is not None]
            if check.when is not None:
                state, reason = _when_state(config, check, values)
                if state == UNKNOWN:
                    evidence.append(_evidence(**base, status=UNKNOWN, message=reason or "applicability condition undetermined", depends_on=depends_on))
                    continue
                if state == NOT_APPLICABLE:
                    evidence.append(_evidence(**base, status=NOT_APPLICABLE, message=reason or "condition not satisfied", depends_on=depends_on))
                    continue
            left, left_reason = _resolve_operand(config, check.screen_id, check.left, values)
            if check.right_field is not None:
                right, right_reason = _resolve_operand(config, check.screen_id, check.right_field, values)
            else:
                right, right_reason = _resolve_literal(config, check)
            reasons = [reason for reason in (left_reason, right_reason) if reason is not None]
            if reasons:
                evidence.append(_evidence(**base, status=UNKNOWN, message="; ".join(reasons), depends_on=depends_on))
                continue
            try:
                passed = compare(left, check.op, right)
            except TypeError:
                message = f"operands are not comparable: {type(left).__name__} {check.op} {type(right).__name__}"
                evidence.append(_evidence(**base, status=UNKNOWN, message=message, depends_on=depends_on))
                continue
            if passed:
                evidence.append(_evidence(**base, status=PROVEN_PASS, message=f"{check.left} {check.op} holds", depends_on=depends_on))
            else:
                evidence.append(_evidence(**base, status=PROVEN_FAIL, message=check.message, depends_on=depends_on))
                errors.append(issue(E_RECORD_CONSISTENCY, rule_id=check.check_id, screen_id=check.screen_id, record_key=record_key, field_name=check.left, message=check.message))
    return errors, evidence

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

def _finite_decimal(value: Any) -> Decimal | None:
    """Parse a finite Decimal; missing, unparsable and non-finite give None."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None

def _sum_check(grouped: list[dict[str, Any]], check: BatchCheck, facts: dict[str, Any], completeness: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    frames = sorted({record.get("frame_seq") for record in grouped if record.get("frame_seq") is not None})
    depends_on = [f"field:{check.field_name}", f"fact:{check.fact}", f"screen:{check.screen_id}"]
    base: dict[str, Any] = {
        "rule_id": check.check_id,
        "kind": "sum_equals",
        "screen_id": check.screen_id,
        "record_key": None,
        "fact": check.fact,
        "field": check.field_name,
        "frames": frames,
        "record_count": len(grouped),
    }
    if check.fact not in facts:
        message = f"footer fact {check.fact!r} is missing"
        return [], [_evidence(**base, status=UNKNOWN, message=message, depends_on=depends_on)]
    raw_fact = facts.get(check.fact)
    expected = _finite_decimal(raw_fact)
    if expected is None:
        message = f"footer fact {check.fact!r} value {raw_fact!r} is not a finite decimal"
        return [], [_evidence(**base, status=UNKNOWN, message=message, depends_on=depends_on)]
    base["fact_value"] = str(expected)
    if completeness is not None and not completeness.get("complete", True):
        stop_reason = completeness.get("stop_reason", "unknown")
        message = f"record list for {check.screen_id} is not proven complete (stop_reason={stop_reason})"
        return [], [_evidence(**base, status=UNKNOWN, message=message, depends_on=depends_on, stop_reason=stop_reason)]
    bad_operands: list[dict[str, Any]] = []
    total = Decimal("0")
    for record in grouped:
        value = record.get("normalized_values", {}).get(check.field_name)
        if value is None:
            bad_operands.append({"record_key": record.get("record_key"), "frame_seq": record.get("frame_seq"), "reason": "missing", "value": None})
            continue
        number = _finite_decimal(value)
        if number is None:
            reason = "non_finite" if _is_non_finite(value) else "invalid_decimal"
            bad_operands.append({"record_key": record.get("record_key"), "frame_seq": record.get("frame_seq"), "reason": reason, "value": str(value)})
            continue
        total += number
    if bad_operands:
        message = f"{len(bad_operands)} of {len(grouped)} {check.field_name} operands are missing or invalid; partial sum is not proof"
        return [], [_evidence(**base, status=UNKNOWN, message=message, depends_on=depends_on, operands=bad_operands)]
    tolerance = Decimal(str(check.tolerance))
    detail = {"total": str(total), "expected": str(expected), "tolerance": str(tolerance)}
    if abs(total - expected) > tolerance:
        message = f"sum {total} does not equal {check.fact}={expected}"
        errors = [issue(E_BATCH_SUM_MISMATCH, rule_id=check.check_id, screen_id=check.screen_id, record_key=None, field_name=check.field_name, message=message)]
        return errors, [_evidence(**base, status=PROVEN_FAIL, message=message, depends_on=depends_on, **detail)]
    message = f"sum {total} equals {check.fact}={expected}"
    return [], [_evidence(**base, status=PROVEN_PASS, message=message, depends_on=depends_on, **detail)]

def _is_non_finite(value: Any) -> bool:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return not number.is_finite()

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

def run_batch_checks(
    records: list[dict[str, Any]],
    facts_by_screen: dict[str, dict[str, Any]],
    checks: tuple[BatchCheck, ...],
    completeness_by_screen: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
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
            completeness = None if completeness_by_screen is None else completeness_by_screen.get(check.screen_id)
            check_errors, check_evidence = _sum_check(grouped, check, facts_by_screen.get(check.screen_id, {}), completeness)
            errors.extend(check_errors)
            evidence.extend(check_evidence)
        elif check.check == "count_equals":
            errors.extend(_count_errors(grouped, check, facts_by_screen.get(check.screen_id, {})))
    return errors, evidence
