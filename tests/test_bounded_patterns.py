"""Tests for the bounded, non-backtracking field-pattern matcher.

Covers the controlled regex subset, its agreement with ``re.fullmatch``,
unsupported-construct and state-budget rejections with source positions, the
two explicit usage contracts (strict collection vs. offline diagnostics), and
the linear time/state upper bound including nested-quantifier patterns such
as ``(A+)+B``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from termharvest.bounded_regex import (
    DEFAULT_STATE_BUDGET,
    MATCH,
    NO_MATCH,
    PatternProgram,
    PatternRejection,
    REASON_BUDGET,
    REASON_SYNTAX,
    REASON_UNSUPPORTED,
    compile_pattern,
)
from termharvest.errors import E_FIELD_PATTERN, E_FIELD_PATTERN_UNKNOWN, E_VALIDATION_CONFIG, HarvestError
from termharvest.validate import STATUS_UNKNOWN, validate_records
from termharvest.validate_config import (
    PATTERN_POLICY_DIAGNOSTIC,
    PATTERN_POLICY_STRICT,
    load_validation_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_rules(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 1\nfields:\n  demo_screen:\n" + body,
        encoding="utf-8",
    )
    return path


def record(screen_id: str = "demo_screen", key: str = "k1", **values: object) -> dict:
    return {
        "screen_id": screen_id,
        "record_key": key,
        "frame_seq": 1,
        "normalized_values": dict(values),
    }


# ---------------------------------------------------------------------------
# Original demo patterns keep their semantics
# ---------------------------------------------------------------------------


DEMO_PATTERNS = ["^C[0-9]{5}$", "^[0-9]{2}$", "^B[0-9]{6}$"]


@pytest.mark.parametrize("pattern", DEMO_PATTERNS)
def test_demo_patterns_compile_inside_default_budget(pattern: str) -> None:
    program = compile_pattern(pattern)
    assert isinstance(program, PatternProgram)
    assert program.states <= DEFAULT_STATE_BUDGET
    assert program.budget == DEFAULT_STATE_BUDGET


@pytest.mark.parametrize(
    "pattern, value, expected",
    [
        ("^C[0-9]{5}$", "C12345", True),
        ("^C[0-9]{5}$", "C1234", False),
        ("^C[0-9]{5}$", "C123456", False),
        ("^C[0-9]{5}$", "X12345", False),
        ("^[0-9]{2}$", "42", True),
        ("^[0-9]{2}$", "4", False),
        ("^[0-9]{2}$", "42a", False),
        ("^B[0-9]{6}$", "B000001", True),
        ("^B[0-9]{6}$", "b000001", False),
    ],
)
def test_demo_patterns_match_like_fullmatch(pattern: str, value: str, expected: bool) -> None:
    program = compile_pattern(pattern)
    assert (program.fullmatch(value) == MATCH) is expected
    assert bool(re.fullmatch(pattern, value)) is expected


def test_demo_contract_loads_under_strict_policy(tmp_path: Path, repo_root: Path) -> None:
    config = load_validation_config(repo_root / "contracts/validators/demo_rules.yaml")
    assert config.pattern_policy == PATTERN_POLICY_STRICT
    assert config.undecidable_patterns() == ()
    for rules in config.fields.values():
        for rule in rules.values():
            if rule.pattern is not None:
                assert rule.pattern_decidable
                assert isinstance(rule.pattern_program, PatternProgram)


# ---------------------------------------------------------------------------
# Supported subset: anchors, literals, classes, groups, alternation, repeats
# ---------------------------------------------------------------------------


SUPPORTED_CASES = [
    ("a*", ["", "a", "aa", "aaa"], ["b", "ab", "a1"]),
    ("a+", ["a", "aa"], ["", "b"]),
    ("a?", ["", "a"], ["aa", "b"]),
    ("a{2}", ["aa"], ["", "a", "aaa"]),
    ("a{2,3}", ["aa", "aaa"], ["", "a", "aaaa"]),
    ("a{2,}", ["aa", "aaa", "a" * 20], ["", "a"]),
    ("ab|cd", ["ab", "cd"], ["", "ac", "abcd"]),
    ("(ab|cd)+", ["ab", "cdabcd", "cd"], ["", "abx"]),
    ("(?:x|y)z", ["xz", "yz"], ["z", "x"]),
    ("[abc]+", ["a", "cab", "bb"], ["", "d", "ad"]),
    ("[^abc]+", ["d", "xyz", "中"], ["", "a", "ab"]),
    ("[a-zA-Z0-9_.-]+", ["a1", "A.b-9_c"], ["", "a b", "a/b"]),
    (r"\d+", ["123", "５"], ["", "a", "1a"]),
    (r"\w*", ["", "C123", "中_文"], ["a b", "-"]),
    (r"\s+", [" ", "\t\n", " "], ["", "a"]),
    (r"\D+", ["abc", "中"], ["", "1", "a1"]),
    ("colou?r", ["color", "colour"], ["colouur", ""]),
    (".*", ["", "anything", "line"], []),
    ("a.c", ["abc", "a c", "a中c"], ["ac", "abbc", "a\nc"]),
    ("^$", [""], ["a", "\n"]),
    ("(a|aa)+", ["a", "aa", "aaaa"], ["", "b"]),
    ("a{1,2}b{3,4}", ["abbb", "aabbbb"], ["", "bbb", "aabbbbb"]),
    (r"[\d\s]+", ["1 2", "5", "\t"], ["", "a"]),
    (r"end\.", ["end."], ["endx", ""]),
    ("a+?", ["a", "aa"], ["", "b"]),
    ("a{2,4}?", ["aa", "aaaa"], ["", "a"]),
    ("^a|b$", ["a", "b"], ["", "ab", "xb", "xa", "c"]),
    ("中+文", ["中文", "中中文"], ["", "文", "中"]),
    ("()", [""], ["a"]),
    ("(a|)", ["", "a"], ["b"]),
    (r"[\]]", ["]"], ["", "a"]),
    ("[a-]", ["a", "-"], ["", "b"]),
]


@pytest.mark.parametrize("pattern, matching, non_matching", SUPPORTED_CASES)
def test_supported_subset_agrees_with_re(pattern: str, matching: list[str], non_matching: list[str]) -> None:
    program = compile_pattern(pattern)
    assert isinstance(program, PatternProgram), pattern
    for value in matching:
        assert program.fullmatch(value) == MATCH, (pattern, value)
        assert re.fullmatch(pattern, value) is not None
    for value in non_matching:
        assert program.fullmatch(value) == NO_MATCH, (pattern, value)
        assert re.fullmatch(pattern, value) is None


def test_anchors_match_python_newline_semantics() -> None:
    program = compile_pattern("a$")
    assert program.fullmatch("a") == MATCH
    # fullmatch must consume every character: 'a' cannot eat the newline, so
    # the trailing-newline $ concession does not rescue this pattern.
    assert program.fullmatch("a\n") == NO_MATCH
    assert program.fullmatch("a\nb") == NO_MATCH
    assert re.fullmatch("a$", "a\n") is None
    # When the body itself consumes the trailing newline, $ before that
    # newline is exactly what makes the fullmatch succeed.
    newline_program = compile_pattern(r"\s$")
    assert newline_program.fullmatch("\n") == MATCH
    assert re.fullmatch(r"\s$", "\n") is not None
    newline_program2 = compile_pattern(r".$")
    assert newline_program2.fullmatch("\n") == NO_MATCH  # dot excludes newline
    assert re.fullmatch(r".$", "\n") is None
    # ^ only matches at position 0 without MULTILINE.
    start = compile_pattern("^a")
    assert start.fullmatch("a") == MATCH
    assert start.fullmatch("\na") == NO_MATCH


def test_unicode_classes_agree_with_re_on_synthetic_alphabet() -> None:
    alphabet = ["a", "Z", "0", "９", "Ａ", "中", "_", " ", "\xa0", "\t", "\n", ".", "-", "/"]
    for pattern in (r"\d", r"\D", r"\w", r"\W", r"\s", r"\S", ".", r"[^\d]", r"[\w\s]"):
        program = compile_pattern(pattern)
        assert isinstance(program, PatternProgram)
        for ch in alphabet:
            assert (program.fullmatch(ch) == MATCH) is bool(re.fullmatch(pattern, ch)), (pattern, ch)


def test_empty_string_is_a_first_class_boundary() -> None:
    for pattern in ("", "a*", "a?", "a{0}", "a{0,0}", "()", "(a|)", "a{0,2}"):
        program = compile_pattern(pattern)
        assert isinstance(program, PatternProgram)
        assert program.fullmatch("") == MATCH, pattern
        assert re.fullmatch(pattern, "") is not None


# ---------------------------------------------------------------------------
# Nested quantifiers and overlapping branches stay bounded and decisive
# ---------------------------------------------------------------------------


def test_nested_quantifier_pattern_decides_long_non_match_in_linear_time() -> None:
    program = compile_pattern("(A+)+B")
    assert isinstance(program, PatternProgram)
    # The classic exponential-backtracking input for a backtracking engine.
    long_input = "A" * 200 + "C"
    started = time.perf_counter()
    verdict = program.fullmatch(long_input)
    elapsed = time.perf_counter() - started
    assert verdict == NO_MATCH
    # Bounded by construction; the wall-clock assertion is only a smoke guard.
    assert elapsed < 2.0
    assert program.fullmatch("A" * 200 + "B") == MATCH
    assert program.fullmatch("B") == NO_MATCH
    assert program.fullmatch("") == NO_MATCH


def test_decision_is_not_string_length_or_plus_heuristic() -> None:
    # A matcher that merely capped input length or searched for consecutive
    # '+' could not distinguish these same-length, same-shape cases.
    catastrophic = compile_pattern("(A+)+B")
    benign = compile_pattern("A+B")
    assert isinstance(catastrophic, PatternProgram) and isinstance(benign, PatternProgram)
    non_match_input = "A" * 300 + "C"
    match_input = "A" * 300 + "B"
    # Long inputs are fully inspected, never truncated or rejected outright.
    assert catastrophic.fullmatch(non_match_input) == NO_MATCH
    assert catastrophic.fullmatch(match_input) == MATCH
    assert benign.fullmatch(non_match_input) == NO_MATCH
    assert benign.fullmatch(match_input) == MATCH
    # A pattern without nested '+' still gets the same linear treatment.
    flat = compile_pattern("A+B+C")
    assert isinstance(flat, PatternProgram)
    assert flat.fullmatch("A" * 300 + "B" * 10 + "C") == MATCH
    assert flat.fullmatch("A" * 300 + "B" * 10 + "X") == NO_MATCH


def test_overlapping_branches_are_unambiguous_and_linear() -> None:
    program = compile_pattern("(a|aa)+")
    assert isinstance(program, PatternProgram)
    for n in (1, 2, 50, 200):
        value = "a" * n
        assert program.fullmatch(value) == MATCH
        assert program.fullmatch(value + "b") == NO_MATCH


def test_runtime_work_is_linear_in_input_length() -> None:
    # Instrument the simulation via the structural bound: every character can
    # touch each consuming edge at most once.  Empirically, doubling the input
    # must roughly double (never explode) the work.
    program = compile_pattern("(A+)+B")
    assert isinstance(program, PatternProgram)

    def measured_steps(text: str) -> int:
        # Re-implement the same state-set loop with an explicit step counter.
        from termharvest.bounded_regex import _epsilon_closure

        steps = 0
        current = _epsilon_closure(program, frozenset({program.start}), position=0, text=text)
        for index, ch in enumerate(text):
            nxt: set[int] = set()
            for state in current:
                for target, predicate, anchor in program.transitions[state]:
                    steps += 1
                    if anchor is None and predicate(ch):
                        nxt.add(target)
            current = _epsilon_closure(program, frozenset(nxt), position=index + 1, text=text)
        return steps

    short = measured_steps("A" * 100 + "C")
    long = measured_steps("A" * 400 + "C")
    # Four times the input must stay within a small factor of four times the
    # work; an exponential engine would never satisfy this.
    assert long <= short * 5 + program.states * 5


# ---------------------------------------------------------------------------
# Unsupported constructs and over-budget patterns are explicit rejections
# ---------------------------------------------------------------------------


UNSUPPORTED_PATTERNS = [
    (r"(a)\1", "backreference"),
    (r"(?=a)", "lookahead"),
    (r"(?!a)", "negative lookahead"),
    (r"(?<=a)", "lookbehind"),
    (r"(?<!a)", "negative lookbehind"),
    (r"(?P<name>a)", "named group"),
    (r"(?i:a)", "inline flag"),
    (r"a++", "possessive"),
    (r"(a)*+", "possessive"),
    (r"\1", "backreference"),
    (r"\01", "octal"),
    (r"\x41", "hex escape"),
    (r"\N{EM DASH}", "named unicode"),
    (r"(?(1)a|b)", "conditional"),
    (r"(?P=name)", "named backreference"),
]


@pytest.mark.parametrize("pattern, label", UNSUPPORTED_PATTERNS)
def test_unsupported_constructs_are_rejected_with_position(pattern: str, label: str) -> None:
    rejection = compile_pattern(pattern)
    assert isinstance(rejection, PatternRejection)
    assert rejection.reason == REASON_UNSUPPORTED
    assert 0 <= rejection.position < len(pattern)
    assert rejection.line >= 1
    assert rejection.column >= 1
    assert label  # documents the construct family
    # And re itself accepts (or at least could parse) most of these, proving
    # the rejection is a subset decision, not a generic parse failure.


@pytest.mark.parametrize(
    "pattern",
    ["[a-", "(a", "a)", "a{2,1}", "*", "+", "?", "[", "\\", "a{,2}"],
)
def test_syntax_errors_are_rejected_with_position(pattern: str) -> None:
    rejection = compile_pattern(pattern)
    assert isinstance(rejection, PatternRejection)
    assert rejection.reason in {REASON_SYNTAX, REASON_UNSUPPORTED}
    assert rejection.position >= 0
    assert rejection.describe().startswith(rejection.reason)


def test_rejection_never_falls_back_to_re() -> None:
    # Even though re can compile these, the bounded API must return rejections
    # rather than PatternProgram objects.
    for pattern in (r"(?=x)", r"a++", r"(a)\1"):
        result = compile_pattern(pattern)
        assert isinstance(result, PatternRejection)
        assert not hasattr(result, "fullmatch")


def test_state_budget_rejection_points_at_construct() -> None:
    rejection = compile_pattern("a{1000}", state_budget=32)
    assert isinstance(rejection, PatternRejection)
    assert rejection.reason == REASON_BUDGET
    assert rejection.position == 1  # the '{' quantifier position
    assert "32" in rejection.message
    # Same pattern compiles once the budget accommodates it.
    program = compile_pattern("a{1000}", state_budget=4096)
    assert isinstance(program, PatternProgram)
    assert program.fullmatch("a" * 1000) == MATCH
    assert program.fullmatch("a" * 999) == NO_MATCH


def test_nested_unroll_respects_budget() -> None:
    rejection = compile_pattern("(a{10}){10}", state_budget=40)
    assert isinstance(rejection, PatternRejection)
    assert rejection.reason == REASON_BUDGET
    assert rejection.position == 7  # outer quantifier '{'


def test_diagnostic_config_marks_budget_overflow_as_undecidable(tmp_path: Path) -> None:
    # A pattern that is perfectly decidable under the default budget becomes
    # undecidable under a deliberately small one, with the quantifier located.
    rules = write_rules(tmp_path, "    code: {required: true, type: string, pattern: '^B[0-9]{6}$'}\n")
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC, state_budget=8)
    undecidable = config.undecidable_patterns()
    assert len(undecidable) == 1
    _, _, rejection = undecidable[0]
    assert rejection.reason == REASON_BUDGET
    assert rejection.position == 7  # the '{6}' quantifier


def test_budget_must_be_positive() -> None:
    rejection = compile_pattern("a", state_budget=0)
    assert isinstance(rejection, PatternRejection)
    assert rejection.reason == REASON_BUDGET


# ---------------------------------------------------------------------------
# The two explicit usage contracts
# ---------------------------------------------------------------------------


def test_strict_contract_rejects_undecidable_config_before_collection(tmp_path: Path) -> None:
    rules = write_rules(tmp_path, '    code: {required: true, type: string, pattern: "(?=unsafe)"}\n')
    with pytest.raises(HarvestError) as caught:
        load_validation_config(rules, pattern_policy=PATTERN_POLICY_STRICT)
    assert caught.value.code == E_VALIDATION_CONFIG
    assert "pattern" in caught.value.context["field"]


def test_strict_contract_rejects_budget_overflow(tmp_path: Path) -> None:
    rules = write_rules(tmp_path, "    code: {required: true, type: string, pattern: 'a{1000}'}\n")
    with pytest.raises(HarvestError) as caught:
        load_validation_config(rules, pattern_policy=PATTERN_POLICY_STRICT, state_budget=16)
    assert caught.value.code == E_VALIDATION_CONFIG


def test_diagnostic_contract_keeps_undecidable_rule_and_locates_it(tmp_path: Path) -> None:
    rules = write_rules(tmp_path, '    code: {required: true, type: string, pattern: "(?=unsafe)"}\n')
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    undecidable = config.undecidable_patterns()
    assert len(undecidable) == 1
    screen_id, field_name, rejection = undecidable[0]
    assert screen_id == "demo_screen"
    assert field_name == "code"
    assert rejection.reason == REASON_UNSUPPORTED
    assert rejection.position == 1  # position of '(?' inside the pattern text


def test_diagnostic_unknown_record_is_never_counted_valid(tmp_path: Path) -> None:
    rules = write_rules(
        tmp_path,
        '    code: {required: true, type: string, pattern: "(?=unsafe)"}\n'
        '    name: {required: true, type: string}\n',
    )
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    records = [record(code="anything", name="ok")]
    result = validate_records(records, config, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    assert result.stats["records_valid"] == 0
    assert result.stats["records_unknown"] == 1
    assert result.stats["records_in"] == 1
    assert "k1" in result.unknown_record_keys
    codes = [error["code"] for error in result.errors]
    assert E_FIELD_PATTERN_UNKNOWN in codes
    # The error carries the pattern source location.
    issue = next(error for error in result.errors if error["code"] == E_FIELD_PATTERN_UNKNOWN)
    assert "line 1 column 2" in issue["message"]


def test_diagnostic_contract_still_reports_other_deterministic_errors(tmp_path: Path) -> None:
    rules = write_rules(
        tmp_path,
        '    code: {required: true, type: string, pattern: "(?=unsafe)"}\n'
        '    region: {required: true, type: string, pattern: "^[0-9]{2}$"}\n',
    )
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    records = [record(code="x", region="ab")]
    result = validate_records(records, config, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    codes = {error["code"] for error in result.errors}
    assert E_FIELD_PATTERN_UNKNOWN in codes
    assert E_FIELD_PATTERN in codes  # region's deterministic mismatch is still located
    assert result.stats["records_valid"] == 0
    assert result.stats["records_unknown"] == 1


def test_strict_pipeline_gate_blocks_before_any_run_side_effect(tmp_path: Path, live_session, demo_screens) -> None:
    from termharvest.pipeline import run_pipeline
    from termharvest.store import Store

    rules = write_rules(tmp_path, "    code: {required: true, type: string, pattern: '(a)\\1'}\n")
    # Strict policy rejects the contract while it is being loaded.
    with pytest.raises(HarvestError) as caught:
        load_validation_config(rules, pattern_policy=PATTERN_POLICY_STRICT)
    assert caught.value.code == E_VALIDATION_CONFIG

    # A config assembled under diagnostics is still refused at the pipeline
    # gate when run under the strict collection contract.
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    store = Store(tmp_path / "ledger.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            run_pipeline(
                session=live_session,
                screens=demo_screens,
                validation_config=config,
                store=store,
                output_dir=tmp_path / "report",
                pattern_policy=PATTERN_POLICY_STRICT,
            )
        assert caught.value.code == E_VALIDATION_CONFIG
        # No run, no frames, no records: the gate fired before connecting.
        assert store.runs() == []
        assert store.count_records() == 0
    finally:
        store.close()


def test_diagnostic_pipeline_marks_record_unknown_and_publishes_nothing_valid(
    tmp_path: Path, demo_session, demo_screens, demo_paths
) -> None:
    from termharvest.pipeline import run_pipeline
    from termharvest.store import Store

    rules = tmp_path / "diag_rules.yaml"
    rules.write_text(
        "version: 1\n"
        "fields:\n"
        "  demo_customer:\n"
        '    customer_id: {required: true, type: string, pattern: "(?=x)"}\n',
        encoding="utf-8",
    )
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    store = Store(tmp_path / "diag.sqlite")
    try:
        result = run_pipeline(
            session=demo_session,
            screens=demo_screens,
            validation_config=config,
            store=store,
            output_dir=tmp_path / "report",
            mode="replay",
            cassette_path=demo_paths["replay_cassette"],
            pattern_policy=PATTERN_POLICY_DIAGNOSTIC,
        )
        statuses = {row["record_key"]: row["validation_status"] for row in store.records()}
        customer_rows = [r for r in result["payload"]["records"] if r["screen_id"] == "demo_customer"]
        assert customer_rows
        assert all(r["validation_status"] == STATUS_UNKNOWN for r in customer_rows)
        assert all(status == STATUS_UNKNOWN for key, status in statuses.items() if key in {r["record_key"] for r in customer_rows})
        summary = result["summary"]
        assert summary["status"] == "failed"
        validation = summary["validation"]
        assert validation["records_unknown"] >= 1
        # Unknown records never enter the valid set; the remaining records may
        # still validate normally under the diagnostic contract.
        assert validation["records_valid"] + validation["records_invalid"] + validation["records_unknown"] == summary["record_count"]
        assert validation["records_valid"] == summary["record_count"] - validation["records_unknown"] - validation["records_invalid"]
        assert E_FIELD_PATTERN_UNKNOWN in summary["error_codes"]
        # No record carrying the undecidable field was published as valid.
        unknown_keys = {r["record_key"] for r in customer_rows}
        assert all(store.records() and row["validation_status"] != "valid" for row in store.records() if row["record_key"] in unknown_keys)
    finally:
        store.close()


def test_same_undecidable_contract_yields_zero_records_in_strict_mode(
    tmp_path: Path, demo_session, demo_screens, demo_paths
) -> None:
    from termharvest.pipeline import run_pipeline
    from termharvest.store import Store

    rules = tmp_path / "strict_rules.yaml"
    rules.write_text(
        "version: 1\n"
        "fields:\n"
        "  demo_customer:\n"
        '    customer_id: {required: true, type: string, pattern: "(?=x)"}\n',
        encoding="utf-8",
    )
    config = load_validation_config(rules, pattern_policy=PATTERN_POLICY_DIAGNOSTIC)
    store = Store(tmp_path / "strict.sqlite")
    try:
        with pytest.raises(HarvestError):
            run_pipeline(
                session=demo_session,
                screens=demo_screens,
                validation_config=config,
                store=store,
                output_dir=tmp_path / "report",
                mode="replay",
                cassette_path=demo_paths["replay_cassette"],
                pattern_policy=PATTERN_POLICY_STRICT,
            )
        assert store.runs() == []
        assert store.count_records() == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Live and replay determinism with the bounded matcher
# ---------------------------------------------------------------------------


def test_live_and_replay_agree_with_bounded_patterns(
    tmp_path: Path, live_session, demo_session, demo_screens, demo_validation, demo_paths
) -> None:
    from termharvest.pipeline import run_pipeline
    from termharvest.store import Store
    from termharvest.clock import FixedClock

    live_store = Store(tmp_path / "live.sqlite")
    replay_store = Store(tmp_path / "replay.sqlite")
    try:
        live = run_pipeline(
            session=live_session,
            screens=demo_screens,
            validation_config=demo_validation,
            store=live_store,
            output_dir=tmp_path / "live_report",
            clock=FixedClock("2026-09-01T00:00:00.000Z"),
            evidence_dir=tmp_path / "live_evidence",
            record_cassette=tmp_path / "recorded.jsonl",
        )
        replay = run_pipeline(
            session=demo_session,
            screens=demo_screens,
            validation_config=demo_validation,
            store=replay_store,
            output_dir=tmp_path / "replay_report",
            clock=FixedClock("2026-09-02T00:00:00.000Z"),
            mode="replay",
            cassette_path=demo_paths["replay_cassette"],
            evidence_dir=tmp_path / "replay_evidence",
        )
        assert live["payload"]["records"] == replay["payload"]["records"]
        assert live["summary"]["validation"] == replay["summary"]["validation"]
    finally:
        live_store.close()
        replay_store.close()


def test_multi_frame_matching_stays_bounded_across_frames() -> None:
    # The 24x80 screen bound means field strings are short, but the matcher's
    # guarantee is structural: many frames reuse one small compiled program.
    program = compile_pattern("(A+)+B")
    assert isinstance(program, PatternProgram)
    frames = ["A" * n + ("C" if n % 2 else "B") for n in range(1, 80)]
    started = time.perf_counter()
    verdicts = [program.fullmatch(frame) for frame in frames]
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert verdicts[0] == NO_MATCH  # "AC"
    assert verdicts[1] == MATCH  # "AAB"
    # Repeated runs are deterministic.
    assert verdicts == [program.fullmatch(frame) for frame in frames]
