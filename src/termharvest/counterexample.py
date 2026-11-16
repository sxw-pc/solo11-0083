"""Offline counterexample cassette generator.

Given a cassette that *passes* validation, the screen/session contracts and
one target ``check_id``, find the smallest edit of raw 24x80 screen cells
that flips exactly that check from pass to fail. Every candidate is
re-verified end to end (cassette hashes/sequence/trailer, replay
expectations, extraction and the full validation suite); a candidate only
counts when the target check fails, every other rule outcome is identical
to the baseline and every non-target record keeps its normalized values.

The output is a fully replayable cassette plus a deterministic change
proof. No network access is ever performed.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .capture import CapturedFrame
from .cassette import Cassette, dumps_cassette, load_cassette, loads_cassette
from .config_types import ScreenContract, ScreenField, SessionContract
from .errors import (
    E_COUNTEREXAMPLE_BASELINE,
    E_COUNTEREXAMPLE_LIMIT,
    E_COUNTEREXAMPLE_NO_SOLUTION,
    E_COUNTEREXAMPLE_UNSUPPORTED,
    E_INTERNAL,
    HarvestError,
)
from .extract import ValueRegion, list_record_rows, locate_value_regions, make_record_key
from .pipeline_steps import _extract_all
from .replay import verify_replay
from .screen import FRAME_COLS, ScreenFrame, normalize_value
from .validate import rule_outcomes, validate_records
from .validate_config import CheckTarget, ValidationConfig, find_check

DEFAULT_MAX_CANDIDATES = 256
SUM_STRATEGIES = ("fact", "amount")
TYPE_PROBE_VALUES = ("INVALID", "X")
MASKED = "***"

@dataclass(frozen=True)
class CounterexampleResult:
    """Paths and proof of a generated counterexample."""

    check_id: str
    strategy: str
    cassette_path: str
    proof_path: str
    proof: dict[str, Any]
    stats: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "check_id": self.check_id,
            "strategy": self.strategy,
            "cassette": self.cassette_path,
            "proof": self.proof_path,
            "stats": self.stats,
        }

@dataclass(frozen=True)
class _CellEdit:
    frame_seq: int
    field_name: str
    region: ValueRegion
    text: str

@dataclass(frozen=True)
class _Candidate:
    strategy: str
    route_index: int
    enum_index: int
    edits: tuple[_CellEdit, ...]
    target_fields: frozenset[tuple[str, str]]
    target_facts: frozenset[tuple[str, str]]

@dataclass(frozen=True)
class _FieldSlot:
    frame_seq: int
    region: ValueRegion
    record_key: str | None
    raw: str

@dataclass
class _World:
    session: SessionContract
    screens: dict[str, ScreenContract]
    config: ValidationConfig
    cassette: Cassette
    frames: list[CapturedFrame]
    records: list[dict[str, Any]]
    facts: dict[str, dict[str, Any]]
    outcomes: dict[str, str]
    step_contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    frames_by_seq: dict[int, CapturedFrame] = field(default_factory=dict)
    provenance: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)

def _unsupported(message: str, **context: Any) -> HarvestError:
    return HarvestError(E_COUNTEREXAMPLE_UNSUPPORTED, message, context=context)

def _screen_frames(world: _World, screen_id: str) -> list[CapturedFrame]:
    return [item for item in world.frames if item.expected_screen == screen_id]

def _screen_field(contract: ScreenContract, name: str) -> ScreenField | None:
    for item in (*contract.fields, *contract.summary_fields):
        if item.name == name:
            return item
    return None

def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None

def _decimals_of(raw: str) -> int:
    _head, sep, tail = raw.strip().partition(".")
    return len(tail) if sep else 0

def _numeric_variants(raw: str, values: list[Decimal]) -> list[str]:
    decimals = _decimals_of(raw)
    texts: list[str] = []
    for value in values:
        text = format(value, f".{decimals}f")
        if text not in texts:
            texts.append(text)
    return texts

def _sum_deltas(raw: str) -> list[Decimal]:
    unit = Decimal(1).scaleb(-_decimals_of(raw))
    deltas = [Decimal(1), Decimal(-1), unit, -unit]
    unique: list[Decimal] = []
    for delta in deltas:
        if delta not in unique:
            unique.append(delta)
    return unique

def _list_provenance(world: _World, contract: ScreenContract) -> list[tuple[int, int, str]]:
    """Map each extracted list record to its (frame_seq, row, record_key)."""
    if contract.screen_id in world.provenance:
        return world.provenance[contract.screen_id]
    region = contract.list_region
    if region is None:
        return []
    paging = world.session.paging if world.session.paging is not None and world.session.paging.screen_id == contract.screen_id else None
    max_pages = paging.max_pages if paging else None
    stop_marker = paging.stop_marker if paging else None
    stop_on_repeated = paging.stop_on_repeated_page if paging else True
    seen: set[str] = set()
    walk: list[tuple[int, int, str]] = []
    pages = 0
    previous_first: str | None = None
    for item in _screen_frames(world, contract.screen_id):
        if max_pages is not None and pages >= max_pages:
            break
        keys: list[str] = []
        rows = list_record_rows(item.frame, region, stop_marker)
        for row in rows:
            line = item.frame.rows[row]
            raw = {column.name: line[column.start : column.end].strip() for column in region.columns}
            normalized = {column.name: normalize_value(raw[column.name], ("trim", "collapse_space", "fullwidth_digits")) for column in region.columns}
            keys.append(make_record_key(contract.screen_id, contract.primary_key, normalized, raw))
        pages += 1
        if not keys:
            break
        if stop_on_repeated and previous_first is not None and keys[0] == previous_first:
            break
        for row, key in zip(rows, keys):
            if key in seen:
                continue
            seen.add(key)
            walk.append((item.frame_seq, row, key))
        previous_first = keys[0]
        if stop_marker and stop_marker in item.frame.text:
            break
    expected = [record["record_key"] for record in world.records if record.get("screen_id") == contract.screen_id]
    if [key for _, _, key in walk] != expected:
        raise HarvestError(E_INTERNAL, "record provenance does not match extraction", context={"screen_id": contract.screen_id})
    world.provenance[contract.screen_id] = walk
    return walk

def _record_field_slots(world: _World, contract: ScreenContract, field_name: str, reasons: dict[str, int]) -> list[_FieldSlot]:
    """Editable cells for a record-level field, in extraction order."""
    frames = _screen_frames(world, contract.screen_id)
    if not frames:
        reasons["screen_not_captured"] += 1
        return []
    if contract.list_region is not None:
        column = next((item for item in contract.list_region.columns if item.name == field_name), None)
        if column is None:
            return []
        slots: list[_FieldSlot] = []
        for frame_seq, row, key in _list_provenance(world, contract):
            frame = world.frames_by_seq[frame_seq].frame
            region = ValueRegion(row, column.start, column.end)
            slots.append(_FieldSlot(frame_seq, region, key, frame.rows[row][column.start : column.end].strip()))
        return slots
    field = _screen_field(contract, field_name)
    if field is None or field.sensitive:
        return []
    frame = frames[0].frame
    try:
        regions = locate_value_regions(frame, field.locator, field_name)
    except HarvestError:
        reasons["locator_error"] += 1
        return []
    record_key = next((record["record_key"] for record in world.records if record.get("screen_id") == contract.screen_id), None)
    return [_FieldSlot(frames[0].frame_seq, region, record_key, frame.rows[region.row][region.col_start : region.col_end].strip()) for region in regions]

def _fact_slots(world: _World, contract: ScreenContract, fact_name: str, reasons: dict[str, int]) -> list[_FieldSlot]:
    field = _screen_field(contract, fact_name)
    if field is None or field.sensitive:
        return []
    slots: list[_FieldSlot] = []
    for item in _screen_frames(world, contract.screen_id):
        try:
            regions = locate_value_regions(item.frame, field.locator, fact_name)
        except HarvestError:
            reasons["locator_error"] += 1
            continue
        for region in regions:
            slots.append(_FieldSlot(item.frame_seq, region, None, item.frame.rows[region.row][region.col_start : region.col_end].strip()))
    if not slots:
        reasons["no_editable_cells"] += 1
    return slots

def _forbidden_cells(world: _World) -> dict[int, set[tuple[int, int]]]:
    """Cells that hold screen-match fragments or step expectations."""
    forbidden: dict[int, set[tuple[int, int]]] = {}
    for item in world.frames:
        contract = world.screens.get(item.expected_screen)
        fragments: list[str] = list(contract.match) if contract is not None else []
        fragments.extend(world.step_contains.get(item.step_id, ()))
        cells: set[tuple[int, int]] = set()
        for fragment in fragments:
            if not fragment:
                continue
            start = item.frame.text.find(fragment)
            while start != -1:
                for offset in range(start, start + len(fragment)):
                    row, col = divmod(offset, FRAME_COLS + 1)
                    if col < FRAME_COLS:
                        cells.add((row, col))
                start = item.frame.text.find(fragment, start + 1)
        forbidden[item.frame_seq] = cells
    return forbidden

def _field_cells_by_frame(world: _World, contract: ScreenContract) -> dict[int, dict[str, set[tuple[int, int]]]]:
    """All located field cells per frame of the contract's screen."""
    result: dict[int, dict[str, set[tuple[int, int]]]] = {}
    paging = world.session.paging
    stop_marker = paging.stop_marker if paging is not None and paging.screen_id == contract.screen_id else None
    for item in _screen_frames(world, contract.screen_id):
        per_field: dict[str, set[tuple[int, int]]] = {}

        def add(name: str, regions: list[ValueRegion]) -> None:
            cells = per_field.setdefault(name, set())
            for region in regions:
                cells.update((region.row, col) for col in range(region.col_start, region.col_end))

        for field_item in (*contract.fields, *contract.summary_fields):
            try:
                add(field_item.name, locate_value_regions(item.frame, field_item.locator, field_item.name))
            except HarvestError:
                continue
        if contract.list_region is not None:
            for row in list_record_rows(item.frame, contract.list_region, stop_marker):
                for column in contract.list_region.columns:
                    per_field.setdefault(column.name, set()).update((row, col) for col in range(column.start, column.end))
        result[item.frame_seq] = per_field
    return result

