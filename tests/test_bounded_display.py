"""Bounded display-semantics validation for synthetic local terminal frames.

These tests pin the two optional screen input contracts:

* ``strict_snapshot`` accepts only a visible 24 x 80 character snapshot and
  rejects ESC sequences / unauthorized controls outright.
* ``bounded_draw`` replays an explicitly enumerated set of CSI cursor and
  erase operations cell by cell, so extraction, match fragments, fingerprints
  and list-region slices all observe the final visible grid rather than the
  raw operation stream.

Failure contexts carry locatable coordinates and reason codes, never field
values.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from termharvest.cassette import load_cassette, write_cassette
from termharvest.config import load_screens_dir
from termharvest.errors import HarvestError
from termharvest.extract import extract_screen
from termharvest.screen import (
    DEFAULT_INPUT_MODE,
    MAX_CSI_SEQUENCES,
    MAX_DRAW_OPS,
    MAX_SEQUENCE_BYTES,
    MAX_WRITTEN_CELLS,
    ScreenFrame,
)
from termharvest.session import TerminalSession
from termharvest.validate import validate_records
from termharvest.validate_config import load_validation_config

ESC = "\x1b"
ROWS = 24
COLS = 80
VALUE_COL = 23
REPO_ROOT = Path(__file__).resolve().parents[1]


def blank_rows() -> list[str]:
    return [" " * COLS for _ in range(ROWS)]


def bounded_text(row_payloads: dict[int, str]) -> str:
    """Build frame text; payload rows may be shorter than 80 columns."""
    rows = blank_rows()
    for index, payload in row_payloads.items():
        rows[index] = payload
    return "\n".join(rows)


def snapshot_text(row_payloads: dict[int, str]) -> str:
    """Build a strict-snapshot frame where every row is exactly 80 columns."""
    rows = blank_rows()
    for index, payload in row_payloads.items():
        rows[index] = payload.ljust(COLS)[:COLS]
    return "\n".join(rows)


def customer_rows(*, balance_cell: str, credit_limit: str, attack_row: str | None = None) -> dict[int, str]:
    """Visible layout of the demo customer contract with full labels."""
    balance_row = attack_row if attack_row is not None else f" BALANCE             : {balance_cell}".ljust(COLS)
    return {
        0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
        3: " CUSTOMER ID         : C00042",
        4: " CUSTOMER NAME       : NORTHWIND SYNTHETIC CO",
        5: " CITY                : HARBORVIEW",
        6: " STATUS              : ACTIVE",
        7: balance_row,
        8: f" CREDIT LIMIT        : {credit_limit}",
        9: " PERIOD START        : 2026-01-01",
        10: " PERIOD END          : 2026-01-31",
        11: " ADDRESS LINE 1      : 100 SAMPLE WAY",
        12: " ADDRESS LINE 2      : SUITE 200",
        13: " NOTES:",
        14: "  SYNTHETIC CUSTOMER ONLY",
        15: " REGION CODE         : 07",
        22: " OPERATOR TOKEN      : PLACEHOLDER",
    }


def customer_contract():
    return load_screens_dir(REPO_ROOT / "contracts/screens")["demo_customer"]


def bill_contract():
    return load_screens_dir(REPO_ROOT / "contracts/screens")["demo_bill_list"]


def validation_rules():
    return load_validation_config(REPO_ROOT / "contracts/validators/demo_rules.yaml")


def attacked_balance_row(visible_balance: str) -> str:
    """80-column raw row: stale 100, padding, CUP back, then the visible value."""
    label = " BALANCE             : "
    cup = ESC + f"[8;{VALUE_COL + 1}H"
    attack = f"{label}100"
    padding = COLS - len(attack) - len(cup) - len(visible_balance)
    assert padding >= 0
    row = attack + " " * padding + cup + visible_balance
    assert len(row) == COLS  # the old length-only check would accept this
    return row


# --------------------------------------------------------------------------- #
# 1. Minimal counterexample: overwritten balance, visible grid is authoritative
# --------------------------------------------------------------------------- #


def test_overwritten_balance_extracts_visible_value_under_bounded_draw():
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    # The raw stream still contains the stale value and the ESC sequence...
    raw_row = frame.text.split("\n")[7]
    assert "100" in raw_row and ESC in raw_row
    # ...but neither appears on the final visible grid.
    assert ESC not in frame.visible_text
    assert "100" not in frame.rows[7]
    assert frame.rows[7][VALUE_COL:].strip() == "900"

    result = extract_screen(frame, customer_contract(), 1)
    record = result.records[0]
    assert record["normalized_values"]["balance"] == "900"
    assert record["input_mode"] == "bounded_draw"
    # A regex that merely strips ESC would concatenate old and new values; the
    # grid replay must not, so the extracted field contains no stale digits.
    assert record["raw_values"]["balance"] == "900"


def test_strict_snapshot_rejects_the_same_attack_frame():
    row7 = attacked_balance_row("900")
    text = snapshot_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7))
    # snapshot_text would truncate/pad; restore the exact 80-col attack row.
    rows = text.split("\n")
    rows[7] = row7
    assert all(len(r) == COLS for r in rows)
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="strict_snapshot")
    error = caught.value
    assert error.code == "E_FRAME_MALFORMED"
    assert error.context["reason"] == "unauthorized_control"
    assert error.context["row"] == 7
    assert error.context["control"] == 0x1B
    # Strict rejection means the hidden stale value can never authorize a pass.
    assert "100" not in error.message


def test_visible_balance_over_credit_limit_fails_record_rule():
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    result = extract_screen(frame, customer_contract(), 1)
    report = validate_records(result.records, validation_rules())
    rule_errors = [item for item in report.errors if item.get("rule_id") == "balance_within_credit_limit"]
    assert len(rule_errors) == 1
    assert rule_errors[0]["code"] == "E_RECORD_CONSISTENCY"
    # The stale 100 (which would pass a 500 limit) is not used.
    assert result.records[0]["normalized_values"]["balance"] == "900"


# --------------------------------------------------------------------------- #
# 2. Dual digests and the raw-operation -> grid evidence mapping
# --------------------------------------------------------------------------- #


def test_raw_and_visible_digests_are_distinct_and_separately_preserved():
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    assert len(frame.sha256) == 64 and len(frame.visible_sha256) == 64
    assert frame.sha256 != frame.visible_sha256
    # The fingerprint binds to the visible grid, never to the raw stream.
    assert frame.fingerprint() == "sha256:" + frame.visible_sha256
    # Each overwritten visible cell points at the later raw operation.
    origin = frame.draw_trace.origin_of(7, VALUE_COL)
    assert origin is not None
    origin_line, origin_col = origin
    assert origin_line == 7
    assert frame.text.split("\n")[7][origin_col : origin_col + 3] == "900"
    # Evidence trace carries coordinates and operation metadata, no contents:
    # every op entry exposes only names, counts and integer cell coordinates.
    trace_dict = frame.draw_trace.to_dict()
    for op in trace_dict["ops"]:
        assert set(op) == {"index", "op", "line", "col", "length", "cells"}
        assert all(isinstance(cell, list) and len(cell) == 2 for cell in op["cells"])
    for cell, origin in trace_dict["cell_origins"]:
        assert isinstance(cell, list) and isinstance(origin, list)
    assert trace_dict["csi_sequence_count"] == 1
    cup_ops = [op for op in trace_dict["ops"] if op["op"] == "CSIH"]
    assert cup_ops and cup_ops[0]["line"] == 7


def test_control_free_frame_has_identical_digests_and_extraction_in_both_modes():
    payload = customer_rows(balance_cell="42.00", credit_limit="5000.00")
    strict = ScreenFrame.parse(snapshot_text(payload), input_mode="strict_snapshot")
    bounded = ScreenFrame.parse(snapshot_text(payload), input_mode="bounded_draw")
    assert strict.visible_text == bounded.visible_text
    assert strict.sha256 == bounded.sha256
    assert strict.visible_sha256 == bounded.visible_sha256
    assert strict.sha256 == strict.visible_sha256
    # Both modes extract the same business values and raw cell contents.
    contract = customer_contract()
    strict_records = extract_screen(strict, contract, 1).records
    bounded_records = extract_screen(bounded, contract, 1).records
    assert strict_records[0]["raw_values"] == bounded_records[0]["raw_values"]
    assert strict_records[0]["normalized_values"] == bounded_records[0]["normalized_values"]
    assert strict_records[0]["record_key"] == bounded_records[0]["record_key"]


# --------------------------------------------------------------------------- #
# 3. Erase-line rewrite, label overwrite and list-region column boundaries
# --------------------------------------------------------------------------- #


def test_erase_line_then_rewrite_leaves_only_new_value():
    label = " BALANCE             : "
    row7 = label + "1000" + " " * 40 + ESC + f"[8;{VALUE_COL + 1}H" + "900" + ESC + "[0K"
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    assert frame.rows[7][VALUE_COL:].strip() == "900"
    erase_ops = [op for op in frame.draw_trace.ops if op.op == "CSIK0"]
    assert len(erase_ops) == 1
    # EL(0) erased from the cursor to the end of the same row only.
    assert all(cell[0] == 7 for cell in erase_ops[0].cells)


def test_overwritten_label_hides_match_fragment_on_visible_grid():
    # The contract fragment starts visible, then is overwritten with spaces.
    original = "CUSTOMER PROFILE"
    row0 = original + " " * (COLS - len(original)) + ESC + "[1;1H" + (" " * COLS)
    frame = ScreenFrame.parse(bounded_text({0: row0}), input_mode="bounded_draw")
    assert "CUSTOMER PROFILE" not in frame.visible_text
    assert "CUSTOMER PROFILE" in frame.text  # still present in the raw stream
    with pytest.raises(HarvestError) as caught:
        extract_screen(frame, customer_contract(), 1)
    assert caught.value.code == "E_SCREEN_CONTRACT_MISMATCH"
    assert "CUSTOMER PROFILE" in caught.value.context["missing"]
    assert caught.value.context["input_mode"] == "bounded_draw"


def test_list_region_slices_visible_columns_after_overwrite():
    # Row 5 is the first list row; overwrite the third character of the id so
    # the visible id changes while the raw row keeps the original spelling.
    line = " B000999    2026-02-01    999.00      OPEN"
    attack = line + " " * (COLS - len(line) - 6) + ESC + "[6;3H" + "1"
    payload = {
        0: " BILL LIST",
        1: " BILL ID",
        5: attack,
        20: " BATCH COUNT         : 1",
        21: " BATCH TOTAL         : 999.00",
    }
    frame = ScreenFrame.parse(bounded_text(payload), input_mode="bounded_draw")
    result = extract_screen(frame, bill_contract(), 2)
    bill_ids = [record["normalized_values"]["bill_id"] for record in result.records]
    assert bill_ids == ["B100999"]
    assert "B000999" not in bill_ids  # raw-only old id never reaches the record
    assert all(ESC not in json.dumps(record) for record in result.records)


# --------------------------------------------------------------------------- #
# 4. Rejection paths: unknown/truncated CSI, controls, bounds, budgets
# --------------------------------------------------------------------------- #


def _bounded_frame(row_payload: str, row_index: int = 7) -> str:
    return bounded_text({row_index: row_payload})


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (ESC + "[?25h" + " " * 73, "csi_unknown"),
        (ESC + "[2m" + " " * 76, "csi_unknown"),
        (ESC + "[1;2 H" + " " * 72, "csi_unknown"),
        (ESC + "[8;24", "csi_truncated"),
        (ESC + "[", "csi_truncated"),
        (ESC, "csi_truncated"),
        (ESC + "[1000;1H" + " " * 69, "csi_parameter"),
        (ESC + "[01;02H" + " " * 71, "csi_parameter"),
        (ESC + "[1;;2H" + " " * 71, "csi_parameter"),
        ("ABC\tDEF" + " " * 72, "unauthorized_control"),
        ("ABC\x07DEF" + " " * 71, "unauthorized_control"),
    ],
)
def test_bounded_draw_rejects_illegal_sequences_with_locatable_reason(payload, reason):
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse(_bounded_frame(payload), input_mode="bounded_draw")
    error = caught.value
    assert error.code == "E_FRAME_MALFORMED"
    assert error.context["reason"] == reason
    assert {"line", "col"} <= set(error.context)
    # Failure context never embeds field contents.
    assert "BALANCE" not in json.dumps(error.context)


@pytest.mark.parametrize(
    ("payload", "row_index"),
    [
        (ESC + "[25;1H" + "x" * 73, 7),    # CUP row out of bounds
        (ESC + "[0;1H" + " " * 72, 7),     # CUP row zero
        (ESC + "[1;81H" + " " * 72, 0),    # CUP column out of bounds
        (ESC + "[5A", 0),                  # cursor up past top
        (ESC + "[99B" + " " * 73, 7),      # cursor down past bottom
        (ESC + "[99C" + " " * 73, 7),      # cursor right past last column
    ],
)
def test_bounded_draw_rejects_out_of_bounds_cursor(payload, row_index):
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse(_bounded_frame(payload, row_index), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "cursor_out_of_bounds"
    assert "target_row" in caught.value.context or "target_col" in caught.value.context


def test_control_character_cannot_impersonate_a_field_value():
    # A BEL byte parked where a value would be is rejected, not stripped.
    payload = " BALANCE             : 9\x0700" + " " * 48
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse(_bounded_frame(payload), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "unauthorized_control"
    assert caught.value.context["control"] == 7


def test_strict_snapshot_rejects_any_escape_even_if_dims_pass():
    text = snapshot_text(customer_rows(balance_cell="900", credit_limit="500"))
    rows = text.split("\n")
    rows[7] = rows[7][:10] + ESC + rows[7][11:]
    assert len(rows[7]) == COLS
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="strict_snapshot")
    assert caught.value.context["reason"] == "unauthorized_control"
    assert caught.value.context["row"] == 7


def test_operation_budget_is_enforced_per_frame():
    # Mix printable ops with zero-cell cursor moves: each move costs one
    # operation but writes nothing, so the operation budget is reached while
    # the CSI-sequence and written-cell budgets stay within their limits.
    move = ESC + "[0A"
    rows = blank_rows()
    printable = 23 * 79  # 1817 prints on the first 23 rows
    rows[: ROWS - 1] = ["p" * 79 for _ in range(ROWS - 1)]
    extra = MAX_DRAW_OPS - printable + 5
    rows[ROWS - 1] = move * extra
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "op_budget"
    assert caught.value.context["max_ops"] == MAX_DRAW_OPS


def test_csi_sequence_budget_is_enforced_per_frame():
    # Keep the whole sequence on row 0 (no newlines inside it) so the CSI
    # limit is reached before any row/column boundary check.
    move = ESC + "[0A"
    rows = blank_rows()
    rows[0] = move * (MAX_CSI_SEQUENCES + 1)
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "csi_budget"
    assert caught.value.context["max_csi_sequences"] == MAX_CSI_SEQUENCES


def test_written_cell_budget_is_enforced_per_frame():
    # Repeated full-screen erases each rewrite all 1920 cells; the write budget
    # (4 x 1920) is hit well before the operation budget.
    rows = blank_rows()
    rows[0] = (ESC + "[2J") * 6
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "write_budget"
    assert caught.value.context["max_written_cells"] == MAX_WRITTEN_CELLS


def test_each_frame_replays_from_an_independent_blank_grid():
    # Two frames parsed sequentially must not share cursor/grid state.
    first = ScreenFrame.parse(snapshot_text(customer_rows(balance_cell="11.00", credit_limit="5000.00")), input_mode="bounded_draw")
    second = ScreenFrame.parse(snapshot_text(customer_rows(balance_cell="22.00", credit_limit="5000.00")), input_mode="bounded_draw")
    assert first.rows[7] != second.rows[7]
    assert second.rows[7][VALUE_COL:].strip() == "22.00"
    # Independent replay: identical blank start gives identical op counts.
    assert first.draw_trace.operation_count == second.draw_trace.operation_count


def test_unknown_input_contract_is_rejected():
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse(snapshot_text({}), input_mode="teletype_1978")
    assert caught.value.context["reason"] == "unknown_input_mode"


def test_csi_sequence_byte_limit_is_enforced():
    long_sequence = ESC + "[" + "1" * (MAX_SEQUENCE_BYTES + 4) + "H"
    rows = blank_rows()
    rows[0] = long_sequence + " " * (COLS - len(long_sequence))
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "csi_too_long"
    assert caught.value.context["sequence_bytes"] > MAX_SEQUENCE_BYTES


def test_row_count_mismatch_is_rejected_in_both_modes():
    short = "\n".join([" " * COLS for _ in range(ROWS - 1)])
    for mode in ("strict_snapshot", "bounded_draw"):
        with pytest.raises(HarvestError) as caught:
            ScreenFrame.parse(short, input_mode=mode)
        assert caught.value.context["reason"] == "row_count"
        assert caught.value.context["rows"] == ROWS - 1


def test_erase_selector_outside_0_1_2_is_rejected():
    rows = blank_rows()
    rows[0] = ESC + "[3K" + " " * 76
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("\n".join(rows), input_mode="bounded_draw")
    assert caught.value.context["reason"] == "csi_parameter"


def test_legacy_recording_and_replay_behavior_is_unchanged(demo_paths, tmp_path):
    # A control-free fixture cassette loads, re-serializes and re-loads with
    # identical text and identical digests.
    original = load_cassette(demo_paths["replay_cassette"])
    rewrite = tmp_path / "rewrite.jsonl"
    write_cassette(
        rewrite,
        session_id=original.session_id,
        frames=[(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in original.frames],
        captured_by=original.captured_by,
    )
    reloaded = load_cassette(rewrite)
    assert [item.frame.text for item in reloaded.frames] == [item.frame.text for item in original.frames]
    assert all(item.frame.sha256 == item.frame.visible_sha256 for item in reloaded.frames)
    assert all(item.frame.input_mode == DEFAULT_INPUT_MODE for item in reloaded.frames)


# --------------------------------------------------------------------------- #
# 5. Cassette persistence, replay tampering and legacy compatibility
# --------------------------------------------------------------------------- #


def test_cassette_persists_dual_digests_mode_and_trace(tmp_path):
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    path = tmp_path / "drawn.jsonl"
    write_cassette(path, session_id="synthetic", frames=[(0, "start", "demo_customer", frame)], captured_by="test")
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(raw_lines[1])
    assert record["frame_sha256"] == frame.sha256
    assert record["visible_sha256"] == frame.visible_sha256
    assert record["input_mode"] == "bounded_draw"
    assert record["draw_trace"]["operation_count"] == frame.draw_trace.operation_count
    # The evidence trace carries only op names, counts and integer coordinates.
    for op in record["draw_trace"]["ops"]:
        assert set(op) == {"index", "op", "line", "col", "length", "cells"}
    for cell, origin in record["draw_trace"]["cell_origins"]:
        assert all(isinstance(value, int) for value in cell + origin)

    replayed = load_cassette(path)
    replayed_frame = replayed.frames[0].frame
    assert replayed_frame.input_mode == "bounded_draw"
    assert replayed_frame.visible_sha256 == frame.visible_sha256
    assert replayed_frame.rows[7][VALUE_COL:].strip() == "900"


def test_cassette_detects_replay_tampering_of_raw_and_visible_hashes(tmp_path):
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    path = tmp_path / "drawn.jsonl"
    write_cassette(path, session_id="synthetic", frames=[(0, "start", "demo_customer", frame)], captured_by="test")

    # Tamper with the raw text while keeping its declared hash: must fail on
    # the recomputed raw digest rather than silently accepting the rewrite.
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    tampered_raw = dict(record)
    tampered_raw["text"] = tampered_raw["text"].replace("900", "800", 1)
    lines[1] = json.dumps(tampered_raw, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        load_cassette(path)
    assert caught.value.code == "E_CASSETTE_MISMATCH"
    assert "raw frame hash" in caught.value.message

    # Tamper with the declared visible digest instead.
    record["visible_sha256"] = "0" * 64
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        load_cassette(path)
    assert caught.value.code == "E_CASSETTE_MISMATCH"
    assert "visible-grid hash" in caught.value.message


def test_cassette_detects_trace_tampering(tmp_path):
    row7 = attacked_balance_row("900")
    frame = ScreenFrame.parse(bounded_text(customer_rows(balance_cell="900", credit_limit="500", attack_row=row7)), input_mode="bounded_draw")
    path = tmp_path / "drawn.jsonl"
    write_cassette(path, session_id="synthetic", frames=[(0, "start", "demo_customer", frame)], captured_by="test")
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["draw_trace"]["operation_count"] += 1
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        load_cassette(path)
    assert caught.value.code == "E_CASSETTE_MISMATCH"
    assert "operation count" in caught.value.message


def test_legacy_cassette_without_mode_fields_still_loads_strict(demo_paths):
    cassette = load_cassette(demo_paths["replay_cassette"])
    assert all(item.frame.input_mode == DEFAULT_INPUT_MODE for item in cassette.frames)
    # Legacy frames are pure snapshots: both digests coincide.
    assert all(item.frame.sha256 == item.frame.visible_sha256 for item in cassette.frames)


# --------------------------------------------------------------------------- #
# 6. Live session wiring under bounded_draw
# --------------------------------------------------------------------------- #


def test_terminal_session_accepts_bounded_mode(tmp_path, demo_paths):
    from termharvest.mockhost import MockTerminalServer

    server = MockTerminalServer(demo_paths["demo"], port=0, frame_timeout=2.0)
    server.start()
    try:
        from termharvest.config import load_session

        session = replace(load_session(REPO_ROOT / "contracts/sessions/demo_session.yaml"), target_port=server.actual_port)
        runner = TerminalSession(session, input_mode="bounded_draw")
        frames = runner.collect()
        assert all(item.frame.input_mode == "bounded_draw" for item in frames)
        assert all(item.frame.visible_sha256 for item in frames)
    finally:
        server.stop()


def test_terminal_session_rejects_unknown_input_mode(demo_session):
    with pytest.raises(HarvestError) as caught:
        TerminalSession(demo_session, input_mode="vt220")
    assert caught.value.code == "E_TARGET_NOT_ALLOWED"
    assert "vt220" in json.dumps(caught.value.context)
