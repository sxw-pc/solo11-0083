"""Offline screen-contract satisfiability precheck: solver-level coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from termharvest.config import load_screen, load_session
from termharvest.contract_sat import (
    PURPOSE_DEPLOYMENT,
    PURPOSE_REGRESSION,
    SAT,
    SENSITIVE_PLACEHOLDER,
    UNKNOWN,
    UNSAT,
    SatBudget,
    check_regression_adaptation,
    solve_screen_contract,
)
from termharvest.extract import extract_screen, validate_extraction
from termharvest.mock_scenario import load_scenario
from termharvest.screen import ScreenFrame
from termharvest.validate_config import load_validation_config


def write_screen(tmp_path: Path, body: str, name: str = "screen.yaml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return load_screen(path)


def write_rules(tmp_path: Path, body: str, name: str = "rules.yaml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return load_validation_config(path)


def write_session(tmp_path: Path, body: str, name: str = "session.yaml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return load_session(path)


def write_scenario(tmp_path: Path, screens: dict[str, list[str]], transitions: dict[str, dict[str, str]] | None = None, name: str = "scenario.json"):
    payload = {
        "scenario_id": "sat_scenario",
        "start_screen": next(iter(screens)),
        "screens": {screen_id: {"rows": rows, "transitions": (transitions or {}).get(screen_id, {})} for screen_id, rows in screens.items()},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_scenario(path)


def blank_rows() -> list[str]:
    return [" " * 80 for _ in range(24)]


def place(rows: list[str], row: int, col: int, text: str) -> list[str]:
    line = rows[row]
    rows[row] = line[:col] + text + line[col + len(text) :]
    return rows


# ---------------------------------------------------------------------------
# Shared character slots


def test_shared_character_slot_conflict_is_unsat(tmp_path):
    """The headline example: two legal locators, two legal enums, one impossible shared slot."""
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: flag_a, locator: {kind: absolute, row: 3, col: 10, length: 2}}
  - {name: flag_b, locator: {kind: absolute, row: 3, col: 11, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    flag_a: {required: true, type: string, enum: [AB]}
    flag_b: {required: true, type: string, enum: [CD]}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNSAT
    conflict = result.conflicts[0]
    assert conflict["type"] == "slot_conflict"
    assert (conflict["row"], conflict["col"]) == (3, 11)
    assert conflict["chars"] == {"flag_a": ["B"], "flag_b": ["C"]}
    assert result.minimal_conflict == ["field:s1.flag_a", "field:s1.flag_b", "slot:3:11"]
    assert result.witness_rows is None


def test_shared_slot_with_compatible_values_is_sat_and_roundtrips(tmp_path):
    """Shared slots are only a conflict when the forced characters disagree."""
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: flag_a, locator: {kind: absolute, row: 3, col: 10, length: 2}}
  - {name: flag_b, locator: {kind: absolute, row: 3, col: 11, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    flag_a: {required: true, type: string, enum: [AB]}
    flag_b: {required: true, type: string, enum: [BC]}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == SAT
    assert result.witness_rows[3][10:13] == "ABC"
    # The witness must survive the real extraction and validation code.
    frame = ScreenFrame.from_rows(result.witness_rows)
    extraction = extract_screen(frame, contract, 1)
    assert extraction.records[0]["normalized_values"] == {"flag_a": "AB", "flag_b": "BC"}
    assert validate_extraction(extraction, rules).ok


def test_normalization_folding_is_part_of_the_slot_model(tmp_path):
    """upper folds raw case variants into one value; the slot model must see both."""
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: flag_a, locator: {kind: absolute, row: 3, col: 10, length: 2}, normalize: [trim, upper]}
  - {name: flag_b, locator: {kind: absolute, row: 3, col: 11, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    flag_a: {required: true, type: string, enum: [AB]}
    flag_b: {required: true, type: string, enum: [CD]}
""",
    )
    conflicted = solve_screen_contract(contract, rules)
    assert conflicted.verdict == UNSAT
    assert conflicted.conflicts[0]["chars"]["flag_a"] == ["B", "b"]

    compatible_rules = write_rules(
        tmp_path,
        """fields:
  s1:
    flag_a: {required: true, type: string, enum: [AB]}
    flag_b: {required: true, type: string, enum: [bC]}
""",
        name="rules2.yaml",
    )
    compatible = solve_screen_contract(contract, compatible_rules)
    assert compatible.verdict == SAT
    assert compatible.witness_rows[3][10:13].upper() == "ABC"