def _sort_key(world: _World, candidate: _Candidate) -> tuple:
    cells = 0
    frames: set[int] = set()
    for edit in candidate.edits:
        frame = world.frames_by_seq[edit.frame_seq].frame
        region = edit.region
        old = frame.rows[region.row][region.col_start : region.col_end]
        new = edit.text + " " * (region.cells - len(edit.text))
        changed = sum(1 for before, after in zip(old, new) if before != after)
        if changed:
            cells += changed
            frames.add(edit.frame_seq)
    return (cells, len(frames), tuple(sorted(frames)), candidate.route_index, candidate.enum_index)

def _eligible(
    candidate: _Candidate,
    forbidden: dict[int, set[tuple[int, int]]],
    field_cells: dict[int, dict[str, set[tuple[int, int]]]],
) -> str | None:
    edited_names = {edit.field_name for edit in candidate.edits}
    for edit in candidate.edits:
        region = edit.region
        if len(edit.text) > region.cells:
            return "value_too_long"
        if any(char in edit.text for char in "\r\n\x00"):
            return "illegal_characters"
        cells = {(region.row, col) for col in range(region.col_start, region.col_end)}
        if cells & forbidden.get(edit.frame_seq, set()):
            return "protected_fragment"
        for name, occupied in field_cells.get(edit.frame_seq, {}).items():
            if name in edited_names:
                continue
            if cells & occupied:
                return f"locator_overlap:{name}"
    return None

