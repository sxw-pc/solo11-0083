"""Redacted screen-capture packages for unauthorized-field verifiers.

A redacted package is a JSONL artifact that can be handed to a verifier who is
not allowed to see sensitive field values.  Raw frames are only ever inspected
in local memory for contract verification; the export never writes sensitive
values into the package, into reports, or into error contexts.

Two digests are kept strictly apart:

* ``source_frame_sha256`` — a provenance claim about the original frame.  It
  cannot be re-verified from the package (the original cells are gone).
* ``redacted_sha256`` — a self-verifiable content digest of the redacted
  frame that the verifier recomputes from the package itself.

The header binds the screen-contract digest and the redaction rules version;
neither is a substitute for the per-frame content digests above.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from .capture import CapturedFrame
from .config_types import ScreenContract, ScreenField, SessionContract
from .errors import (
    E_REDACT_CONTRACT_MISMATCH,
    E_REDACT_LABEL_AMBIGUOUS,
    E_REDACT_LOCATOR_OUT_OF_BOUNDS,
    E_REDACT_OVERLAP,
    E_REDACT_PACKAGE_INCOMPLETE,
    E_REDACT_PACKAGE_MISMATCH,
    E_REDACT_PACKAGE_PARSE,
    E_REDACT_RESIDUAL_VALUE,
    HarvestError,
)
from .extract import Extraction
from .pipeline_steps import _extract_all
from .screen import FRAME_COLS, FRAME_ROWS, ScreenFrame, ensure_contract_match, ensure_fingerprint
from .validate import ValidationResult, validate_records
from .validate_config import ValidationConfig

REDACTION_RULES_VERSION = "1"
REDACTION_CHAR = "█"
PACKAGE_FORMAT = "termharvest-redacted-package"
PACKAGE_FORMAT_VERSION = 1
SCOPE_SPAN = "span"
SCOPE_ROW = "row"
SCOPES = (SCOPE_SPAN, SCOPE_ROW)

@dataclass(frozen=True)
class CellSpan:
    """A half-open character span [start, end) on one row of the 24x80 grid."""

    row: int
    start: int
    end: int

    def overlaps(self, other: "CellSpan") -> bool:
        return self.row == other.row and self.start < other.end and other.start < self.end

    def contains(self, row: int, start: int, end: int) -> bool:
        return self.row == row and self.start <= start and end <= self.end

@dataclass(frozen=True)
class FieldRedaction:
    """Resolved redaction spans for one sensitive field (positions only)."""

    field: str
    spans: tuple[CellSpan, ...]

@dataclass(frozen=True)
class RedactedPackageFrame:
    frame_seq: int
    step_id: str
    expected_screen: str
    contracted: bool
    source_frame_sha256: str
    redacted_sha256: str
    projection_sha256: str | None
    redactions: tuple[FieldRedaction, ...]
    frame: ScreenFrame

@dataclass(frozen=True)
class RedactedPackage:
    session_id: str
    captured_by: str
    scope: str
    rules_version: str
    contract_sha256: str
    frames: tuple[RedactedPackageFrame, ...]
    results_sha256: str
    source_file: str

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Span resolution
# ---------------------------------------------------------------------------

def _strict_label_row(frame: ScreenFrame, label: str, first: int, last: int, *, screen_id: str, field_name: str) -> int:
    """Locate a sensitive label; refuse to guess when it is missing or repeated."""
    matches = [row_index for row_index in range(max(0, first), min(last, len(frame.rows) - 1) + 1) if label in frame.rows[row_index]]
    if not matches:
        raise HarvestError(
            E_REDACT_LOCATOR_OUT_OF_BOUNDS,
            "sensitive label was not found in search_rows",
            context={"screen_id": screen_id, "field": field_name, "label": label, "search_rows": [first, last]},
        )
    if len(matches) > 1:
        raise HarvestError(
            E_REDACT_LABEL_AMBIGUOUS,
            "sensitive label appears on multiple rows; refusing to guess which value to redact",
            context={"screen_id": screen_id, "field": field_name, "label": label, "rows": matches},
        )
    row_index = matches[0]
    if frame.rows[row_index].count(label) > 1:
        raise HarvestError(
            E_REDACT_LABEL_AMBIGUOUS,
            "sensitive label appears more than once on its row; refusing to guess which value to redact",
            context={"screen_id": screen_id, "field": field_name, "label": label, "row": row_index},
        )
    return row_index

def _out_of_bounds(screen_id: str, field_name: str, reason: str, **position: Any) -> HarvestError:
    context: dict[str, Any] = {"screen_id": screen_id, "field": field_name}
    context.update(position)
    return HarvestError(E_REDACT_LOCATOR_OUT_OF_BOUNDS, reason, context=context)

def _sensitive_spans(frame: ScreenFrame, item: ScreenField, *, screen_id: str) -> tuple[CellSpan, ...]:
    """Resolve a sensitive field locator to grid spans, strictly.

    Any ambiguity or out-of-grid resolution blocks the export; the exporter
    must never guess which characters to hide.
    """
    data = item.locator.data
    kind = item.locator.kind
    name = item.name
    if kind == "absolute":
        row, col, length = data["row"], data["col"], data["length"]
        if row >= FRAME_ROWS:
            raise _out_of_bounds(screen_id, name, "absolute row is outside the frame", row=row)
        if col + length > FRAME_COLS:
            raise _out_of_bounds(screen_id, name, "absolute span extends past column 80", row=row, col=col, length=length)
        return (CellSpan(row, col, col + length),)
    if kind == "label":
        first, last = data["search_rows"]
        row_index = _strict_label_row(frame, data["label"], first, last, screen_id=screen_id, field_name=name)
        if data["direction"] == "right":
            start = frame.rows[row_index].find(data["label"]) + len(data["label"])
            while start < FRAME_COLS and frame.rows[row_index][start] in " :\t":
                start += 1
            length = data["length"]
            end = FRAME_COLS if length is None else start + length
            if start >= FRAME_COLS or end > FRAME_COLS:
                raise _out_of_bounds(screen_id, name, "label/right value span is outside the frame", row=row_index, col=start, length=length)
            return (CellSpan(row_index, start, end),)
        target_row = row_index + data["row_offset"]
        if target_row >= FRAME_ROWS:
            raise _out_of_bounds(screen_id, name, "next_line value row is outside the frame", row=target_row)
        start = data["col"] or 0
        length = data["length"]
        end = FRAME_COLS if length is None else start + length
        if start >= FRAME_COLS or end > FRAME_COLS:
            raise _out_of_bounds(screen_id, name, "next_line value span is outside the frame", row=target_row, col=start, length=length)
        return (CellSpan(target_row, start, end),)
    if kind == "multiline":
        spans: list[CellSpan] = []
        for row_index in data["rows"]:
            if row_index >= FRAME_ROWS:
                raise _out_of_bounds(screen_id, name, "multiline row is outside the frame", row=row_index)
            col, length = data["col"], data["length"]
            if col + length > FRAME_COLS:
                raise _out_of_bounds(screen_id, name, "multiline span extends past column 80", row=row_index, col=col, length=length)
            spans.append(CellSpan(row_index, col, col + length))
        return tuple(spans)
    raise _out_of_bounds(screen_id, name, f"unsupported locator kind {kind!r}")

def _extraction_spans(frame: ScreenFrame, item: ScreenField, *, screen_id: str) -> tuple[CellSpan, ...]:
    """Mirror ``extract._locate`` clamping for a non-sensitive field.

    The protected region is exactly what the extractor would read, so the
    overlap check guarantees replayed extraction sees identical values.
    """
    data = item.locator.data
    kind = item.locator.kind
    name = item.name
    if kind == "absolute":
        row, col, length = data["row"], data["col"], data["length"]
        if row >= len(frame.rows):
            raise _out_of_bounds(screen_id, name, "absolute row of a non-sensitive field is outside the frame", row=row)
        return (CellSpan(row, col, min(FRAME_COLS, col + length)),)
    if kind == "label":
        first, last = data["search_rows"]
        row_index = None
        for candidate in range(max(0, first), min(last, len(frame.rows) - 1) + 1):
            if data["label"] in frame.rows[candidate]:
                row_index = candidate
                break
        if row_index is None:
            raise _out_of_bounds(screen_id, name, "label of a non-sensitive field was not found in search_rows", label=data["label"])
        if data["direction"] == "right":
            start = frame.rows[row_index].find(data["label"]) + len(data["label"])
            while start < FRAME_COLS and frame.rows[row_index][start] in " :\t":
                start += 1
            length = data["length"]
            end = FRAME_COLS if length is None else min(FRAME_COLS, start + length)
            return (CellSpan(row_index, start, end),)
        target_row = row_index + data["row_offset"]
        if target_row >= len(frame.rows):
            raise _out_of_bounds(screen_id, name, "next_line row of a non-sensitive field is outside the frame", row=target_row)
        start = data["col"] or 0
        length = data["length"]
        end = FRAME_COLS if length is None else min(FRAME_COLS, start + length)
        return (CellSpan(target_row, start, end),)
    if kind == "multiline":
        spans = []
        for row_index in data["rows"]:
            if row_index >= len(frame.rows):
                raise _out_of_bounds(screen_id, name, "multiline row of a non-sensitive field is outside the frame", row=row_index)
            spans.append(CellSpan(row_index, data["col"], min(FRAME_COLS, data["col"] + data["length"])))
        return tuple(spans)
    raise _out_of_bounds(screen_id, name, f"unsupported locator kind {kind!r}")

def _match_fragment_spans(frame: ScreenFrame, fragments: Iterable[str]) -> list[tuple[CellSpan, str]]:
    spans: list[tuple[CellSpan, str]] = []
    for fragment in fragments:
        if not fragment:
            continue
        for row_index, row in enumerate(frame.rows):
            start = row.find(fragment)
            while start != -1:
                spans.append((CellSpan(row_index, start, start + len(fragment)), fragment))
                start = row.find(fragment, start + 1)
    return spans

def _protected_spans(frame: ScreenFrame, contract: ScreenContract) -> list[tuple[CellSpan, str]]:
    """Cells that must stay visible: non-sensitive fields and match fragments."""
    protected: list[tuple[CellSpan, str]] = []
    for item in (*contract.fields, *contract.summary_fields):
        if item.sensitive:
            continue
        for span in _extraction_spans(frame, item, screen_id=contract.screen_id):
            protected.append((span, f"field:{item.name}"))
    region = contract.list_region
    if region is not None:
        for row_index in range(region.start_row, region.end_row, region.step):
            for column in region.columns:
                protected.append((CellSpan(row_index, column.start, column.end), f"list_column:{column.name}"))
    for span, fragment in _match_fragment_spans(frame, contract.match):
        protected.append((span, f"match_fragment:{fragment}"))
    return protected

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _row_scoped(spans: Iterable[CellSpan]) -> tuple[CellSpan, ...]:
    return tuple(CellSpan(row, 0, FRAME_COLS) for row in sorted({span.row for span in spans}))

def _check_overlap(contract: ScreenContract, field_name: str, spans: Iterable[CellSpan], protected: list[tuple[CellSpan, str]]) -> None:
    for span in spans:
        for other, owner in protected:
            if span.overlaps(other):
                raise HarvestError(
                    E_REDACT_OVERLAP,
                    "redaction would hide required non-sensitive content; refusing to export",
                    context={
                        "screen_id": contract.screen_id,
                        "field": field_name,
                        "row": span.row,
                        "span": [span.start, span.end],
                        "conflicts_with": owner,
                        "conflict_span": [other.row, other.start, other.end],
                    },
                )

def _check_residual(frame: ScreenFrame, contract: ScreenContract, value_redactions: list[FieldRedaction], coverage_spans: list[CellSpan]) -> None:
    """Refuse the export if a declared sensitive value survives anywhere else."""
    for item in value_redactions:
        for span in item.spans:
            piece = frame.rows[span.row][span.start : span.end].strip()
            if not piece:
                continue
            for row_index, row in enumerate(frame.rows):
                start = row.find(piece)
                while start != -1:
                    end = start + len(piece)
                    if not any(candidate.contains(row_index, start, end) for candidate in coverage_spans):
                        raise HarvestError(
                            E_REDACT_RESIDUAL_VALUE,
                            "sensitive value also appears outside the declared redaction spans; refusing to export",
                            context={"screen_id": contract.screen_id, "field": item.field, "row": row_index, "col": start},
                        )
                    start = row.find(piece, start + 1)

def resolve_redactions(frame: ScreenFrame, contract: ScreenContract, *, scope: str) -> tuple[FieldRedaction, ...]:
    """Resolve and validate redaction spans for one contracted frame."""
    sensitive_fields = [item for item in (*contract.fields, *contract.summary_fields) if item.sensitive]
    if not sensitive_fields:
        return ()
    resolved = [FieldRedaction(field=item.name, spans=_sensitive_spans(frame, item, screen_id=contract.screen_id)) for item in sensitive_fields]
    protected = _protected_spans(frame, contract)
    effective: list[FieldRedaction] = []
    for item in resolved:
        spans = _row_scoped(item.spans) if scope == SCOPE_ROW else item.spans
        _check_overlap(contract, item.field, spans, protected)
        effective.append(FieldRedaction(field=item.field, spans=spans))
    coverage = [span for item in effective for span in item.spans]
    _check_residual(frame, contract, resolved, coverage)
    return tuple(effective)

def redact_frame(frame: ScreenFrame, redactions: Iterable[FieldRedaction]) -> ScreenFrame:
    """Mask the resolved spans; the result is still a valid 24x80 frame."""
    rows = [list(row) for row in frame.rows]
    for item in redactions:
        for span in item.spans:
            for col in range(span.start, span.end):
                rows[span.row][col] = REDACTION_CHAR
    return ScreenFrame.from_rows(["".join(row) for row in rows])

def projection_digest(rows: tuple[str, ...] | list[str], redactions: Iterable[FieldRedaction]) -> str:
    """Fingerprint of the non-sensitive visible cells only.

    Redacted cells are canonically blanked to spaces, so the digest is a
    function of exactly the cells a verifier is allowed to see.  It is
    computed over the raw frame at export time and recomputed over the
    redacted frame at replay time; the two match iff every non-sensitive
    cell is identical.
    """
    grid = [list(row) for row in rows]
    for item in redactions:
        for span in item.spans:
            for col in range(span.start, span.end):
                grid[span.row][col] = " "
    return _sha256_text("\n".join("".join(row) for row in grid))

# ---------------------------------------------------------------------------
# Contract and results digests
# ---------------------------------------------------------------------------

def _contract_projection(contract: ScreenContract) -> dict[str, Any]:
    def field_projection(item: ScreenField) -> dict[str, Any]:
        return {"name": item.name, "sensitive": item.sensitive, "normalize": list(item.normalize), "locator": {"kind": item.locator.kind, "data": item.locator.data}}

    region = contract.list_region
    return {
        "screen_id": contract.screen_id,
        "match": list(contract.match),
        "primary_key": list(contract.primary_key),
        "fingerprint": contract.fingerprint,
        "fields": [field_projection(item) for item in contract.fields],
        "summary_fields": [field_projection(item) for item in contract.summary_fields],
        "list_region": None
        if region is None
        else {
            "start_row": region.start_row,
            "end_row": region.end_row,
            "step": region.step,
            "skip_blank": region.skip_blank,
            "columns": [{"name": column.name, "start": column.start, "end": column.end} for column in region.columns],
        },
    }

def contract_digest(screens: dict[str, ScreenContract]) -> str:
    """Digest of the screen contracts, excluding machine-specific file paths."""
    payload = {screen_id: _contract_projection(screens[screen_id]) for screen_id in sorted(screens)}
    return _sha256_text(_canonical(payload))

def _results_projection(extractions: list[Extraction], validation: ValidationResult) -> dict[str, Any]:
    """Non-sensitive extraction outcome; frame hashes are deliberately excluded."""
    projected_extractions: list[dict[str, Any]] = []
    for extraction in extractions:
        records = [
            {
                "screen_id": record["screen_id"],
                "frame_seq": record["frame_seq"],
                "raw_values": record["raw_values"],
                "normalized_values": record["normalized_values"],
                "primary_key": record["primary_key"],
                "record_key": record["record_key"],
            }
            for record in extraction.records
        ]
        projected_extractions.append(
            {
                "screen_id": extraction.screen_id,
                "frame_seq": extraction.frame_seq,
                "facts": extraction.facts,
                "facts_raw": extraction.facts_raw,
                "sensitive_skipped": extraction.sensitive_skipped,
                "records": records,
            }
        )
    return {"extractions": projected_extractions, "validation": validation.to_dict()}

def results_digest(extractions: list[Extraction], validation: ValidationResult) -> str:
    return _sha256_text(_canonical(_results_projection(extractions, validation)))

def _run_extraction(
    frames: list[CapturedFrame], screens: dict[str, ScreenContract], session: SessionContract, validation_config: ValidationConfig | None
) -> tuple[list[Extraction], ValidationResult]:
    extractions, _page_details = _extract_all(frames, screens, session)
    records = [record for extraction in extractions for record in extraction.records]
    facts = {extraction.screen_id: extraction.facts for extraction in extractions}
    if validation_config is not None:
        validation = validate_records(records, validation_config, facts_by_screen=facts)
    else:
        validation = ValidationResult(errors=[], stats={"records_in": len(records), "records_valid": len(records), "records_invalid": 0, "error_count": 0, "error_codes": []})
    return extractions, validation

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _redact_one(frame: ScreenFrame, contract: ScreenContract | None, *, scope: str) -> tuple[ScreenFrame, tuple[FieldRedaction, ...], str | None]:
    if contract is None:
        return frame, (), None
    ensure_contract_match(frame, screen_id=contract.screen_id, fragments=contract.match)
    ensure_fingerprint(frame, contract.fingerprint)
    redactions = resolve_redactions(frame, contract, scope=scope)
    redacted = redact_frame(frame, redactions)
    projection = projection_digest(frame.rows, redactions)
    return redacted, redactions, projection

def export_redacted_package(
    path: str | Path,
    *,
    frames: list[CapturedFrame],
    screens: dict[str, ScreenContract],
    session: SessionContract,
    validation_config: ValidationConfig | None = None,
    scope: str = SCOPE_SPAN,
    captured_by: str = "local",
) -> dict[str, Any]:
    """Write a redacted package from in-memory raw frames.

    Raw frames are used only in local memory for contract verification and
    for resolving the redaction spans; sensitive values are never written to
    the package or to error contexts.
    """
    if scope not in SCOPES:
        raise HarvestError(E_REDACT_CONTRACT_MISMATCH, f"unknown redaction scope {scope!r}", context={"scope": scope, "allowed": list(SCOPES)})
    redacted_frames: list[RedactedPackageFrame] = []
    for item in frames:
        contract = screens.get(item.expected_screen)
        redacted, redactions, projection = _redact_one(item.frame, contract, scope=scope)
        redacted_frames.append(
            RedactedPackageFrame(
                frame_seq=item.frame_seq,
                step_id=item.step_id,
                expected_screen=item.expected_screen,
                contracted=contract is not None,
                source_frame_sha256=item.frame.sha256,
                redacted_sha256=redacted.sha256,
                projection_sha256=projection,
                redactions=redactions,
                frame=redacted,
            )
        )
    extractions, validation = _run_extraction(frames, screens, session, validation_config)
    digest = results_digest(extractions, validation)
    header = {
        "record_type": "header",
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_FORMAT_VERSION,
        "rules_version": REDACTION_RULES_VERSION,
        "scope": scope,
        "session_id": session.session_id,
        "captured_by": captured_by,
        "contract_sha256": contract_digest(screens),
        "redaction_char": REDACTION_CHAR,
    }
    lines = [_canonical(header)]
    for item in redacted_frames:
        lines.append(
            _canonical(
                {
                    "record_type": "frame",
                    "frame_seq": item.frame_seq,
                    "step_id": item.step_id,
                    "expected_screen": item.expected_screen,
                    "contracted": item.contracted,
                    "source_frame_sha256": item.source_frame_sha256,
                    "redacted_sha256": item.redacted_sha256,
                    "projection_sha256": item.projection_sha256,
                    "redactions": [{"field": entry.field, "spans": [[span.row, span.start, span.end] for span in entry.spans]} for entry in item.redactions],
                    "text": item.frame.text,
                }
            )
        )
    lines.append(_canonical({"record_type": "trailer", "frame_count": len(redacted_frames), "results_sha256": digest}))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "package": str(target),
        "scope": scope,
        "frames": len(redacted_frames),
        "redacted_fields": sorted({entry.field for item in redacted_frames for entry in item.redactions}),
        "results_sha256": digest,
    }

# ---------------------------------------------------------------------------
# Package loading and replay verification
# ---------------------------------------------------------------------------

def _parse_redactions(raw: Any, *, source: str, line: int) -> tuple[FieldRedaction, ...]:
    if not isinstance(raw, list):
        raise HarvestError(E_REDACT_PACKAGE_PARSE, "redactions must be a list", context={"file": source, "line": line})
    parsed: list[FieldRedaction] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("field"), str) or not isinstance(entry.get("spans"), list):
            raise HarvestError(E_REDACT_PACKAGE_PARSE, "redaction entry must have field and spans", context={"file": source, "line": line})
        spans: list[CellSpan] = []
        for span in entry["spans"]:
            if not isinstance(span, list) or len(span) != 3 or not all(isinstance(value, int) and not isinstance(value, bool) for value in span):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "redaction span must be [row, start, end] integers", context={"file": source, "line": line})
            row, start, end = span
            if not (0 <= row < FRAME_ROWS and 0 <= start < end <= FRAME_COLS):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "redaction span is outside the 24x80 grid", context={"file": source, "line": line, "row": row})
            spans.append(CellSpan(row, start, end))
        parsed.append(FieldRedaction(field=entry["field"], spans=tuple(spans)))
    return tuple(parsed)

def load_redacted_package(path: str | Path) -> RedactedPackage:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarvestError(E_REDACT_PACKAGE_PARSE, f"cannot read redacted package: {exc}", context={"file": str(source)}) from exc

    header: dict[str, Any] | None = None
    frames: list[RedactedPackageFrame] = []
    trailer: dict[str, Any] | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarvestError(E_REDACT_PACKAGE_PARSE, "package line is not valid JSON", context={"file": str(source), "line": line_number}, cause=exc) from exc
        if not isinstance(record, dict):
            raise HarvestError(E_REDACT_PACKAGE_PARSE, "package record must be an object", context={"file": str(source), "line": line_number})
        record_type = record.get("record_type")
        if record_type == "header":
            if header is not None:
                raise HarvestError(E_REDACT_PACKAGE_MISMATCH, "package has multiple headers", context={"file": str(source), "line": line_number})
            header = record
        elif record_type == "frame":
            if header is None:
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "frame appears before header", context={"file": str(source), "line": line_number})
            seq = record.get("frame_seq")
            step_id = record.get("step_id")
            expected_screen = record.get("expected_screen")
            frame_text = record.get("text")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "frame_seq must be a non-negative integer", context={"file": str(source), "line": line_number})
            if not isinstance(step_id, str) or not isinstance(expected_screen, str):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "step_id and expected_screen must be strings", context={"file": str(source), "line": line_number})
            if not isinstance(frame_text, str):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "frame text must be a string", context={"file": str(source), "line": line_number})
            frame = ScreenFrame.parse(frame_text)
            recorded_hash = record.get("redacted_sha256")
            if not isinstance(recorded_hash, str) or recorded_hash != frame.sha256:
                raise HarvestError(E_REDACT_PACKAGE_MISMATCH, "redacted frame hash does not match package record", context={"file": str(source), "line": line_number, "frame_seq": seq})
            if len(frames) != seq:
                raise HarvestError(E_REDACT_PACKAGE_MISMATCH, "frame_seq values must be contiguous from zero", context={"file": str(source), "line": line_number})
            redactions = _parse_redactions(record.get("redactions", []), source=str(source), line=line_number)
            projection = record.get("projection_sha256")
            if projection is not None and not isinstance(projection, str):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "projection_sha256 must be a string or null", context={"file": str(source), "line": line_number})
            source_hash = record.get("source_frame_sha256")
            if not isinstance(source_hash, str):
                raise HarvestError(E_REDACT_PACKAGE_PARSE, "source_frame_sha256 must be a string", context={"file": str(source), "line": line_number})
            frames.append(
                RedactedPackageFrame(
                    frame_seq=seq,
                    step_id=step_id,
                    expected_screen=expected_screen,
                    contracted=bool(record.get("contracted")),
                    source_frame_sha256=source_hash,
                    redacted_sha256=recorded_hash,
                    projection_sha256=projection,
                    redactions=redactions,
                    frame=frame,
                )
            )
        elif record_type == "trailer":
            trailer = record
        else:
            raise HarvestError(E_REDACT_PACKAGE_PARSE, f"unknown package record_type {record_type!r}", context={"file": str(source), "line": line_number})
    if header is None:
        raise HarvestError(E_REDACT_PACKAGE_INCOMPLETE, "package has no header", context={"file": str(source)})
    if trailer is None:
        raise HarvestError(E_REDACT_PACKAGE_INCOMPLETE, "package has no trailer", context={"file": str(source)})
    if trailer.get("frame_count") != len(frames):
        raise HarvestError(
            E_REDACT_PACKAGE_INCOMPLETE,
            "package frame count does not match trailer",
            context={"file": str(source), "declared": trailer.get("frame_count"), "actual": len(frames)},
        )
    if header.get("format") != PACKAGE_FORMAT or header.get("format_version") != PACKAGE_FORMAT_VERSION:
        raise HarvestError(E_REDACT_PACKAGE_MISMATCH, "unsupported package format", context={"file": str(source), "format": header.get("format"), "format_version": header.get("format_version")})
    rules_version = header.get("rules_version")
    if rules_version != REDACTION_RULES_VERSION:
        raise HarvestError(
            E_REDACT_CONTRACT_MISMATCH,
            "unsupported redaction rules version",
            context={"file": str(source), "rules_version": rules_version, "supported": REDACTION_RULES_VERSION},
        )
    scope = header.get("scope")
    if scope not in SCOPES:
        raise HarvestError(E_REDACT_PACKAGE_PARSE, "package header has an unknown scope", context={"file": str(source), "scope": scope})
    session_id = header.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HarvestError(E_REDACT_PACKAGE_PARSE, "package header has no session_id", context={"file": str(source)})
    contract_sha256 = header.get("contract_sha256")
    if not isinstance(contract_sha256, str):
        raise HarvestError(E_REDACT_PACKAGE_PARSE, "package header has no contract_sha256", context={"file": str(source)})
    results_sha256 = trailer.get("results_sha256")
    if not isinstance(results_sha256, str):
        raise HarvestError(E_REDACT_PACKAGE_PARSE, "package trailer has no results_sha256", context={"file": str(source)})
    return RedactedPackage(
        session_id=session_id,
        captured_by=str(header.get("captured_by", "unknown")),
        scope=scope,
        rules_version=rules_version,
        contract_sha256=contract_sha256,
        frames=tuple(frames),
        results_sha256=results_sha256,
        source_file=str(source),
    )

def _verify_session_alignment(package: RedactedPackage, session: SessionContract) -> None:
    expected = [("start", session.start.screen_id)]
    expected.extend((step.step_id, step.expect.screen_id) for step in session.steps)
    if len(package.frames) != len(expected):
        raise HarvestError(
            E_REDACT_PACKAGE_MISMATCH,
            "package frame count does not match session steps",
            context={"expected": len(expected), "actual": len(package.frames)},
        )
    for index, (item, (step_id, screen_id)) in enumerate(zip(package.frames, expected)):
        if item.step_id != step_id or item.expected_screen != screen_id:
            raise HarvestError(
                E_REDACT_PACKAGE_MISMATCH,
                "package frame order does not match session steps",
                context={"index": index, "expected_step": step_id, "record_step": item.step_id, "expected_screen": screen_id, "record_screen": item.expected_screen},
            )

def verify_redacted_package(
    path: str | Path,
    *,
    screens: dict[str, ScreenContract],
    session: SessionContract,
    validation_config: ValidationConfig | None = None,
) -> dict[str, Any]:
    """Replay a redacted package offline and verify every recorded digest.

    The original whole-screen fingerprint is never compared against redacted
    frames; fingerprint contracts are verified through the recorded
    projection of the non-sensitive visible cells instead.
    """
    package = load_redacted_package(path)
    local_contract_digest = contract_digest(screens)
    if local_contract_digest != package.contract_sha256:
        raise HarvestError(
            E_REDACT_CONTRACT_MISMATCH,
            "screen contracts differ from the contracts bound to the package",
            context={"package_contract_sha256": package.contract_sha256, "local_contract_sha256": local_contract_digest},
        )
    if session.session_id != package.session_id:
        raise HarvestError(
            E_REDACT_PACKAGE_MISMATCH,
            "package session_id does not match session contract",
            context={"package_session_id": package.session_id, "session_id": session.session_id},
        )
    _verify_session_alignment(package, session)
    for item in package.frames:
        if item.projection_sha256 is not None:
            actual = projection_digest(item.frame.rows, item.redactions)
            if actual != item.projection_sha256:
                raise HarvestError(
                    E_REDACT_PACKAGE_MISMATCH,
                    "projection fingerprint of non-sensitive cells does not match package record",
                    context={"frame_seq": item.frame_seq, "expected_screen": item.expected_screen},
                )
    replay_frames = [CapturedFrame(frame_seq=item.frame_seq, step_id=item.step_id, expected_screen=item.expected_screen, frame=item.frame) for item in package.frames]
    replay_screens = {screen_id: replace(contract, fingerprint=None) for screen_id, contract in screens.items()}
    extractions, validation = _run_extraction(replay_frames, replay_screens, session, validation_config)
    digest = results_digest(extractions, validation)
    if digest != package.results_sha256:
        raise HarvestError(
            E_REDACT_PACKAGE_MISMATCH,
            "replayed non-sensitive results do not match the results digest recorded at export",
            context={"package_results_sha256": package.results_sha256, "replayed_results_sha256": digest},
        )
    sensitive_names = {item.name for contract in screens.values() for item in (*contract.fields, *contract.summary_fields) if item.sensitive}
    leaked: list[str] = []
    for extraction in extractions:
        for record in extraction.records:
            leaked.extend(name for name in sensitive_names if name in record["raw_values"] or name in record["normalized_values"])
        leaked.extend(name for name in sensitive_names if name in extraction.facts)
    if leaked:
        raise HarvestError(
            E_REDACT_PACKAGE_MISMATCH,
            "sensitive fields present in replayed results",
            context={"fields": sorted(set(leaked))},
        )
    return {
        "status": "verified",
        "package": package.source_file,
        "scope": package.scope,
        "rules_version": package.rules_version,
        "frames": len(package.frames),
        "records": sum(len(extraction.records) for extraction in extractions),
        "screens": sorted({extraction.screen_id for extraction in extractions}),
        "sensitive_skipped": sorted({name for extraction in extractions for name in extraction.sensitive_skipped}),
        "results_sha256": digest,
        "validation_ok": validation.ok,
    }
