"""Declarative validation-rule parsing."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config_util import integer, list_value, load_yaml_document, mapping, text
from .errors import E_VALIDATION_CONFIG, HarvestError

FIELD_TYPES = frozenset({"string", "integer", "decimal", "date"})
OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
BATCH_CHECKS = frozenset({"unique_key", "duplicate_record", "non_empty", "sum_equals", "count_equals"})
POLICIES = frozenset({"strict", "investigation"})
POLICY_STRICT = "strict"
POLICY_INVESTIGATION = "investigation"

@dataclass(frozen=True)
class FieldRule:
    required: bool = False
    type: str = "string"
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    date_format: str = "%Y-%m-%d"

@dataclass(frozen=True)
class WhenCondition:
    """Explicit applicability condition for an optional record check.

    The check only runs when ``field`` equals one of ``accepted``.  When the
    condition field itself cannot be determined the rule is ``unknown``; when
    the condition is determined and does not match, the rule is
    ``not_applicable`` instead of being silently skipped.
    """

    field: str
    accepted: tuple[Any, ...]

@dataclass(frozen=True)
class RecordCheck:
    check_id: str
    screen_id: str
    left: str
    op: str
    right_field: str | None
    right_value: Any
    message: str
    when: WhenCondition | None = None

@dataclass(frozen=True)
class BatchCheck:
    check_id: str
    check: str
    screen_id: str
    key: tuple[str, ...] = ()
    field_name: str | None = None
    fact: str | None = None
    tolerance: float = 0.0
    message: str = ""

@dataclass(frozen=True)
class ValidationConfig:
    fields: dict[str, dict[str, FieldRule]] = field(default_factory=dict)
    record_checks: tuple[RecordCheck, ...] = ()
    batch_checks: tuple[BatchCheck, ...] = ()
    policy: str = POLICY_STRICT

def _error(source: Path, field_name: str, message: str) -> HarvestError:
    return HarvestError(E_VALIDATION_CONFIG, message, context={"file": str(source), "field": field_name})

def _bool(value: Any, source: Path, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _error(source, field_name, "expected boolean")
    return value

def _number(value: Any, source: Path, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(source, field_name, "expected a number")
    return float(value)

def _parse_field_rule(raw: Any, source: Path, field_name: str) -> FieldRule:
    item = mapping(raw, source, field_name)
    rule_type = item.get("type", "string")
    if rule_type not in FIELD_TYPES:
        raise _error(source, f"{field_name}.type", f"type must be one of {sorted(FIELD_TYPES)}")
    pattern = item.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise _error(source, f"{field_name}.pattern", "pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _error(source, f"{field_name}.pattern", f"invalid regex: {exc}") from exc
    enum_raw = item.get("enum")
    enum = None
    if enum_raw is not None:
        values = list_value(enum_raw, source, f"{field_name}.enum")
        if not values:
            raise _error(source, f"{field_name}.enum", "enum must not be empty")
        enum = tuple(str(value) for value in values)
    date_format = item.get("format", "%Y-%m-%d")
    if not isinstance(date_format, str):
        raise _error(source, f"{field_name}.format", "format must be a string")
    try:
        datetime.strptime("2000-01-01", date_format)
    except ValueError as exc:
        raise _error(source, f"{field_name}.format", f"invalid date format: {exc}") from exc
    return FieldRule(
        required=_bool(item.get("required", False), source, f"{field_name}.required"),
        type=rule_type,
        pattern=pattern,
        enum=enum,
        minimum=None if item.get("min") is None else _number(item.get("min"), source, f"{field_name}.min"),
        maximum=None if item.get("max") is None else _number(item.get("max"), source, f"{field_name}.max"),
        date_format=date_format,
    )

def _coerce_literal(rule: FieldRule, value: Any) -> bool:
    """Local coercion probe so ``when`` values can be checked at load time."""
    from .validate_fields import coerce

    valid, _converted = coerce(rule, value)
    return valid

def _parse_when(raw: Any, source: Path, field_name: str, fields: dict[str, dict[str, FieldRule]], screen_id: str) -> WhenCondition | None:
    if raw is None:
        return None
    item = mapping(raw, source, field_name)
    condition_field = text(item.get("field"), source, f"{field_name}.field")
    has_equals = "equals" in item
    has_in = "in" in item
    if has_equals == has_in:
        raise _error(source, field_name, "when requires exactly one of equals or in")
    if has_equals:
        accepted: tuple[Any, ...] = (item.get("equals"),)
    else:
        accepted_raw = list_value(item.get("in"), source, f"{field_name}.in")
        if not accepted_raw:
            raise _error(source, f"{field_name}.in", "in must not be empty")
        accepted = tuple(accepted_raw)
    rule = fields.get(screen_id, {}).get(condition_field)
    if rule is not None:
        for index, value in enumerate(accepted):
            if not _coerce_literal(rule, value):
                raise _error(source, f"{field_name}.accepted[{index}]", f"value {value!r} can never satisfy {condition_field} of type {rule.type}")
    return WhenCondition(field=condition_field, accepted=accepted)

def _parse_record_checks(raw: Any, source: Path, fields: dict[str, dict[str, FieldRule]]) -> tuple[RecordCheck, ...]:
    if raw is None:
        return ()
    checks: list[RecordCheck] = []
    seen: set[str] = set()
    for index, value in enumerate(list_value(raw, source, "record_checks")):
        field_name = f"record_checks[{index}]"
        item = mapping(value, source, field_name)
        check_id = text(item.get("id"), source, f"{field_name}.id")
        if check_id in seen:
            raise _error(source, f"{field_name}.id", "duplicate check id")
        seen.add(check_id)
        left = text(item.get("left"), source, f"{field_name}.left")
        op = text(item.get("op"), source, f"{field_name}.op")
        if op not in OPS:
            raise _error(source, f"{field_name}.op", f"op must be one of {sorted(OPS)}")
        has_field = item.get("right_field") is not None
        has_value = "right" in item
        if has_field == has_value:
            raise _error(source, field_name, "exactly one of right_field or right is required")
        screen_id = text(item.get("screen_id"), source, f"{field_name}.screen_id")
        if has_value:
            left_rule = fields.get(screen_id, {}).get(left)
            if left_rule is not None and not _coerce_literal(left_rule, item.get("right")):
                raise _error(source, f"{field_name}.right", f"value {item.get('right')!r} can never compare against {left} of type {left_rule.type}")
        checks.append(
            RecordCheck(
                check_id=check_id,
                screen_id=screen_id,
                left=left,
                op=op,
                right_field=(text(item.get("right_field"), source, f"{field_name}.right_field") if has_field else None),
                right_value=item.get("right") if has_value else None,
                message=str(item.get("message", f"{left} {op} right")),
                when=_parse_when(item.get("when"), source, f"{field_name}.when", fields, screen_id),
            )
        )
    return tuple(checks)

def _parse_batch_checks(raw: Any, source: Path) -> tuple[BatchCheck, ...]:
    if raw is None:
        return ()
    checks: list[BatchCheck] = []
    seen: set[str] = set()
    for index, value in enumerate(list_value(raw, source, "batch_checks")):
        field_name = f"batch_checks[{index}]"
        item = mapping(value, source, field_name)
        check_id = text(item.get("id"), source, f"{field_name}.id")
        if check_id in seen:
            raise _error(source, f"{field_name}.id", "duplicate check id")
        seen.add(check_id)
        check = text(item.get("check"), source, f"{field_name}.check")
        if check not in BATCH_CHECKS:
            raise _error(source, f"{field_name}.check", f"check must be one of {sorted(BATCH_CHECKS)}")
        key_raw = list_value(item.get("key", []), source, f"{field_name}.key")
        key = tuple(text(entry, source, f"{field_name}.key[{i}]") for i, entry in enumerate(key_raw))
        if check == "unique_key" and not key:
            raise _error(source, f"{field_name}.key", "unique_key requires key fields")
        if check in {"sum_equals", "count_equals"}:
            if item.get("field") is None or item.get("fact") is None:
                raise _error(source, field_name, f"{check} requires field and fact")
        tolerance = _number(item.get("tolerance", 0.0), source, f"{field_name}.tolerance")
        if tolerance < 0:
            raise _error(source, f"{field_name}.tolerance", "tolerance must be >= 0")
        if not math.isfinite(tolerance):
            raise _error(source, f"{field_name}.tolerance", "tolerance must be finite")
        checks.append(
            BatchCheck(
                check_id=check_id,
                check=check,
                screen_id=text(item.get("screen_id"), source, f"{field_name}.screen_id"),
                key=key,
                field_name=(None if item.get("field") is None else text(item.get("field"), source, f"{field_name}.field")),
                fact=(None if item.get("fact") is None else text(item.get("fact"), source, f"{field_name}.fact")),
                tolerance=tolerance,
                message=str(item.get("message", f"batch check {check_id} failed")),
            )
        )
    return tuple(checks)

def _parse_policy(raw: Any, source: Path) -> str:
    if raw is None:
        return POLICY_STRICT
    policy = text(raw, source, "policy")
    if policy not in POLICIES:
        raise _error(source, "policy", f"policy must be one of {sorted(POLICIES)}")
    return policy

def load_validation_config(path: str | Path) -> ValidationConfig:
    source = Path(path)
    doc = load_yaml_document(source)
    fields: dict[str, dict[str, FieldRule]] = {}
    fields_raw = mapping(doc.get("fields", {}), source, "fields")
    for screen_id, rules_raw in fields_raw.items():
        if not isinstance(screen_id, str):
            raise _error(source, "fields", "screen ids must be strings")
        screen_rules = mapping(rules_raw, source, f"fields.{screen_id}")
        parsed: dict[str, FieldRule] = {}
        for field_name, rule_raw in screen_rules.items():
            parsed[field_name] = _parse_field_rule(rule_raw, source, f"fields.{screen_id}.{field_name}")
        fields[screen_id] = parsed
    return ValidationConfig(
        fields=fields,
        record_checks=_parse_record_checks(doc.get("record_checks"), source, fields),
        batch_checks=_parse_batch_checks(doc.get("batch_checks"), source),
        policy=_parse_policy(doc.get("policy"), source),
    )