def _group_slots(contract: ScreenContract, slots: list[_FieldSlot]) -> list[list[_FieldSlot]]:
    if contract.list_region is not None:
        return [[slot] for slot in slots]
    return [slots] if slots else []

def _edits_for_group(group: list[_FieldSlot], field_name: str, text: str) -> tuple[_CellEdit, ...]:
    edits = [_CellEdit(group[0].frame_seq, field_name, group[0].region, text)]
    edits.extend(_CellEdit(slot.frame_seq, field_name, slot.region, "") for slot in group[1:])
    return tuple(edits)

def _field_candidates(world: _World, target: CheckTarget, routes: list[str], reasons: dict[str, int]) -> list[_Candidate]:
    contract = world.screens.get(target.screen_id)
    if contract is None:
        raise _unsupported("no screen contract for the target screen", check_id=target.check_id, screen_id=target.screen_id)
    field_name = str(target.field_name)
    rule = target.field_rule
    assert rule is not None
    screen_field = _screen_field(contract, field_name)
    if screen_field is not None and screen_field.sensitive:
        raise _unsupported("target field is sensitive and must never be edited", check_id=target.check_id, field=field_name)
    if field_name in contract.primary_key:
        reasons["primary_key"] += 1
        return []
    slots = _record_field_slots(world, contract, field_name, reasons)
    if not slots:
        reasons["no_editable_cells"] += 1
        return []
    candidates: list[_Candidate] = []
    target_fields = frozenset({(target.screen_id, field_name)})
    enum = 0
    for route_index, route in enumerate(routes):
        for group in _group_slots(contract, slots):
            texts: list[str] = []
            if route == "required":
                texts = [""]
            elif route == "type":
                texts = list(TYPE_PROBE_VALUES)
            elif route == "range":
                raw = group[0].raw
                unit = Decimal(1).scaleb(-_decimals_of(raw))
                values: list[Decimal] = []
                if rule.minimum is not None:
                    minimum = Decimal(str(rule.minimum))
                    values.extend([minimum - 1, minimum - unit])
                if rule.maximum is not None:
                    maximum = Decimal(str(rule.maximum))
                    values.extend([maximum + 1, maximum + unit])
                texts = _numeric_variants(raw, values)
            for text in texts:
                candidates.append(_Candidate(f"field_{route}", route_index, enum, _edits_for_group(group, field_name, text), target_fields, frozenset()))
                enum += 1
    return candidates

