"""Primitive YAML/type validation and loopback target checks."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import yaml

from .errors import E_CONFIG_BAD_TARGET, E_CONFIG_PARSE, E_CONFIG_SCHEMA, HarvestError

def load_yaml_document(path: str | Path) -> dict[str, Any]:
    """Load a YAML mapping, preserving file/line information in errors."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarvestError(E_CONFIG_PARSE, f"cannot read YAML: {exc}", context={"file": str(source)}) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        context = {"file": str(source)}
        if mark is not None:
            context["line"] = mark.line + 1
            context["column"] = mark.column + 1
        raise HarvestError(E_CONFIG_PARSE, "invalid YAML", context=context, cause=exc) from exc
    if not isinstance(document, dict):
        raise HarvestError(E_CONFIG_SCHEMA, "YAML root must be a mapping", context={"file": str(source), "field": "$"})
    return document

def schema_error(message: str, path: Path, field_name: str) -> HarvestError:
    return HarvestError(E_CONFIG_SCHEMA, message, context={"file": str(path), "field": field_name})

def mapping(value: Any, path: Path, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise schema_error("expected a mapping", path, field_name)
    return value

def list_value(value: Any, path: Path, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise schema_error("expected a list", path, field_name)
    return value

def text(value: Any, path: Path, field_name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise schema_error("expected a non-empty string", path, field_name)
    return value

def integer(value: Any, path: Path, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise schema_error("expected an integer", path, field_name)
    if minimum is not None and value < minimum:
        raise schema_error(f"must be >= {minimum}", path, field_name)
    return value

def string_list(value: Any, path: Path, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    items = list_value(value, path, field_name)
    if not items and not allow_empty:
        raise schema_error("must contain at least one item", path, field_name)
    return tuple(text(item, path, f"{field_name}[{index}]") for index, item in enumerate(items))

def is_loopback_host(host: str) -> bool:
    """Strict, DNS-free loopback whitelist."""
    if not isinstance(host, str):
        return False
    candidate = host.strip().lower().rstrip(".")
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate.strip("[]"))
    except ValueError:
        return False
    return address.is_loopback

def validate_target_parts(host: Any, port: Any, *, source: str = "<target>") -> tuple[str, int]:
    if not isinstance(host, str) or not host.strip():
        raise HarvestError(E_CONFIG_BAD_TARGET, "target host must be a non-empty string", context={"file": source, "field": "target.host"})
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise HarvestError(E_CONFIG_BAD_TARGET, "target port must be 1..65535", context={"file": source, "field": "target.port"})
    if not is_loopback_host(host):
        raise HarvestError(E_CONFIG_BAD_TARGET, "target host must be loopback", context={"file": source, "field": "target.host", "host": host})
    return host.strip(), port
