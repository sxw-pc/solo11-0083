"""Fixed-size screen frames with bounded display-semantics validation.

Two optional screen input contracts select how raw frame text is turned into
the authoritative 24 x 80 visible grid:

``strict_snapshot``
    Only visible characters plus the ``\\n`` row separator are accepted. Any
    ESC sequence or other unauthorized control character is rejected, so the
    raw text is itself the visible snapshot.

``bounded_draw``
    A finite, explicitly enumerated set of CSI cursor positioning and erase
    sequences is replayed cell by cell onto a blank 24 x 80 grid. Extraction
    always runs against the *final visible grid*, never against the raw
    operation stream. Unknown sequences, truncation, out-of-bounds movement,
    budget overruns and control characters disguised as field values are all
    rejected rather than silently dropped.

Every frame keeps two independent digests: ``sha256`` over the original bytes
and ``visible_sha256`` over the final visible grid, together with an evidence
trace mapping each raw operation to the grid cells it touched. The two digests
are deliberately never merged into one hash.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import E_FRAME_MALFORMED, HarvestError

FRAME_ROWS = 24
FRAME_COLS = 80
MAX_FRAME_BYTES = 64 * 1024
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

#: Screen input contract identifiers.
INPUT_MODES = ("strict_snapshot", "bounded_draw")
DEFAULT_INPUT_MODE = "strict_snapshot"

# --- bounded_draw safety budgets (per frame, independent initial grid) -------
MAX_DRAW_OPS = 2048
MAX_SEQUENCE_BYTES = 16
MAX_PARAM_VALUE = 999
MAX_PARAM_DIGITS = 3
MAX_CSI_SEQUENCES = 512
MAX_WRITTEN_CELLS = FRAME_ROWS * FRAME_COLS * 4

#: CSI final bytes that bounded_draw is allowed to execute.
_CSI_CURSOR_OPS = frozenset({"H", "f", "A", "B", "C", "D"})
_CSI_ERASE_OPS = frozenset({"J", "K"})
_ALLOWED_CSI_FINAL = _CSI_CURSOR_OPS | _CSI_ERASE_OPS


@dataclass(frozen=True)
class DrawOp:
    """One raw drawing operation and the grid cells it affected.

    The trace never stores cell contents, so it can be surfaced as evidence
    without leaking sensitive field values.
    """

    index: int
    op: str
    line: int
    col: int
    length: int
    cells: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class GridDrawTrace:
    """Evidence produced while replaying one frame onto the visible grid."""

    mode: str
    ops: tuple[DrawOp, ...]
    cell_origins: tuple[tuple[tuple[int, int], tuple[int, int]], ...]
    operation_count: int
    csi_sequence_count: int
    written_cells: int

    def origin_of(self, row: int, col: int) -> tuple[int, int] | None:
        """Return the raw (line, col) origin of the last write to a cell."""
        for cell, origin in reversed(self.cell_origins):
            if cell == (row, col):
                return origin
        return None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "operation_count": self.operation_count,
            "csi_sequence_count": self.csi_sequence_count,
            "written_cells": self.written_cells,
            "ops": [
                {"index": op.index, "op": op.op, "line": op.line, "col": op.col, "length": op.length, "cells": [list(cell) for cell in op.cells]}
                for op in self.ops
            ],
            "cell_origins": [[list(cell), list(origin)] for cell, origin in self.cell_origins],
        }


def _malformed(message: str, **context: object) -> HarvestError:
    return HarvestError(E_FRAME_MALFORMED, message, context=context)


def _raw_position(text: str, offset: int) -> tuple[int, int]:
    """Convert a raw character offset into a raw (line, column) pair."""
    line = text.count("\n", 0, offset)
    last_nl = text.rfind("\n", 0, offset)
    return line, 0 if last_nl < 0 else offset - last_nl - 1


def _is_unauthorized_control(ch: str) -> bool:
    if ch == "\n" or ch == " ":
        return False
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def _strict_snapshot(text: str, expected_rows: int, expected_cols: int) -> "ScreenFrame":
    rows = text.split("\n")
    if len(rows) != expected_rows:
        raise _malformed(
            f"frame has {len(rows)} rows, expected {expected_rows}",
            reason="row_count",
            rows=len(rows),
            expected_rows=expected_rows,
        )
    for index, row in enumerate(rows):
        bad = next((ch for ch in row if _is_unauthorized_control(ch)), None)
        if bad is not None:
            raise _malformed(
                "strict_snapshot frame contains an unauthorized control character",
                reason="unauthorized_control",
                row=index,
                control=ord(bad),
                mode=DEFAULT_INPUT_MODE,
            )
        if len(row) != expected_cols:
            raise _malformed(
                f"row {index} has {len(row)} columns, expected {expected_cols}",
                reason="column_count",
                row=index,
                columns=len(row),
                expected_cols=expected_cols,
            )
    visible_text = "\n".join(rows)
    trace = GridDrawTrace(mode=DEFAULT_INPUT_MODE, ops=(), cell_origins=(), operation_count=0, csi_sequence_count=0, written_cells=0)
    return ScreenFrame(
        text=text,
        rows=tuple(rows),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        visible_sha256=hashlib.sha256(visible_text.encode("utf-8")).hexdigest(),
        input_mode=DEFAULT_INPUT_MODE,
        draw_trace=trace,
    )


def _parse_csi_parameter(raw: str, *, default: int, line: int, col: int) -> int:
    if raw == "":
        return default
    if not raw.isdigit() or len(raw) > MAX_PARAM_DIGITS:
        raise _malformed(
            "bounded_draw CSI parameter is out of its allowed numeric range",
            reason="csi_parameter",
            line=line,
            col=col,
            parameter=raw,
            max_digits=MAX_PARAM_DIGITS,
        )
    value = int(raw)
    if value > MAX_PARAM_VALUE:
        raise _malformed(
            "bounded_draw CSI parameter is out of its allowed numeric range",
            reason="csi_parameter",
            line=line,
            col=col,
            parameter=value,
            max_value=MAX_PARAM_VALUE,
        )
    return value


def _bounded_replay(text: str, expected_rows: int, expected_cols: int) -> "ScreenFrame":
    grid: list[list[str]] = [[" " for _ in range(expected_cols)] for _ in range(expected_rows)]
    row = 0
    col = 0
    ops: list[DrawOp] = []
    cell_origins: list[tuple[tuple[int, int], tuple[int, int]]] = []
    csi_sequences = 0
    written_cells = 0

    def add_op(name: str, start_offset: int, length: int, cells: list[tuple[int, int]]) -> None:
        line, raw_col = _raw_position(text, start_offset)
        ops.append(DrawOp(index=len(ops), op=name, line=line, col=raw_col, length=length, cells=tuple(cells)))

    def write_cell(r: int, c: int, ch: str, offset: int) -> None:
        nonlocal written_cells
        if not (0 <= r < expected_rows and 0 <= c < expected_cols):
            line, raw_col = _raw_position(text, offset)
            raise _malformed(
                "bounded_draw write escapes the 24 x 80 grid boundary",
                reason="write_out_of_bounds",
                line=line,
                col=raw_col,
                target_row=r,
                target_col=c,
            )
        if written_cells >= MAX_WRITTEN_CELLS:
            line, raw_col = _raw_position(text, offset)
            raise _malformed(
                "bounded_draw frame exceeds its per-frame written-cell budget",
                reason="write_budget",
                line=line,
                col=raw_col,
                max_written_cells=MAX_WRITTEN_CELLS,
            )
        grid[r][c] = ch
        cell_origins.append(((r, c), _raw_position(text, offset)))
        written_cells += 1

    offset = 0
    total = len(text)
    while offset < total:
        if len(ops) >= MAX_DRAW_OPS:
            line, raw_col = _raw_position(text, offset)
            raise _malformed(
                "bounded_draw frame exceeds its per-frame operation budget",
                reason="op_budget",
                line=line,
                col=raw_col,
                max_ops=MAX_DRAW_OPS,
            )
        ch = text[offset]
        if ch == "\x1b":
            line, raw_col = _raw_position(text, offset)
            # Hand-rolled bounded CSI scan: ESC [, short decimal parameter
            # groups, at most one intermediate byte, one final byte. Scanning
            # manually lets us distinguish truncation from unknown sequences.
            if offset + 1 >= total or text[offset + 1] != "[":
                raise _malformed(
                    "bounded_draw frame contains a truncated or unauthorized escape sequence",
                    reason="csi_truncated",
                    line=line,
                    col=raw_col,
                )
            scan = offset + 2
            while scan < total and text[scan] in "0123456789;":
                scan += 1
            if scan < total and 0x3C <= ord(text[scan]) <= 0x3F:
                # Private marker (e.g. '?') or parameter separator: not in the
                # bounded grammar, so the whole sequence is unknown.
                raise _malformed(
                    "bounded_draw frame contains an unknown CSI sequence",
                    reason="csi_unknown",
                    line=line,
                    col=raw_col,
                    marker=text[scan],
                )
            intermediate = ""
            if scan < total and 0x20 <= ord(text[scan]) <= 0x2F:
                intermediate = text[scan]
                scan += 1  # a single optional intermediate byte
            if scan >= total:
                raise _malformed(
                    "bounded_draw frame contains a truncated CSI sequence",
                    reason="csi_truncated",
                    line=line,
                    col=raw_col,
                )
            final = text[scan]
            seq_len = scan + 1 - offset
            if ord(final) < 0x40 or ord(final) > 0x7E:
                raise _malformed(
                    "bounded_draw frame contains a malformed CSI sequence",
                    reason="csi_truncated",
                    line=line,
                    col=raw_col,
                )
            params_raw = text[offset + 2 : scan - (1 if intermediate else 0)]
            if intermediate:
                raise _malformed(
                    "bounded_draw frame contains an unknown CSI sequence",
                    reason="csi_unknown",
                    line=line,
                    col=raw_col,
                    intermediate=intermediate,
                    final=final,
                )
            if seq_len > MAX_SEQUENCE_BYTES:
                raise _malformed(
                    "bounded_draw CSI sequence exceeds its byte limit",
                    reason="csi_too_long",
                    line=line,
                    col=raw_col,
                    sequence_bytes=seq_len,
                    max_sequence_bytes=MAX_SEQUENCE_BYTES,
                )
            if final not in _ALLOWED_CSI_FINAL:
                raise _malformed(
                    "bounded_draw frame contains an unknown CSI sequence",
                    reason="csi_unknown",
                    line=line,
                    col=raw_col,
                    final=final,
                )
            groups = params_raw.split(";") if params_raw else [""]
            if len(groups) > 2 or any(group == "" for group in groups if params_raw):
                raise _malformed(
                    "bounded_draw CSI parameters are malformed",
                    reason="csi_parameter",
                    line=line,
                    col=raw_col,
                    parameters=params_raw,
                )
            for group in groups:
                if group != "" and (not group.isdigit() or len(group) > MAX_PARAM_DIGITS or (len(group) > 1 and group[0] == "0")):
                    raise _malformed(
                        "bounded_draw CSI parameter is out of its allowed numeric range",
                        reason="csi_parameter",
                        line=line,
                        col=raw_col,
                        parameter=group,
                        max_digits=MAX_PARAM_DIGITS,
                    )
            if csi_sequences >= MAX_CSI_SEQUENCES:
                raise _malformed(
                    "bounded_draw frame exceeds its per-frame CSI sequence budget",
                    reason="csi_budget",
                    line=line,
                    col=raw_col,
                    max_csi_sequences=MAX_CSI_SEQUENCES,
                )
            params = [_parse_csi_parameter(group, default=1, line=line, col=raw_col) for group in groups]
            cells: list[tuple[int, int]] = []
            if final in ("H", "f"):  # CUP: cursor position, 1-based
                target_row = params[0] - 1
                target_col = (params[1] if len(params) > 1 else 1) - 1
                if not (0 <= target_row < expected_rows and 0 <= target_col < expected_cols):
                    raise _malformed(
                        "bounded_draw cursor positioning escapes the grid boundary",
                        reason="cursor_out_of_bounds",
                        line=line,
                        col=raw_col,
                        target_row=target_row,
                        target_col=target_col,
                    )
                row, col = target_row, target_col
                add_op(f"CSI{final}", offset, seq_len, [])
            elif final in ("A", "B", "C", "D"):  # relative cursor movement
                delta = params[0]
                if final == "A":
                    row -= delta
                elif final == "B":
                    row += delta
                elif final == "C":
                    col += delta
                else:
                    col -= delta
                if not (0 <= row < expected_rows and 0 <= col < expected_cols):
                    raise _malformed(
                        "bounded_draw cursor movement escapes the grid boundary",
                        reason="cursor_out_of_bounds",
                        line=line,
                        col=raw_col,
                        target_row=row,
                        target_col=col,
                    )
                add_op(f"CSI{final}", offset, seq_len, [])
            else:  # erase: ED (J) or EL (K), only parameters 0..2 are legal
                selector = params[0]
                if selector not in (0, 1, 2):
                    raise _malformed(
                        "bounded_draw erase parameter is out of range",
                        reason="csi_parameter",
                        line=line,
                        col=raw_col,
                        parameter=selector,
                    )
                if final == "K":  # EL: erase in line
                    if selector == 0:
                        span = range(col, expected_cols)
                    elif selector == 1:
                        span = range(0, col + 1)
                    else:
                        span = range(0, expected_cols)
                    for erase_col in span:
                        write_cell(row, erase_col, " ", offset)
                        cells.append((row, erase_col))
                else:  # ED: erase in display
                    if selector == 0:
                        for erase_col in range(col, expected_cols):
                            write_cell(row, erase_col, " ", offset)
                            cells.append((row, erase_col))
                        for erase_row in range(row + 1, expected_rows):
                            for erase_col in range(expected_cols):
                                write_cell(erase_row, erase_col, " ", offset)
                                cells.append((erase_row, erase_col))
                    elif selector == 1:
                        for erase_row in range(0, row):
                            for erase_col in range(expected_cols):
                                write_cell(erase_row, erase_col, " ", offset)
                                cells.append((erase_row, erase_col))
                        for erase_col in range(0, col + 1):
                            write_cell(row, erase_col, " ", offset)
                            cells.append((row, erase_col))
                    else:
                        for erase_row in range(expected_rows):
                            for erase_col in range(expected_cols):
                                write_cell(erase_row, erase_col, " ", offset)
                                cells.append((erase_row, erase_col))
                add_op(f"CSI{final}{selector}", offset, seq_len, cells)
            csi_sequences += 1
            offset = scan + 1
            continue

        if ch == "\n":
            if row + 1 >= expected_rows:
                line, raw_col = _raw_position(text, offset)
                raise _malformed(
                    "bounded_draw newline escapes the grid boundary",
                    reason="cursor_out_of_bounds",
                    line=line,
                    col=raw_col,
                    target_row=row + 1,
                )
            add_op("LF", offset, 1, [])
            row += 1
            col = 0
            offset += 1
            continue

        if _is_unauthorized_control(ch):
            line, raw_col = _raw_position(text, offset)
            raise _malformed(
                "bounded_draw frame contains an unauthorized control character",
                reason="unauthorized_control",
                line=line,
                col=raw_col,
                control=ord(ch),
            )

        write_cell(row, col, ch, offset)
        add_op("PRINT", offset, 1, [(row, col)])
        col += 1
        if col > expected_cols:
            line, raw_col = _raw_position(text, offset)
            raise _malformed(
                "bounded_draw text write overflows the 80-column row",
                reason="write_out_of_bounds",
                line=line,
                col=raw_col,
                target_row=row,
                target_col=col - 1,
            )
        offset += 1

    if row != expected_rows - 1:
        raise _malformed(
            f"bounded_draw replay ended on row {row}, expected the final 24th row",
            reason="row_count",
            rows=row + 1,
            expected_rows=expected_rows,
        )
    visible_rows = tuple("".join(cells) for cells in grid)
    visible_text = "\n".join(visible_rows)
    trace = GridDrawTrace(
        mode="bounded_draw",
        ops=tuple(ops),
        cell_origins=tuple(cell_origins),
        operation_count=len(ops),
        csi_sequence_count=csi_sequences,
        written_cells=written_cells,
    )
    return ScreenFrame(
        text=text,
        rows=visible_rows,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        visible_sha256=hashlib.sha256(visible_text.encode("utf-8")).hexdigest(),
        input_mode="bounded_draw",
        draw_trace=trace,
    )


@dataclass(frozen=True)
class ScreenFrame:
    """A validated frame and its authoritative final visible 24 x 80 grid."""

    text: str
    rows: tuple[str, ...]
    sha256: str
    visible_sha256: str = ""
    input_mode: str = DEFAULT_INPUT_MODE
    draw_trace: GridDrawTrace | None = None

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        expected_rows: int = FRAME_ROWS,
        expected_cols: int = FRAME_COLS,
        input_mode: str = DEFAULT_INPUT_MODE,
    ) -> "ScreenFrame":
        if not isinstance(text, str):
            raise HarvestError(E_FRAME_MALFORMED, "frame must be text", context={"type": type(text).__name__})
        if "\r" in text:
            raise _malformed("frame must not contain carriage returns", reason="unauthorized_control", control=ord("\r"))
        if "\x00" in text:
            raise _malformed("frame must not contain NUL", reason="unauthorized_control", control=0)
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise _malformed(
                f"frame exceeds {MAX_FRAME_BYTES} bytes",
                reason="frame_too_large",
                bytes=len(text.encode("utf-8")),
                limit=MAX_FRAME_BYTES,
            )
        if input_mode == "strict_snapshot":
            frame = _strict_snapshot(text, expected_rows, expected_cols)
        elif input_mode == "bounded_draw":
            frame = _bounded_replay(text, expected_rows, expected_cols)
        else:
            raise _malformed("unknown screen input contract", reason="unknown_input_mode", input_mode=input_mode, allowed=list(INPUT_MODES))
        if frame.draw_trace is None:  # defensive: parsers always attach a trace
            raise _malformed("frame replay produced no evidence trace", reason="internal")
        return frame

    @classmethod
    def from_rows(cls, rows: list[str] | tuple[str, ...], *, input_mode: str = DEFAULT_INPUT_MODE) -> "ScreenFrame":
        return cls.parse("\n".join(rows), input_mode=input_mode)

    @property
    def visible_text(self) -> str:
        return "\n".join(self.rows)

    def contains_all(self, fragments: tuple[str, ...] | list[str]) -> bool:
        return all(fragment in self.visible_text for fragment in fragments)

    def line(self, row: int) -> str:
        if not 0 <= row < len(self.rows):
            raise HarvestError(E_FRAME_MALFORMED, f"row {row} is outside frame", context={"row": row, "rows": len(self.rows)})
        return self.rows[row]

    def fingerprint(self) -> str:
        # Fingerprints bind to the final visible grid, so strict snapshots and
        # equivalent bounded drawings of the same screen share a fingerprint.
        return f"sha256:{self.visible_sha256}"


def ensure_contract_match(frame: ScreenFrame, *, screen_id: str, fragments: tuple[str, ...]) -> None:
    visible = frame.visible_text
    missing = [fragment for fragment in fragments if fragment not in visible]
    if missing:
        from .errors import E_SCREEN_CONTRACT_MISMATCH

        raise HarvestError(
            E_SCREEN_CONTRACT_MISMATCH,
            "screen does not contain required match fragments on the visible grid",
            context={
                "screen_id": screen_id,
                "missing": missing,
                "frame_sha256": frame.sha256,
                "visible_sha256": frame.visible_sha256,
                "input_mode": frame.input_mode,
            },
        )


def ensure_fingerprint(frame: ScreenFrame, fingerprint: str | None) -> None:
    if fingerprint is None:
        return
    actual = frame.fingerprint()
    if actual != fingerprint:
        from .errors import E_SCREEN_FINGERPRINT_MISMATCH

        raise HarvestError(
            E_SCREEN_FINGERPRINT_MISMATCH,
            "screen fingerprint does not match the visible grid contract",
            context={"expected": fingerprint, "actual": actual, "input_mode": frame.input_mode},
        )


def to_halfwidth_digits(value: str) -> str:
    return value.translate(FULLWIDTH_DIGITS)


def normalize_value(raw: str | None, rules: tuple[str, ...] | list[str]) -> str | None:
    """Normalize while retaining an empty value as ``None``."""
    if raw is None:
        return None
    value = raw
    for rule in rules:
        if rule == "trim":
            value = value.strip()
        elif rule == "collapse_space":
            value = re.sub(r"[ \t]+", " ", value)
        elif rule == "fullwidth_digits":
            value = to_halfwidth_digits(value)
        elif rule == "upper":
            value = value.upper()
        else:
            raise ValueError(f"unknown normalize rule: {rule}")
    if value.strip() == "":
        return None
    return value


def find_label_row(frame: ScreenFrame, label: str, first: int, last: int) -> int | None:
    for row_index in range(max(0, first), min(last, len(frame.rows) - 1) + 1):
        if label in frame.rows[row_index]:
            return row_index
    return None