def _compare_kind(config: ValidationConfig, target: CheckTarget, left_value: Any, right_value: Any) -> str | None:
    check = target.record_check
    assert check is not None
    left_rule = config.fields.get(check.screen_id, {}).get(check.left)
    if left_rule is not None and left_rule.type == "date":
        return "date"
    if left_rule is not None and left_rule.type in {"integer", "decimal"}:
        return "decimal" if _as_decimal(left_value) is not None and _as_decimal(right_value) is not None else None
    if _as_decimal(left_value) is not None and _as_decimal(right_value) is not None:
        return "decimal"
    return None

def _date_format_for(config: ValidationConfig, screen_id: str, field_name: str) -> str:
    rule = config.fields.get(screen_id, {}).get(field_name)
    return rule.date_format if rule is not None else "%Y-%m-%d"

def _flip_values(config: ValidationConfig, target: CheckTarget, side: str, left_value: Any, right_value: Any, raw: str, kind: str) -> list[str]:
    check = target.record_check
    assert check is not None
    op = check.op
    if kind == "decimal":
        left = _as_decimal(left_value)
        right = _as_decimal(right_value)
        if left is None or right is None:
            return []
        unit = Decimal(1).scaleb(-_decimals_of(raw))
        one = Decimal(1)
        table: dict[tuple[str, str], list[Decimal]] = {
            ("lte", "left"): [right + unit, right + one],
            ("lte", "right"): [left - unit, left - one],
            ("gte", "left"): [right - unit, right - one],
            ("gte", "right"): [left + unit, left + one],
            ("lt", "left"): [right, right + unit],
            ("lt", "right"): [left, left - unit],
            ("gt", "left"): [right, right - unit],
            ("gt", "right"): [left, left + unit],
            ("eq", "left"): [right + unit, right + one],
            ("eq", "right"): [left + unit],
            ("ne", "left"): [right],
            ("ne", "right"): [left],
        }
        return _numeric_variants(raw, table[(op, side)])
    day = timedelta(days=1)
    left_format = _date_format_for(config, check.screen_id, check.left)
    right_format = _date_format_for(config, check.screen_id, check.right_field) if check.right_field else left_format
    try:
        left = datetime.strptime(str(left_value), left_format)
        right = datetime.strptime(str(right_value), right_format)
    except (ValueError, TypeError):
        return []
    edited_format = left_format if side == "left" else right_format
    table = {
        ("lte", "left"): [right + day],
        ("lte", "right"): [left - day],
        ("gte", "left"): [right - day],
        ("gte", "right"): [left + day],
        ("lt", "left"): [right],
        ("lt", "right"): [left],
        ("gt", "left"): [right],
        ("gt", "right"): [left],
        ("eq", "left"): [right + day],
        ("eq", "right"): [left + day],
        ("ne", "left"): [right],
        ("ne", "right"): [left],
    }
    texts: list[str] = []
    for value in table[(op, side)]:
        text = value.strftime(edited_format)
        if text not in texts:
            texts.append(text)
    return texts

