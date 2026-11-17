"""Field and list extraction from validated screen frames.

All slicing operates on the frame's final visible 24 x 80 grid
(``frame.rows``), never on the raw operation stream. Frames accepted under the
``bounded_draw`` input contract therefore extract exactly what an operator can
see, including cells overwritten by cursor movement or erased by CSI commands.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .config_types import Locator, ScreenContract, ScreenField
from .errors import E_EXTRACT_LOCATOR_OUT_OF_BOUNDS, HarvestError
from .screen import ScreenFrame, ensure_contract_match, ensure_fingerprint, find_label_row, normalize_value


@dataclass
class Extraction:
    screen_id: str
    frame_seq: int
    frame_sha256: str
    records: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, str | None] = field(default_factory=dict)
    facts_raw: dict[str, str] = field(default_factory=dict)
    sensitive_skipped: list[str] = field(default_factory=list)
    visible_sha256: str = ""
    input_mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "frame_seq": self.frame_seq,
            "frame_sha256": self.frame_sha256,
            "visible_sha256": self.visible_sha256,
            "input_mode": self.input_mode,
            "records": self.records,
            "facts": self.facts,
            "sensitive_skipped": self.sensitive_skipped,
        }


def _bounds_error(field_name: str, message: str, **context: Any) -> HarvestError:
    payload = {"field": field_name}
    payload.update(context)
    return HarvestError(E_EXTRACT_LOCATOR_OUT_OF_BOUNDS, message, context=payload)


def _locate(frame: ScreenFrame, locator: Locator, field_name: str) -> str:
    data = locator.data
    rows = frame.rows
    cols = len(rows[0]) if rows else 80
    if locator.kind == "absolute":
        row, col, length = data["row"], data["col"], data["length"]
        if not 0 <= row < len(rows):
            raise _bounds_error(field_name, "absolute row is outside the visible frame", row=row, rows=len(rows))
        if not 0 <= col < cols or col + length > cols:
            raise _bounds_error(field_name, "absolute column span is outside the visible grid", row=row, col=col, length=length, columns=cols)
        return rows[row][col : col + length].strip()

    if locator.kind == "label":
        first, last = data["search_rows"]
        row_index = find_label_row(frame, data["label"], first, last)
        if row_index is None:
            raise _bounds_error(field_name, "label was not found in search_rows", label=data["label"], search_rows=[first, last])
        line = rows[row_index]
        if data["direction"] == "right":
            start = line.find(data["label"]) + len(data["label"])
            while start < cols and line[start] in " :\t":
                start += 1
            if start >= cols:
                raise _bounds_error(field_name, "label value starts past the visible grid", row=row_index, col=start, columns=cols)
            length = data["length"]
            end = cols if length is None else min(cols, start + length)
            return line[start:end].strip()
        target_row = row_index + data["row_offset"]
        if not 0 <= target_row < len(rows):
            raise _bounds_error(field_name, "next_line value row is outside the visible frame", row=target_row, rows=len(rows))
        start = data["col"] or 0
        if not 0 <= start < cols:
            raise _bounds_error(field_name, "next_line value column is outside the visible grid", row=target_row, col=start, columns=cols)
        length = data["length"]
        end = cols if length is None else min(cols, start + length)
        return rows[target_row][start:end].strip()

    if locator.kind == "multiline":
        pieces: list[str] = []
        for row_index in data["rows"]:
            if not 0 <= row_index < len(rows):
                raise _bounds_error(field_name, "multiline row is outside the visible frame", row=row_index, rows=len(rows))
            col, length = data["col"], data["length"]
            if not 0 <= col < cols or col + length > cols:
                raise _bounds_error(field_name, "multiline column span is outside the visible grid", row=row_index, col=col, length=length, columns=cols)
            piece = rows[row_index][col : col + length].strip()
            if piece:
                pieces.append(piece)
        return data["join"].join(pieces)

    raise _bounds_error(field_name, f"unsupported locator kind {locator.kind!r}")


def _extract_fields(frame: ScreenFrame, fields: tuple[ScreenField, ...], *, skipped: list[str]) -> tuple[dict[str, str], dict[str, str | None]]:
    raw_values: dict[str, str] = {}
    normalized: dict[str, str | None] = {}
    for item in fields:
        if item.sensitive:
            skipped.append(item.name)
            continue
        raw = _locate(frame, item.locator, item.name)
        raw_values[item.name] = raw
        normalized[item.name] = normalize_value(raw, item.normalize)
    return raw_values, normalized


def make_record_key(screen_id: str, primary_key: tuple[str, ...] | list[str], normalized_values: dict[str, str | None], raw_values: dict[str, str]) -> str:
    """Stable natural key; falls back to canonical raw content."""
    if primary_key:
        payload: Any = [[name, normalized_values.get(name)] for name in primary_key]
    else:
        payload = sorted(raw_values.items())
    canonical = json.dumps({"screen_id": screen_id, "key": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record(contract: ScreenContract, frame: ScreenFrame, frame_seq: int, raw_values: dict[str, str], normalized: dict[str, str | None]) -> dict[str, Any]:
    primary = {name: normalized.get(name) for name in contract.primary_key}
    return {
        "screen_id": contract.screen_id,
        "frame_seq": frame_seq,
        "raw_values": raw_values,
        "normalized_values": normalized,
        "frame_sha256": frame.sha256,
        "visible_sha256": frame.visible_sha256,
        "input_mode": frame.input_mode,
        "primary_key": primary,
        "record_key": make_record_key(contract.screen_id, contract.primary_key, normalized, raw_values),
    }


def _list_page_records(frame: ScreenFrame, contract: ScreenContract, frame_seq: int, skipped: list[str], stop_marker: str | None) -> list[dict[str, Any]]:
    region = contract.list_region
    if region is None:
        return []
    if region.end_row > len(frame.rows):
        raise HarvestError(
            E_EXTRACT_LOCATOR_OUT_OF_BOUNDS,
            "list_region end_row is outside the visible frame",
            context={"screen_id": contract.screen_id, "end_row": region.end_row, "rows": len(frame.rows)},
        )
    records: list[dict[str, Any]] = []
    cols = len(frame.rows[0])
    for row_index in range(region.start_row, region.end_row, region.step):
        line = frame.rows[row_index]
        if stop_marker and stop_marker in line:
            break
        if region.skip_blank and not line.strip():
            continue
        raw: dict[str, str] = {}
        for column in region.columns:
            if column.end > cols:
                raise HarvestError(
                    E_EXTRACT_LOCATOR_OUT_OF_BOUNDS,
                    "list_region column span is outside the visible grid",
                    context={"screen_id": contract.screen_id, "column": column.name, "start": column.start, "end": column.end, "columns": cols},
                )
            raw[column.name] = line[column.start : column.end].strip()
        if not any(value for value in raw.values()):
            continue
        normalized = {column.name: normalize_value(raw[column.name], ("trim", "collapse_space", "fullwidth_digits")) for column in region.columns}
        records.append(_record(contract, frame, frame_seq, raw, normalized))
    return records


def extract_screen(frame: ScreenFrame, contract: ScreenContract, frame_seq: int, *, stop_marker: str | None = None) -> Extraction:
    ensure_contract_match(frame, screen_id=contract.screen_id, fragments=contract.match)
    ensure_fingerprint(frame, contract.fingerprint)
    skipped: list[str] = []
    facts_raw: dict[str, str] = {}
    facts: dict[str, str | None] = {}
    for item in contract.summary_fields:
        if item.sensitive:
            skipped.append(item.name)
            continue
        raw = _locate(frame, item.locator, item.name)
        facts_raw[item.name] = raw
        facts[item.name] = normalize_value(raw, item.normalize)

    result = Extraction(
        screen_id=contract.screen_id,
        frame_seq=frame_seq,
        frame_sha256=frame.sha256,
        facts=facts,
        facts_raw=facts_raw,
        sensitive_skipped=sorted(set(skipped)),
        visible_sha256=frame.visible_sha256,
        input_mode=frame.input_mode,
    )
    if contract.list_region is not None:
        result.records = _list_page_records(frame, contract, frame_seq, skipped, stop_marker)
        result.sensitive_skipped = sorted(set(skipped))
        return result

    raw_values, normalized = _extract_fields(frame, contract.fields, skipped=skipped)
    result.records = [_record(contract, frame, frame_seq, raw_values, normalized)]
    result.sensitive_skipped = sorted(set(skipped))
    return result