# ---------------------------------------------------------------------------
# Jointly unsatisfiable rule sets


def test_individually_satisfiable_rules_can_be_jointly_unsat(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: left_code, locator: {kind: absolute, row: 3, col: 0, length: 4}}
  - {name: right_code, locator: {kind: absolute, row: 4, col: 0, length: 4}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    left_code: {required: true, type: string, enum: [AA]}
    right_code: {required: true, type: string, enum: [BB]}
record_checks:
  - {id: codes_must_match, screen_id: s1, left: left_code, op: eq, right_field: right_code}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNSAT
    assert result.conflicts[0]["type"] == "joint_constraints"
    # Every single rule is satisfiable on its own; only the combination fails.
    assert sorted(result.minimal_conflict) == ["field:s1.left_code", "field:s1.right_code", "record_check:codes_must_match"]


def test_cross_field_comparison_drives_the_search(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: low, locator: {kind: absolute, row: 3, col: 0, length: 2}}
  - {name: high, locator: {kind: absolute, row: 4, col: 0, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    low: {required: true, type: integer, min: 1, max: 3}
    high: {required: true, type: integer, min: 1, max: 3}
record_checks:
  - {id: ordering, screen_id: s1, left: low, op: lt, right_field: high}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == SAT
    values = result.witness_values
    assert int(values["low"]) < int(values["high"])

    impossible = write_rules(
        tmp_path,
        """fields:
  s1:
    low: {required: true, type: integer, min: 3, max: 3}
    high: {required: true, type: integer, min: 1, max: 2}
record_checks:
  - {id: ordering, screen_id: s1, left: low, op: lt, right_field: high}
""",
        name="rules2.yaml",
    )
    refused = solve_screen_contract(contract, impossible)
    assert refused.verdict == UNSAT
    assert "record_check:ordering" in refused.minimal_conflict


def test_empty_domain_is_unsat(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: code, locator: {kind: absolute, row: 3, col: 0, length: 4}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    code: {required: true, type: string, enum: [ABC], pattern: "^[0-9]{2}$"}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNSAT
    assert result.conflicts[0]["type"] == "empty_domain"
    assert result.conflicts[0]["field"] == "code"


# ---------------------------------------------------------------------------
# Label placement with multiple candidate rows


def test_label_with_multiple_candidate_rows_is_not_guessed_locally(tmp_path):
    """Blockers on the first search rows must push the label to a later row."""
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: blocker_3, locator: {kind: absolute, row: 3, col: 0, length: 80}}
  - {name: blocker_4, locator: {kind: absolute, row: 4, col: 0, length: 80}}
  - {name: payload, locator: {kind: label, label: PAYLOAD, search_rows: [3, 5], direction: right, length: 4}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    blocker_3: {required: true, type: string, enum: [%s]}
    blocker_4: {required: true, type: string, enum: [%s]}
    payload: {required: true, type: string, enum: [V1]}
""" % ("A" * 80, "B" * 80),
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == SAT
    assert result.witness_rows[3] == "A" * 80
    assert result.witness_rows[4] == "B" * 80
    assert result.witness_rows[5].startswith("PAYLOAD V1")
    frame = ScreenFrame.from_rows(result.witness_rows)
    extraction = extract_screen(frame, contract, 1)
    assert extraction.records[0]["normalized_values"]["payload"] == "V1"


# ---------------------------------------------------------------------------
# Sensitive fields


def test_sensitive_field_uses_position_and_placeholder_only(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: public_id, locator: {kind: absolute, row: 3, col: 0, length: 4}}
  - {name: secret_note, sensitive: true, locator: {kind: absolute, row: 5, col: 0, length: 20}}
  - {name: secret_label, sensitive: true, locator: {kind: label, label: TOKEN, search_rows: [7, 7], direction: right, length: 12}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    public_id: {required: true, type: string, enum: [P1]}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == SAT
    assert SENSITIVE_PLACEHOLDER in result.witness_rows[5]
    assert SENSITIVE_PLACEHOLDER in result.witness_rows[7]
    assert "secret_note" not in result.witness_values
    assert "secret_label" not in result.witness_values
    frame = ScreenFrame.from_rows(result.witness_rows)
    extraction = extract_screen(frame, contract, 1)
    assert extraction.sensitive_skipped == ["secret_label", "secret_note"]
    serialized = json.dumps(result.to_dict(include_witness=True))
    assert "secret" not in serialized.replace("secret_note", "").replace("secret_label", "")


# ---------------------------------------------------------------------------
# UNKNOWN paths


def test_arbitrary_regex_gives_unknown_not_fabricated_verdict(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: code, locator: {kind: absolute, row: 3, col: 0, length: 8}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    code: {required: true, type: string, pattern: "^[0-9]+[A-Z]$"}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNKNOWN
    assert any(reason.startswith("pattern_not_supported:code") for reason in result.reasons)
    assert result.witness_rows is None


def test_budget_boundary_flips_unsat_to_unknown(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fields:
  - {name: a, locator: {kind: absolute, row: 3, col: 0, length: 2}}
  - {name: b, locator: {kind: absolute, row: 4, col: 0, length: 2}}
  - {name: c, locator: {kind: absolute, row: 5, col: 0, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    a: {required: true, type: string, enum: [A1, A2, A3, A4, A5]}
    b: {required: true, type: string, enum: [B1, B2, B3, B4, B5]}
    c: {required: true, type: string, enum: [C1, C2, C3, C4, C5]}
record_checks:
  - {id: never_equal, screen_id: s1, left: a, op: ne, right_field: a}
""",
    )
    # 125 assignments, all failing: with enough budget this is a proven UNSAT.
    proven = solve_screen_contract(contract, rules)
    assert proven.verdict == UNSAT
    # The same model with a budget below the search space must not fabricate UNSAT.
    constrained = solve_screen_contract(contract, rules, budget=SatBudget(max_assignments=10))
    assert constrained.verdict == UNKNOWN
    assert any(reason == "budget_exceeded:assignments" for reason in constrained.reasons)


def test_pinned_fingerprint_is_unknown(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
fingerprint: sha256:%s
fields:
  - {name: a, locator: {kind: absolute, row: 3, col: 0, length: 2}}
""" % ("0" * 64),
    )
    rules = write_rules(tmp_path, "fields: {}\n")
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNKNOWN
    assert "fingerprint_pinned" in result.reasons


# ---------------------------------------------------------------------------
# List regions and summary fields sharing positions


def test_summary_label_avoids_list_region_rows(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
list_region:
  start_row: 3
  end_row: 6
  columns:
    item: {start: 0, end: 6}
summary_fields:
  - {name: total_info, locator: {kind: label, label: TOTAL, search_rows: [3, 7], direction: right, length: 4}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    item: {required: true, type: string, enum: [BOOK]}
batch_checks:
  - {id: not_empty, check: non_empty, screen_id: s1}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == SAT
    # The label must not land on a list row: there it would become a fake record.
    label_row = next(index for index, row in enumerate(result.witness_rows) if "TOTAL" in row)
    assert label_row == 6
    frame = ScreenFrame.from_rows(result.witness_rows)
    extraction = extract_screen(frame, contract, 1)
    assert [record["normalized_values"]["item"] for record in extraction.records] == ["BOOK"]


def test_summary_absolute_span_conflicting_with_single_row_region_is_unsat(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
list_region:
  start_row: 5
  end_row: 6
  columns:
    item: {start: 10, end: 12}
summary_fields:
  - {name: marker, locator: {kind: absolute, row: 5, col: 10, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    item: {required: true, type: string, enum: [AB]}
    marker: {required: true, type: string, enum: [CD]}
batch_checks:
  - {id: not_empty, check: non_empty, screen_id: s1}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNSAT
    conflict = result.conflicts[0]
    assert conflict["type"] == "slot_conflict"
    assert (conflict["row"], conflict["col"]) == (5, 10)
    assert set(conflict["fields"]) == {"marker", "item"}


def test_pigeonhole_on_unique_keys_is_unsat(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [S1]
list_region:
  start_row: 5
  end_row: 9
  columns:
    code: {start: 0, end: 2}
summary_fields:
  - {name: total_count, locator: {kind: absolute, row: 20, col: 0, length: 2}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    code: {required: true, type: string, enum: [AA, BB]}
    total_count: {required: true, type: string, enum: ["3"]}
batch_checks:
  - {id: unique_codes, check: unique_key, screen_id: s1, key: [code]}
  - {id: count_matches, check: count_equals, screen_id: s1, field: code, fact: total_count}
""",
    )
    result = solve_screen_contract(contract, rules)
    assert result.verdict == UNSAT
    conflict = result.conflicts[0]
    assert conflict["type"] == "pigeonhole"
    assert conflict["distinct_keys"] == 2
    assert conflict["required_records"] == 3


# ---------------------------------------------------------------------------
# Two purposes, two different answers


def _two_frame_contract(tmp_path):
    contract = write_screen(
        tmp_path,
        """screen_id: s1
match: [PROFILE]
fields:
  - {name: code, locator: {kind: label, label: CODE, search_rows: [3, 3], direction: right, length: 4}}
""",
    )
    rules = write_rules(
        tmp_path,
        """fields:
  s1:
    code: {required: true, type: string, enum: [AA, BB]}
""",
    )
    return contract, rules


def _two_frame_session(tmp_path):
    return write_session(
        tmp_path,
        """session_id: two_frame
target: {host: 127.0.0.1, port: 19771}
start: {screen_id: menu, contains: [MENU]}
steps:
  - {id: first, key: K1, expect: {screen_id: s1, contains: [PROFILE]}}
  - {id: second, key: K2, expect: {screen_id: s1, contains: [PROFILE]}}
""",
    )


def _two_frame_scenario(tmp_path, second_code: str):
    menu = place(blank_rows(), 0, 0, "MENU")
    good = place(place(blank_rows(), 0, 0, "PROFILE"), 3, 0, "CODE AA")
    bad = place(place(blank_rows(), 0, 0, "PROFILE"), 3, 0, f"CODE {second_code}")
    return write_scenario(
        tmp_path,
        {"menu": menu, "frame_good": good, "frame_bad": bad},
        transitions={"menu": {"K1": "frame_good"}, "frame_good": {"K2": "frame_bad"}},
    )


def test_deployment_and_regression_answer_different_questions(tmp_path):
    contract, rules = _two_frame_contract(tmp_path)
    session = _two_frame_session(tmp_path)
    scenario = _two_frame_scenario(tmp_path, "ZZ")

    # Deployment feasibility: one conforming synthetic screen is enough.
    deployment = solve_screen_contract(contract, rules)
    assert deployment.verdict == SAT
    assert deployment.purpose == PURPOSE_DEPLOYMENT
    assert deployment.witness_rows is not None

    # Regression adaptation: every claimed frame must conform; one does not.
    regression = check_regression_adaptation(session, {"s1": contract}, rules, scenario)
    assert len(regression) == 1
    adapted = regression[0]
    assert adapted.purpose == PURPOSE_REGRESSION
    assert adapted.verdict == UNSAT
    assert [frame["step_id"] for frame in adapted.non_adapting_frames] == ["second"]
    assert adapted.non_adapting_frames[0]["errors"][0]["code"] == "E_FIELD_ENUM"


def test_regression_adaptation_sat_when_all_frames_conform(tmp_path):
    contract, rules = _two_frame_contract(tmp_path)
    session = _two_frame_session(tmp_path)
    scenario = _two_frame_scenario(tmp_path, "BB")
    regression = check_regression_adaptation(session, {"s1": contract}, rules, scenario)
    assert regression[0].verdict == SAT
    assert regression[0].non_adapting_frames == []
    assert regression[0].stats["frames_checked"] == 2


# ---------------------------------------------------------------------------
# Repository contracts


def test_repository_demo_contracts_are_deployment_sat(demo_screens, demo_validation):
    for screen_id in sorted(demo_screens):
        result = solve_screen_contract(demo_screens[screen_id], demo_validation)
        assert result.verdict == SAT, (screen_id, result.reasons)
        frame = ScreenFrame.from_rows(result.witness_rows)
        extraction = extract_screen(frame, demo_screens[screen_id], 1)
        assert validate_extraction(extraction, demo_validation).ok


def test_repository_demo_scenario_is_regression_adapted(demo_session, demo_screens, demo_validation, demo_paths):
    scenario = load_scenario(demo_paths["demo"])
    results = check_regression_adaptation(demo_session, demo_screens, demo_validation, scenario)
    assert results
    assert all(result.verdict == SAT for result in results), [r.non_adapting_frames for r in results]