def _record_check_candidates(world: _World, target: CheckTarget, routes: list[str], reasons: dict[str, int]) -> list[_Candidate]:
    check = target.record_check
    assert check is not None
    contract = world.screens.get(check.screen_id)
    if contract is None:
        raise _unsupported("no screen contract for the target screen", check_id=target.check_id, screen_id=check.screen_id)
    involved = [check.left] + ([check.right_field] if check.right_field else [])
    for name in involved:
        screen_field = _screen_field(contract, str(name))
        if screen_field is not None and screen_field.sensitive:
            raise _unsupported("record check involves a sensitive field", check_id=target.check_id, field=name)
    records = [record for record in world.records if record.get("screen_id") == check.screen_id]
    if not records:
        reasons["no_records"] += 1
        return []
    sides = [("left", check.left)] + ([("right", check.right_field)] if "right" in routes and check.right_field else [])
    slot_cache: dict[str, list[_FieldSlot]] = {}
    candidates: list[_Candidate] = []
    enum = 0
    for record in records:
        values = record.get("normalized_values", {})
        left_value = values.get(check.left)
        right_value = values.get(check.right_field) if check.right_field else check.right_value
        kind = _compare_kind(world.config, target, left_value, right_value)
        if kind is None:
            raise _unsupported("record check operands are neither numeric nor dates", check_id=target.check_id)
        for route_index, (side, field_name) in enumerate(sides):
            if field_name in contract.primary_key:
                reasons["primary_key"] += 1
                continue
            if field_name not in slot_cache:
                slot_cache[field_name] = _record_field_slots(world, contract, str(field_name), reasons)
            slots = [slot for slot in slot_cache[field_name] if slot.record_key == record.get("record_key")]
            if not slots:
                reasons["no_editable_cells"] += 1
                continue
            texts = _flip_values(world.config, target, side, left_value, right_value, slots[0].raw, kind)
            for text in texts:
                edits = _edits_for_group(slots, str(field_name), text)
                candidates.append(
                    _Candidate(f"record_check_{side}", route_index, enum, edits, frozenset({(check.screen_id, str(field_name))}), frozenset())
                )
                enum += 1
    return candidates

def _fact_candidates(world: _World, target: CheckTarget, route_index: int, reasons: dict[str, int], *, integer_like: bool) -> list[_Candidate]:
    check = target.batch_check
    assert check is not None
    contract = world.screens.get(check.screen_id)
    assert contract is not None
    fact_name = str(check.fact)
    screen_field = _screen_field(contract, fact_name)
    if screen_field is not None and screen_field.sensitive:
        raise _unsupported("target fact is a sensitive field", check_id=target.check_id, fact=fact_name)
    candidates: list[_Candidate] = []
    enum = 0
    for slot in _fact_slots(world, contract, fact_name, reasons):
        base = _as_decimal(slot.raw)
        if base is None:
            reasons["unparseable_fact"] += 1
            continue
        deltas = [Decimal(1), Decimal(-1)] if integer_like else _sum_deltas(slot.raw)
        for text in _numeric_variants(slot.raw, [base + delta for delta in deltas]):
            candidates.append(
                _Candidate("fact", route_index, enum, (_CellEdit(slot.frame_seq, fact_name, slot.region, text),), frozenset(), frozenset({(check.screen_id, fact_name)}))
            )
            enum += 1
    return candidates

def _sum_equals_candidates(world: _World, target: CheckTarget, routes: list[str], reasons: dict[str, int]) -> list[_Candidate]:
    check = target.batch_check
    assert check is not None
    contract = world.screens.get(check.screen_id)
    if contract is None:
        raise _unsupported("no screen contract for the target screen", check_id=target.check_id, screen_id=check.screen_id)
    candidates: list[_Candidate] = []
    for route_index, route in enumerate(routes):
        if route == "fact":
            candidates.extend(_fact_candidates(world, target, route_index, reasons, integer_like=False))
            continue
        field_name = str(check.field_name)
        screen_field = _screen_field(contract, field_name)
        if screen_field is not None and screen_field.sensitive:
            raise _unsupported("target amount field is sensitive", check_id=target.check_id, field=field_name)
        if field_name in contract.primary_key:
            reasons["primary_key"] += 1
            continue
        enum = 0
        for slot in _record_field_slots(world, contract, field_name, reasons):
            base = _as_decimal(slot.raw)
            if base is None:
                reasons["unparseable_amount"] += 1
                continue
            for text in _numeric_variants(slot.raw, [base + delta for delta in _sum_deltas(slot.raw)]):
                candidates.append(
                    _Candidate(
                        "amount",
                        route_index,
                        enum,
                        (_CellEdit(slot.frame_seq, field_name, slot.region, text),),
                        frozenset({(check.screen_id, field_name)}),
                        frozenset(),
                    )
                )
                enum += 1
    return candidates

