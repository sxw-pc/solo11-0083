"""Field-level coercion and checks."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import E_FIELD_ENUM, E_FIELD_FORMAT, E_FIELD_PATTERN, E_FIELD_RANGE, E_FIELD_REQUIRED, E_FIELD_TYPE
from .validate_config import FieldRule, ValidationConfig

INTEGER_RE = re.compile(r"^-?[0-9]+$")

def issue(code: str, *, rule_id: str, screen_id: str, record_key: str | None, field_name: str | None, message: str) -> dict[str, Any]:
    return {"code": code, "rule_id": rule_id, "screen_id": screen_id, "record_key": record_key, "field": field_name, "message": message}

def coerce(rule: FieldRule, value: Any) -> tuple[bool, Any]:
    if rule.type == "string":
        if isinstance(value, str):
            return True, value
        return False, None
    if rule.type == "integer":
        if isinstance(value, int) and not isinstance(value, bool):
            return True, value
        if not isinstance(value, str) or not INTEGER_RE.fullmatch(value):
            return False, None
        return True, int(value)
    if rule.type == "decimal":
        if isinstance(value, bool):
            return False, None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return False, None
        if not number.is_finite():
            return False, None
        return True, number
    if rule.type == "date":
        if not isinstance(value, str):
            return False, None
        try:
            return True, datetime.strptime(value, rule.date_format).date()
        except ValueError:
            return False, None
    return False, None

def _range_has(converted: Any, limit: float, *, below: bool) -> bool:
    """Return True when a value violates a numeric/date range."""
    if isinstance(converted, Decimal):
        limit_value: Any = Decimal(str(limit))
    else:
        limit_value = limit
    try:
        return converted < limit_value if below else converted > limit_value
    except TypeError:
        return False

def validate_field(screen_id: str, record: dict[str, Any], field_name: str, rule: FieldRule) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    value = record.get("normalized_values", {}).get(field_name)
    rule_id = f"field:{screen_id}.{field_name}"
    record_key = record.get("record_key")

    def add(code: str, message: str) -> None:
        errors.append(issue(code, rule_id=rule_id, screen_id=screen_id, record_key=record_key, field_name=field_name, message=message))

    if value is None or value == "":
        if rule.required:
            add(E_FIELD_REQUIRED, f"{field_name} is required")
        return errors
    valid, converted = coerce(rule, value)
    if not valid:
        add(E_FIELD_TYPE, f"{field_name} is not a valid {rule.type}")
        return errors
    if rule.pattern is not None and not re.fullmatch(rule.pattern, str(value)):
        add(E_FIELD_PATTERN, f"{field_name} does not match {rule.pattern}")
    if rule.enum is not None and str(value) not in rule.enum:
        add(E_FIELD_ENUM, f"{field_name} must be one of {list(rule.enum)}")
    if rule.minimum is not None and _range_has(converted, rule.minimum, below=True):
        add(E_FIELD_RANGE, f"{field_name} is below {rule.minimum}")
    if rule.maximum is not None and _range_has(converted, rule.maximum, below=False):
        add(E_FIELD_RANGE, f"{field_name} is above {rule.maximum}")
    if rule.type == "date" and value != converted.strftime(rule.date_format):
        add(E_FIELD_FORMAT, f"{field_name} must use {rule.date_format}")
    return errors

def convert_for_compare(config: ValidationConfig, screen_id: str, name: str, value: Any) -> Any:
    rule = config.fields.get(screen_id, {}).get(name)
    if rule is None:
        return str(value) if value is not None else None
    valid, converted = coerce(rule, value)
    return converted if valid else None

def compare(left: Any, op: str, right: Any) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    return False
