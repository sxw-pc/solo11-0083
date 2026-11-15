"""Screen contract parsing: fields, locators and list regions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config_types import Column, ListRegion, Locator, ScreenContract, ScreenField
from .config_util import integer, list_value, load_yaml_document, mapping, schema_error, string_list, text
from .errors import E_CONFIG_BAD_LOCATOR, E_CONFIG_DUPLICATE_FIELD, E_CONFIG_PARSE, E_CONFIG_SCHEMA, HarvestError

NORMALIZE_RULES = frozenset({"trim", "collapse_space", "fullwidth_digits", "upper"})

def _parse_locator(raw: Any, path: Path, field_name: str) -> Locator:
    item = mapping(raw, path, field_name)
    kind = text(item.get("kind"), path, f"{field_name}.kind")
    if kind == "absolute":
        data = {
            "row": integer(item.get("row"), path, f"{field_name}.row", minimum=0),
            "col": integer(item.get("col"), path, f"{field_name}.col", minimum=0),
            "length": integer(item.get("length"), path, f"{field_name}.length", minimum=1),
        }
    elif kind == "label":
        rows = list_value(item.get("search_rows"), path, f"{field_name}.search_rows")
        if len(rows) != 2:
            raise schema_error("search_rows must contain [first, last]", path, f"{field_name}.search_rows")
        first = integer(rows[0], path, f"{field_name}.search_rows[0]", minimum=0)
        last = integer(rows[1], path, f"{field_name}.search_rows[1]", minimum=0)
        if last < first:
            raise schema_error("search_rows last must be >= first", path, f"{field_name}.search_rows")
        direction = item.get("direction", "right")
        if direction not in {"right", "next_line"}:
            raise schema_error("direction must be right or next_line", path, f"{field_name}.direction")
        length = item.get("length")
        data = {
            "label": text(item.get("label"), path, f"{field_name}.label"),
            "search_rows": [first, last],
            "direction": direction,
            "length": None if length is None else integer(length, path, f"{field_name}.length", minimum=1),
            "row_offset": integer(item.get("row_offset", 1), path, f"{field_name}.row_offset", minimum=1),
            "col": None if item.get("col") is None else integer(item.get("col"), path, f"{field_name}.col", minimum=0),
        }
    elif kind == "multiline":
        rows = list_value(item.get("rows"), path, f"{field_name}.rows")
        if len(rows) < 2:
            raise schema_error("multiline rows needs at least two rows", path, f"{field_name}.rows")
        data = {
            "rows": [integer(row, path, f"{field_name}.rows[{index}]", minimum=0) for index, row in enumerate(rows)],
            "col": integer(item.get("col"), path, f"{field_name}.col", minimum=0),
            "length": integer(item.get("length"), path, f"{field_name}.length", minimum=1),
            "join": text(item.get("join", " "), path, f"{field_name}.join", nonempty=False),
        }
    else:
        raise HarvestError(E_CONFIG_BAD_LOCATOR, f"unsupported locator kind {kind!r}", context={"file": str(path), "field": field_name, "kind": kind})
    col = data.get("col")
    if col is not None and col >= 80:
        raise HarvestError(E_CONFIG_BAD_LOCATOR, "column must be < 80", context={"file": str(path), "field": f"{field_name}.col"})
    if kind == "absolute" and data["col"] + data["length"] > 80:
        raise HarvestError(E_CONFIG_BAD_LOCATOR, "absolute locator extends past column 80", context={"file": str(path), "field": field_name})
    if kind == "multiline" and data["col"] + data["length"] > 80:
        raise HarvestError(E_CONFIG_BAD_LOCATOR, "multiline locator extends past column 80", context={"file": str(path), "field": field_name})
    return Locator(kind=kind, data=data)

def _parse_field(raw: Any, path: Path, field_name: str) -> ScreenField:
    item = mapping(raw, path, field_name)
    name = text(item.get("name"), path, f"{field_name}.name")
    normalize = item.get("normalize", ["trim", "collapse_space", "fullwidth_digits"])
    rules = string_list(normalize, path, f"{field_name}.normalize", allow_empty=True)
    unknown = set(rules) - NORMALIZE_RULES
    if unknown:
        raise schema_error(f"unknown normalize rules: {sorted(unknown)}", path, f"{field_name}.normalize")
    sensitive = item.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise schema_error("sensitive must be boolean", path, f"{field_name}.sensitive")
    locator = _parse_locator(item.get("locator"), path, f"{field_name}.locator")
    return ScreenField(name=name, locator=locator, normalize=rules, sensitive=sensitive)

def _parse_fields(raw: Any, path: Path, field_name: str) -> tuple[ScreenField, ...]:
    if raw is None:
        return ()
    fields: list[ScreenField] = []
    seen: set[str] = set()
    for index, item in enumerate(list_value(raw, path, field_name)):
        parsed = _parse_field(item, path, f"{field_name}[{index}]")
        if parsed.name in seen:
            raise HarvestError(E_CONFIG_DUPLICATE_FIELD, f"duplicate field name {parsed.name!r}", context={"file": str(path), "field": f"{field_name}[{index}].name"})
        seen.add(parsed.name)
        fields.append(parsed)
    return tuple(fields)

def _parse_list_region(raw: Any, path: Path) -> ListRegion | None:
    if raw is None:
        return None
    item = mapping(raw, path, "list_region")
    columns_raw = mapping(item.get("columns"), path, "list_region.columns")
    if not columns_raw:
        raise schema_error("list_region.columns must not be empty", path, "list_region.columns")
    columns: list[Column] = []
    for name, value in columns_raw.items():
        field_path = f"list_region.columns.{name}"
        spec = mapping(value, path, field_path)
        start = integer(spec.get("start"), path, f"{field_path}.start", minimum=0)
        end = integer(spec.get("end"), path, f"{field_path}.end", minimum=0)
        if end <= start:
            raise schema_error("end must be > start", path, f"{field_path}.end")
        if end > 80:
            raise schema_error("end must be <= 80", path, f"{field_path}.end")
        columns.append(Column(name=name, start=start, end=end))
    start_row = integer(item.get("start_row"), path, "list_region.start_row", minimum=0)
    end_row = integer(item.get("end_row"), path, "list_region.end_row", minimum=0)
    if end_row <= start_row:
        raise schema_error("end_row must be > start_row", path, "list_region.end_row")
    if end_row > 24:
        raise schema_error("end_row must be <= 24", path, "list_region.end_row")
    skip_blank = item.get("skip_blank", True)
    if not isinstance(skip_blank, bool):
        raise schema_error("skip_blank must be boolean", path, "list_region.skip_blank")
    return ListRegion(start_row=start_row, end_row=end_row, step=integer(item.get("step", 1), path, "list_region.step", minimum=1), skip_blank=skip_blank, columns=tuple(columns))

def load_screen(path: str | Path) -> ScreenContract:
    source = Path(path)
    doc = load_yaml_document(source)
    screen_id = text(doc.get("screen_id"), source, "screen_id")
    match = string_list(doc.get("match"), source, "match")
    fields = _parse_fields(doc.get("fields"), source, "fields")
    summary = _parse_fields(doc.get("summary_fields"), source, "summary_fields")
    list_region = _parse_list_region(doc.get("list_region"), source)
    if not fields and list_region is None:
        raise schema_error("screen needs fields or list_region", source, "fields")
    primary_key = string_list(doc.get("primary_key", []), source, "primary_key", allow_empty=True)
    known = {item.name for item in fields}
    if list_region:
        known.update(column.name for column in list_region.columns)
    missing = set(primary_key) - known
    if missing:
        raise schema_error(f"primary_key fields not declared: {sorted(missing)}", source, "primary_key")
    fingerprint = doc.get("fingerprint")
    if fingerprint is not None:
        fingerprint = text(fingerprint, source, "fingerprint")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise schema_error("fingerprint must be sha256:<64 lowercase hex>", source, "fingerprint")
    return ScreenContract(screen_id=screen_id, match=match, fields=fields, summary_fields=summary, list_region=list_region, primary_key=primary_key, fingerprint=fingerprint, source_file=str(source))

def load_screens_dir(path: str | Path) -> dict[str, ScreenContract]:
    directory = Path(path)
    if not directory.is_dir():
        raise HarvestError(E_CONFIG_PARSE, "screens directory not found", context={"file": str(directory)})
    contracts: dict[str, ScreenContract] = {}
    for file_path in sorted(directory.glob("*.yaml")):
        contract = load_screen(file_path)
        if contract.screen_id in contracts:
            raise HarvestError(E_CONFIG_SCHEMA, f"duplicate screen_id {contract.screen_id!r}", context={"file": str(file_path), "field": "screen_id"})
        contracts[contract.screen_id] = contract
    if not contracts:
        raise HarvestError(E_CONFIG_PARSE, "no screen contracts found", context={"file": str(directory)})
    return contracts