def _count_equals_candidates(world: _World, target: CheckTarget, routes: list[str], reasons: dict[str, int]) -> list[_Candidate]:
    check = target.batch_check
    assert check is not None
    if world.screens.get(check.screen_id) is None:
        raise _unsupported("no screen contract for the target screen", check_id=target.check_id, screen_id=check.screen_id)
    return _fact_candidates(world, target, 0, reasons, integer_like=True)

def _routes_for(target: CheckTarget, strategy: str | None) -> list[str]:
    if target.kind == "field":
        if strategy is not None:
            raise _unsupported("strategy applies only to sum_equals targets", check_id=target.check_id)
        rule = target.field_rule
        assert rule is not None
        routes: list[str] = []
        if rule.required:
            routes.append("required")
        if rule.type in {"integer", "decimal", "date"}:
            routes.append("type")
        if rule.minimum is not None or rule.maximum is not None:
            routes.append("range")
        if not routes:
            raise _unsupported(
                "field rule has no required/type/range lever (phase 1)", check_id=target.check_id, screen_id=target.screen_id, field=target.field_name
            )
        return routes
    if target.kind == "record_check":
        if strategy is not None:
            raise _unsupported("strategy applies only to sum_equals targets", check_id=target.check_id)
        check = target.record_check
        assert check is not None
        return ["left", "right"] if check.right_field else ["left"]
    check = target.batch_check
    assert check is not None
    if check.check == "sum_equals":
        if strategy is not None and strategy not in SUM_STRATEGIES:
            raise _unsupported("strategy must be one of fact|amount", check_id=target.check_id, strategy=strategy)
        return [strategy] if strategy is not None else list(SUM_STRATEGIES)
    if check.check == "count_equals":
        if strategy not in (None, "fact"):
            raise _unsupported("count_equals supports only the fact route", check_id=target.check_id, strategy=strategy)
        return ["fact"]
    raise _unsupported("batch check kind is not supported in phase 1", check_id=target.check_id, batch_check=check.check)

def _apply_edits(world: _World, edits: tuple[_CellEdit, ...]) -> list[tuple[int, str, str, ScreenFrame]]:
    rows_by_seq = {seq: list(item.frame.rows) for seq, item in world.frames_by_seq.items()}
    for edit in edits:
        region = edit.region
        rows = rows_by_seq[edit.frame_seq]
        line = rows[region.row]
        replacement = edit.text + " " * (region.cells - len(edit.text))
        rows[region.row] = line[: region.col_start] + replacement + line[region.col_end :]
    return [(item.frame_seq, item.step_id, item.expected_screen, ScreenFrame.from_rows(rows_by_seq[item.frame_seq])) for item in world.frames]

def _evaluate(world: _World, target: CheckTarget, candidate: _Candidate) -> tuple[bool, str, dict[str, Any]]:
    frames = _apply_edits(world, candidate.edits)
    text = dumps_cassette(session_id=world.cassette.session_id, frames=frames, captured_by=world.cassette.captured_by)
    try:
        rewritten = loads_cassette(text, source="<counterexample-candidate>")
        captured = verify_replay(rewritten, world.session)
        extractions, _pages = _extract_all(captured, world.screens, world.session)
    except HarvestError as exc:
        return False, f"replay:{exc.code}", {}
    records = [record for extraction in extractions for record in extraction.records]
    facts = {extraction.screen_id: extraction.facts for extraction in extractions}
    validation = validate_records(records, world.config, facts_by_screen=facts)
    outcomes = rule_outcomes(world.config, validation.errors)
    if outcomes.get(target.check_id) != "fail":
        return False, "target_not_flipped", {}
    flipped = [rule_id for rule_id, status in outcomes.items() if status != world.outcomes.get(rule_id)]
    if flipped != [target.check_id]:
        others = [rule_id for rule_id in flipped if rule_id != target.check_id]
        return False, "other_rules_changed:" + ",".join(others), {}
    if [record.get("record_key") for record in records] != [record.get("record_key") for record in world.records]:
        return False, "record_set_changed", {}
    changed_records: list[dict[str, Any]] = []
    for baseline_record, record in zip(world.records, records):
        before = baseline_record.get("normalized_values", {})
        after = record.get("normalized_values", {})
        if before == after:
            continue
        screen_id = str(record.get("screen_id"))
        changed_fields = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        for name in changed_fields:
            if (screen_id, name) not in candidate.target_fields:
                return False, f"record_tampered:{name}", {}
        changed_records.append(
            {
                "record_key": record.get("record_key"),
                "screen_id": screen_id,
                "fields": {name: {"old": before.get(name), "new": after.get(name)} for name in changed_fields},
            }
        )
    changed_facts: list[dict[str, Any]] = []
    for screen_id in sorted(set(world.facts) | set(facts)):
        before = world.facts.get(screen_id, {})
        after = facts.get(screen_id, {})
        for name in sorted(set(before) | set(after)):
            if before.get(name) == after.get(name):
                continue
            if (screen_id, name) not in candidate.target_facts:
                return False, f"fact_tampered:{name}", {}
            changed_facts.append({"screen_id": screen_id, "fact": name, "old": before.get(name), "new": after.get(name)})
    return True, "ok", {
        "outcomes": outcomes,
        "errors": validation.errors,
        "changed_records": changed_records,
        "changed_facts": changed_facts,
        "cassette_text": text,
    }

def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _build_proof(
    world: _World,
    target: CheckTarget,
    strategy: str,
    candidate: _Candidate,
    details: dict[str, Any],
    cassette_path: str | Path,
    stats: dict[str, Any],
) -> dict[str, Any]:
    sensitive = {
        item.name
        for contract in world.screens.values()
        for item in (*contract.fields, *contract.summary_fields)
        if item.sensitive
    }

    def present(name: str, value: Any) -> Any:
        if name in sensitive:
            return MASKED
        return value

    patches: list[dict[str, Any]] = []
    for edit in candidate.edits:
        region = edit.region
        item = world.frames_by_seq[edit.frame_seq]
        old = item.frame.rows[region.row][region.col_start : region.col_end].strip()
        new = edit.text.strip()
        patches.append(
            {
                "screen_id": item.expected_screen,
                "frame_seq": edit.frame_seq,
                "step_id": item.step_id,
                "field": edit.field_name,
                "row": region.row,
                "col_start": region.col_start,
                "col_end": region.col_end,
                "old": present(edit.field_name, old),
                "new": present(edit.field_name, new),
                "old_sha256": _short_hash(old),
                "new_sha256": _short_hash(new),
            }
        )
    failed_baseline = sorted(rule_id for rule_id, status in world.outcomes.items() if status == "fail")
    failed_now = sorted(rule_id for rule_id, status in details["outcomes"].items() if status == "fail")
    return {
        "version": 1,
        "kind": "counterexample_proof",
        "check_id": target.check_id,
        "strategy": strategy,
        "session_id": world.session.session_id,
        "input": {"cassette": str(cassette_path)},
        "outputs": {"cassette": "counterexample.jsonl", "proof": "proof.json"},
        "patches": patches,
        "baseline": {"rule_outcomes": world.outcomes, "failed_rules": failed_baseline},
        "counterexample": {"rule_outcomes": details["outcomes"], "failed_rules": failed_now},
        "changed_rules": [rule_id for rule_id in details["outcomes"] if details["outcomes"][rule_id] != world.outcomes.get(rule_id)],
        "changed_records": details["changed_records"],
        "changed_facts": details["changed_facts"],
        "stats": stats,
    }

def generate_counterexample(
    *,
    cassette_path: str | Path,
    session: SessionContract,
    screens: dict[str, ScreenContract],
    validation_config: ValidationConfig,
    check_id: str,
    output_dir: str | Path,
    strategy: str | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> CounterexampleResult:
    """Generate a minimal single-check counterexample cassette plus proof.

    Raises HarvestError with E_COUNTEREXAMPLE_BASELINE when the baseline
    replay or validation does not pass, E_COUNTEREXAMPLE_UNSUPPORTED for
    unknown or out-of-scope rules, E_COUNTEREXAMPLE_NO_SOLUTION when the
    bounded search is exhausted and E_COUNTEREXAMPLE_LIMIT when the
    candidate budget runs out first. Nothing is written unless a valid
    counterexample is found.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 0:
        raise _unsupported("max_candidates must be a non-negative integer", max_candidates=max_candidates)
    cassette = load_cassette(cassette_path)
    frames = verify_replay(cassette, session)
    extractions, _pages = _extract_all(frames, screens, session)
    records = [record for extraction in extractions for record in extraction.records]
    facts = {extraction.screen_id: extraction.facts for extraction in extractions}
    baseline = validate_records(records, validation_config, facts_by_screen=facts)
    outcomes = rule_outcomes(validation_config, baseline.errors)
    if not baseline.ok:
        failed = sorted(rule_id for rule_id, status in outcomes.items() if status == "fail")
        raise HarvestError(
            E_COUNTEREXAMPLE_BASELINE,
            "baseline cassette does not pass validation; refusing to generate",
            context={"check_id": check_id, "failed_rules": failed, "target_already_failing": check_id in failed},
        )
    target = find_check(validation_config, check_id)
    if target is None:
        raise _unsupported("unknown check_id", check_id=check_id)
    routes = _routes_for(target, strategy)

    step_contains: dict[str, tuple[str, ...]] = {"start": session.start.contains}
    step_contains.update({step.step_id: step.expect.contains for step in session.steps})
    world = _World(
        session=session,
        screens=screens,
        config=validation_config,
        cassette=cassette,
        frames=frames,
        records=records,
        facts=facts,
        outcomes=outcomes,
        step_contains=step_contains,
        frames_by_seq={item.frame_seq: item for item in frames},
    )

    reasons: dict[str, int] = defaultdict(int)
    if target.kind == "field":
        candidates = _field_candidates(world, target, routes, reasons)
    elif target.kind == "record_check":
        candidates = _record_check_candidates(world, target, routes, reasons)
    elif target.batch_check is not None and target.batch_check.check == "sum_equals":
        candidates = _sum_equals_candidates(world, target, routes, reasons)
    else:
        candidates = _count_equals_candidates(world, target, routes, reasons)

    forbidden = _forbidden_cells(world)
    cells_cache: dict[str, dict[int, dict[str, set[tuple[int, int]]]]] = {}

    def field_cells(screen_id: str) -> dict[int, dict[str, set[tuple[int, int]]]]:
        if screen_id not in cells_cache:
            contract = screens.get(screen_id)
            cells_cache[screen_id] = _field_cells_by_frame(world, contract) if contract is not None else {}
        return cells_cache[screen_id]

    ordered = sorted(candidates, key=lambda candidate: _sort_key(world, candidate))
    evaluated = 0
    skipped = 0
    reject_reasons: dict[str, int] = {}
    for candidate in ordered:
        if evaluated >= max_candidates:
            raise HarvestError(
                E_COUNTEREXAMPLE_LIMIT,
                "candidate budget exhausted before a valid counterexample was found",
                context={"check_id": check_id, "candidates_evaluated": evaluated, "max_candidates": max_candidates, "candidates_remaining": len(ordered) - evaluated - skipped},
            )
        reason = _eligible(candidate, forbidden, field_cells(target.screen_id))
        if reason is not None:
            skipped += 1
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            continue
        evaluated += 1
        valid, why, details = _evaluate(world, target, candidate)
        if not valid:
            reject_reasons[why] = reject_reasons.get(why, 0) + 1
            continue
        key = _sort_key(world, candidate)
        stats = {
            "candidates_evaluated": evaluated,
            "candidates_skipped": skipped,
            "cells_changed": key[0],
            "frames_affected": list(key[2]),
            "max_candidates": max_candidates,
        }
        strategy_name = candidate.strategy
        proof = _build_proof(world, target, strategy_name, candidate, details, cassette_path, stats)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        cassette_file = output / "counterexample.jsonl"
        cassette_file.write_text(details["cassette_text"], encoding="utf-8")
        proof_file = output / "proof.json"
        proof_file.write_text(json.dumps(proof, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return CounterexampleResult(
            check_id=check_id,
            strategy=strategy_name,
            cassette_path=str(cassette_file),
            proof_path=str(proof_file),
            proof=proof,
            stats=stats,
        )
    raise HarvestError(
        E_COUNTEREXAMPLE_NO_SOLUTION,
        "no bounded candidate flips only the target check",
        context={"check_id": check_id, "candidates_evaluated": evaluated, "candidates_skipped": skipped, "reasons": dict(sorted(reject_reasons.items())), "build_reasons": dict(sorted(reasons.items()))},
    )

__all__ = ["CounterexampleResult", "DEFAULT_MAX_CANDIDATES", "SUM_STRATEGIES", "generate_counterexample"]
