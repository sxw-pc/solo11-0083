from __future__ import annotations

import json
from pathlib import Path

import pytest

from termharvest.cassette import load_cassette, write_cassette
from termharvest.cli import main as cli_main
from termharvest.clock import FixedClock
from termharvest.config import load_session, load_screens_dir
from termharvest.counterexample import generate_counterexample
from termharvest.errors import (
    E_COUNTEREXAMPLE_BASELINE,
    E_COUNTEREXAMPLE_LIMIT,
    E_COUNTEREXAMPLE_NO_SOLUTION,
    E_COUNTEREXAMPLE_UNSUPPORTED,
    HarvestError,
)
from termharvest.pipeline import run_pipeline
from termharvest.screen import ScreenFrame
from termharvest.store import Store
from termharvest.validate_config import load_validation_config

# ---------------------------------------------------------------------------
# synthetic world: one bill page with amounts 100, 0, 0 and footer total 100
# ---------------------------------------------------------------------------

def _put(rows: list[str], row: int, col: int, text: str) -> None:
    rows[row] = rows[row][:col] + text + rows[row][col + len(text) :]

def _bill_page(page_no: int, bills: list[tuple[str, str, str, str]], total: str, count: int, end_marker: bool) -> str:
    rows = [" " * 80 for _ in range(24)]
    _put(rows, 0, 1, "SYNTHETIC OPERATIONS CONSOLE / BILL LIST")
    _put(rows, 1, 1, "READ-ONLY SCREEN - FIXTURE DATA ONLY")
    _put(rows, 2, 1, f"PAGE {page_no} OF 2")
    _put(rows, 3, 1, "CUSTOMER: C00042 NORTHWIND SYNTHETIC CO")
    _put(rows, 4, 1, "BILL ID")
    _put(rows, 4, 12, "BILL DATE")
    _put(rows, 4, 25, "AMOUNT")
    _put(rows, 4, 38, "STATUS")
    for offset, (bill_id, bill_date, amount, status) in enumerate(bills):
        row = 5 + offset
        _put(rows, row, 1, bill_id)
        _put(rows, row, 12, bill_date)
        _put(rows, row, 25, amount)
        _put(rows, row, 38, status)
    if end_marker:
        _put(rows, 5 + len(bills), 1, "END OF LIST")
    _put(rows, 20, 1, "BATCH COUNT")
    _put(rows, 20, 14, ":")
    _put(rows, 20, 16, str(count))
    _put(rows, 21, 1, "BATCH TOTAL")
    _put(rows, 21, 14, ":")
    _put(rows, 21, 16, total)
    _put(rows, 22, 1, "CONTROL: PF7 = NEXT PAGE   PF3 = MAIN MENU")
    _put(rows, 23, 1, "READ-ONLY DEMO / LOOPBACK ONLY")
    return "\n".join(rows)

PAGE1_BILLS = [("B000201", "2026-02-01", "100", "OPEN"), ("B000202", "2026-02-02", "0", "PAID"), ("B000203", "2026-02-03", "0", "OPEN")]

@pytest.fixture()
def synthetic_cassette(tmp_path, demo_paths) -> Path:
    """A passing cassette whose bill page holds amounts 100, 0, 0, footer 100."""
    demo = load_cassette(demo_paths["replay_cassette"])
    page1 = ScreenFrame.parse(_bill_page(1, PAGE1_BILLS, "100", 3, False))
    page2 = ScreenFrame.parse(_bill_page(2, [], "100", 3, True))
    frames = [
        (0, "start", "main_menu", demo.frames[0].frame),
        (1, "open_customer", "demo_customer", demo.frames[1].frame),
        (2, "open_bills", "demo_bill_list", page1),
        (3, "next_page", "demo_bill_list", page2),
    ]
    path = tmp_path / "synthetic.jsonl"
    write_cassette(path, session_id="demo_session", frames=frames, captured_by="test-synthetic")
    return path

def _generate(cassette, session, screens, validation, check_id, out, *, strategy=None, max_candidates=256):
    return generate_counterexample(
        cassette_path=cassette,
        session=session,
        screens=screens,
        validation_config=validation,
        check_id=check_id,
        strategy=strategy,
        max_candidates=max_candidates,
        output_dir=out,
    )

def _replay(cassette_path, session, screens, validation, tmp_path, name="replay"):
    store = Store(tmp_path / f"{name}.sqlite")
    try:
        return run_pipeline(
            session=session,
            screens=screens,
            validation_config=validation,
            store=store,
            output_dir=tmp_path / f"{name}_report",
            clock=FixedClock("2026-07-01T00:00:00.000Z"),
            mode="replay",
            cassette_path=cassette_path,
            evidence_dir=tmp_path / f"{name}_evidence",
        )
    finally:
        store.close()

# ---------------------------------------------------------------------------
# sum_equals: the two authorized routes on the synthetic 100/0/0 page
# ---------------------------------------------------------------------------

def test_sum_equals_fact_route_keeps_details_and_bumps_footer(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    result = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out", strategy="fact")
    proof = result.proof
    assert proof["strategy"] == "fact"
    assert proof["changed_rules"] == ["bill_total_matches_detail"]
    assert proof["changed_facts"] == [{"screen_id": "demo_bill_list", "fact": "batch_total", "old": "100", "new": "101"}]
    assert proof["changed_records"] == []
    patch = proof["patches"][0]
    assert (patch["screen_id"], patch["frame_seq"], patch["row"]) == ("demo_bill_list", 2, 21)
    assert (patch["old"], patch["new"]) == ("100", "101")
    # every baseline rule passed; only the target fails afterwards
    assert set(proof["baseline"]["failed_rules"]) == set()
    assert proof["counterexample"]["failed_rules"] == ["bill_total_matches_detail"]
    flipped = [rid for rid, status in proof["counterexample"]["rule_outcomes"].items() if status != proof["baseline"]["rule_outcomes"][rid]]
    assert flipped == ["bill_total_matches_detail"]

def test_sum_equals_amount_route_bumps_first_amount_only(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    result = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out", strategy="amount")
    proof = result.proof
    assert proof["strategy"] == "amount"
    assert proof["changed_facts"] == []
    assert len(proof["changed_records"]) == 1
    changed = proof["changed_records"][0]
    assert changed["fields"] == {"amount": {"old": "100", "new": "101"}}
    patch = proof["patches"][0]
    assert (patch["field"], patch["frame_seq"], patch["row"], patch["old"], patch["new"]) == ("amount", 2, 5, "100", "101")
    # footer fact stays 100 on the mutated cassette
    cassette = load_cassette(result.cassette_path)
    assert "BATCH TOTAL  : 100" in cassette.frames[2].frame.text
    assert "BATCH TOTAL  : 101" not in cassette.frames[2].frame.text

def test_sum_routes_produce_distinct_cassettes_records_and_reports(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    fact = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "fact", strategy="fact")
    amount = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "amount", strategy="amount")
    fact_bytes = Path(fact.cassette_path).read_bytes()
    amount_bytes = Path(amount.cassette_path).read_bytes()
    assert fact_bytes != amount_bytes
    fact_frames = load_cassette(fact.cassette_path).frames
    amount_frames = load_cassette(amount.cassette_path).frames
    assert [item.frame.sha256 for item in fact_frames] != [item.frame.sha256 for item in amount_frames]
    fact_run = _replay(fact.cassette_path, demo_session, demo_screens, demo_validation, tmp_path, "fact_replay")
    amount_run = _replay(amount.cassette_path, demo_session, demo_screens, demo_validation, tmp_path, "amount_replay")
    # both trigger exactly the same single check
    for run in (fact_run, amount_run):
        assert run["summary"]["status"] == "failed"
        assert [error["rule_id"] for error in run["payload"]["errors"]] == ["bill_total_matches_detail"]
    # but records and reports differ
    assert fact_run["payload"]["records"] != amount_run["payload"]["records"]
    assert fact_run["payload"]["captured_frames"] != amount_run["payload"]["captured_frames"]
    amount_values = [r["normalized_values"].get("amount") for r in amount_run["payload"]["records"] if r["screen_id"] == "demo_bill_list"]
    fact_values = [r["normalized_values"].get("amount") for r in fact_run["payload"]["records"] if r["screen_id"] == "demo_bill_list"]
    assert amount_values == ["101", "0", "0"]
    assert fact_values == ["100", "0", "0"]

def test_sum_auto_strategy_picks_fact_first(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    result = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out")
    assert result.strategy == "fact"

# ---------------------------------------------------------------------------
# field rules and record checks on the demo cassette
# ---------------------------------------------------------------------------

def test_field_required_route_blanks_value(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_customer.customer_name", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert (patch["field"], patch["old"], patch["new"]) == ("customer_name", "NORTHWIND SYNTHETIC CO", "")
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_FIELD_REQUIRED", "field:demo_customer.customer_name")]

def test_field_range_route_goes_below_minimum(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_customer.balance", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert (patch["old"], patch["new"]) == ("1250.75", "-1.00")
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_FIELD_RANGE", "field:demo_customer.balance")]

def test_field_type_route_writes_unparseable_value(demo_session, demo_screens, demo_paths, tmp_path):
    validators = tmp_path / "type_only.yaml"
    validators.write_text("version: 1\nfields:\n  demo_customer:\n    balance: {type: decimal}\n", encoding="utf-8")
    validation = load_validation_config(validators)
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, validation, "field:demo_customer.balance", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert patch["new"] == "INVALID"
    run = _replay(result.cassette_path, demo_session, demo_screens, validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_FIELD_TYPE", "field:demo_customer.balance")]

def test_record_check_numeric_route(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "balance_within_credit_limit", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert (patch["field"], patch["old"], patch["new"]) == ("balance", "1250.75", "5000.01")
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_RECORD_CONSISTENCY", "balance_within_credit_limit")]

def test_record_check_date_route(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "period_starts_before_end", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert (patch["field"], patch["old"], patch["new"]) == ("period_start", "2026-01-01", "2026-02-01")
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_RECORD_CONSISTENCY", "period_starts_before_end")]

def test_count_equals_route_bumps_footer_count(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    result = _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "bill_count_matches_footer", tmp_path / "out")
    patch = result.proof["patches"][0]
    assert (patch["field"], patch["old"], patch["new"]) == ("batch_count", "5", "6")
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert [(e["code"], e["rule_id"]) for e in run["payload"]["errors"]] == [("E_BATCH_COUNT_MISMATCH", "bill_count_matches_footer")]

# ---------------------------------------------------------------------------
# overlapping locators and sensitive fields
# ---------------------------------------------------------------------------

def _write_overlap_world(tmp_path: Path) -> dict[str, Path]:
    screens_dir = tmp_path / "screens"
    screens_dir.mkdir()
    (screens_dir / "duo.yaml").write_text(
        "version: 1\n"
        "screen_id: duo\n"
        "match: [DUO SCREEN]\n"
        "primary_key: [ident]\n"
        "fields:\n"
        "  - {name: ident, locator: {kind: absolute, row: 3, col: 1, length: 8}}\n"
        "  - {name: alpha, locator: {kind: absolute, row: 5, col: 10, length: 20}}\n"
        "  - {name: beta, locator: {kind: absolute, row: 5, col: 15, length: 20}}\n",
        encoding="utf-8",
    )
    (tmp_path / "session.yaml").write_text(
        "session_id: overlap_session\n"
        "target: {host: 127.0.0.1, port: 19771}\n"
        "start: {screen_id: main_menu, contains: [MAIN MENU]}\n"
        "steps:\n"
        "  - {id: go, key: ENTER, expect: {screen_id: duo, contains: [DUO SCREEN]}}\n",
        encoding="utf-8",
    )
    (tmp_path / "validators.yaml").write_text("version: 1\nfields:\n  duo:\n    alpha: {required: true, type: string}\n", encoding="utf-8")
    menu = [" " * 80 for _ in range(24)]
    _put(menu, 5, 1, "MAIN MENU")
    duo = [" " * 80 for _ in range(24)]
    _put(duo, 0, 1, "DUO SCREEN")
    _put(duo, 3, 1, "ID0001")
    _put(duo, 5, 10, "AAAAAAAAAA")
    _put(duo, 5, 15, "BBBBB")
    cassette = tmp_path / "overlap.jsonl"
    write_cassette(
        cassette,
        session_id="overlap_session",
        frames=[(0, "start", "main_menu", ScreenFrame.from_rows(menu)), (1, "go", "duo", ScreenFrame.from_rows(duo))],
        captured_by="test-overlap",
    )
    return {"screens": screens_dir, "session": tmp_path / "session.yaml", "validators": tmp_path / "validators.yaml", "cassette": cassette}

def test_overlapping_locator_is_refused_without_output(tmp_path):
    world = _write_overlap_world(tmp_path)
    session = load_session(world["session"])
    screens = load_screens_dir(world["screens"])
    validation = load_validation_config(world["validators"])
    out = tmp_path / "out"
    with pytest.raises(HarvestError) as caught:
        _generate(world["cassette"], session, screens, validation, "field:duo.alpha", out)
    assert caught.value.code == E_COUNTEREXAMPLE_NO_SOLUTION
    reasons = caught.value.context["reasons"]
    assert any(reason.startswith("locator_overlap:beta") for reason in reasons)
    assert not out.exists()

def test_sensitive_field_target_is_refused(demo_session, demo_screens, demo_paths, tmp_path):
    validators = tmp_path / "sensitive.yaml"
    validators.write_text("version: 1\nfields:\n  demo_customer:\n    operator_token: {required: false, type: string}\n", encoding="utf-8")
    validation = load_validation_config(validators)
    with pytest.raises(HarvestError) as caught:
        _generate(demo_paths["replay_cassette"], demo_session, demo_screens, validation, "field:demo_customer.operator_token", tmp_path / "out")
    assert caught.value.code == E_COUNTEREXAMPLE_UNSUPPORTED
    assert not (tmp_path / "out").exists()

def test_proof_never_contains_sensitive_content(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    result = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out", strategy="fact")
    proof_text = Path(result.proof_path).read_text(encoding="utf-8")
    cassette_text = Path(result.cassette_path).read_text(encoding="utf-8")
    assert "SYNTH-PLACEHOLDER-NOT-A-SECRET" not in proof_text
    assert "operator_token" not in proof_text
    # the cassette itself is a full frame copy and legitimately retains the fixture token
    assert "SYNTH-PLACEHOLDER-NOT-A-SECRET" in cassette_text

# ---------------------------------------------------------------------------
# no-solution vs over-limit, and multi-rule failures never disguised
# ---------------------------------------------------------------------------

def test_primary_key_target_has_no_solution(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    with pytest.raises(HarvestError) as caught:
        _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_customer.customer_id", tmp_path / "out")
    assert caught.value.code == E_COUNTEREXAMPLE_NO_SOLUTION
    assert caught.value.context["build_reasons"] == {"primary_key": 1}

def test_amount_field_range_would_break_sum_so_no_solution(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    """Every amount edit also flips sum_equals: it must not be disguised."""
    with pytest.raises(HarvestError) as caught:
        _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_bill_list.amount", tmp_path / "out")
    assert caught.value.code == E_COUNTEREXAMPLE_NO_SOLUTION
    reasons = caught.value.context["reasons"]
    assert reasons.get("other_rules_changed:bill_total_matches_detail", 0) > 1
    assert caught.value.context["candidates_evaluated"] > 1

def test_search_limit_is_distinct_from_no_solution(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    with pytest.raises(HarvestError) as caught:
        _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_bill_list.amount", tmp_path / "out", max_candidates=1)
    assert caught.value.code == E_COUNTEREXAMPLE_LIMIT
    assert caught.value.context["candidates_evaluated"] == 1
    with pytest.raises(HarvestError) as caught_zero:
        _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, "field:demo_bill_list.amount", tmp_path / "out", max_candidates=0)
    assert caught_zero.value.code == E_COUNTEREXAMPLE_LIMIT
    assert not (tmp_path / "out").exists()

# ---------------------------------------------------------------------------
# unsupported rules, baseline failures, cassette tampering
# ---------------------------------------------------------------------------

def test_unsupported_rules_are_rejected(demo_session, demo_screens, demo_validation, demo_paths, tmp_path):
    for check_id in ("unique_bills", "no_duplicate_bill_records", "bill_list_not_empty", "does_not_exist", "field:demo_customer.missing"):
        with pytest.raises(HarvestError) as caught:
            _generate(demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation, check_id, tmp_path / "out")
        assert caught.value.code == E_COUNTEREXAMPLE_UNSUPPORTED, check_id

def test_failing_baseline_is_refused(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    # rewrite the cassette with a footer that already breaks sum_equals
    cassette = load_cassette(synthetic_cassette)
    broken = ScreenFrame.parse(_bill_page(1, PAGE1_BILLS, "999", 3, False))
    frames = [(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in cassette.frames]
    frames[2] = (2, "open_bills", "demo_bill_list", broken)
    bad = tmp_path / "broken.jsonl"
    write_cassette(bad, session_id=cassette.session_id, frames=frames, captured_by="test-broken")
    with pytest.raises(HarvestError) as caught:
        _generate(bad, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out")
    assert caught.value.code == E_COUNTEREXAMPLE_BASELINE
    assert caught.value.context["target_already_failing"] is True
    assert "bill_total_matches_detail" in caught.value.context["failed_rules"]

def test_tampered_frame_hash_is_rejected(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    lines = synthetic_cassette.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if "BATCH TOTAL" in line)
    record = json.loads(lines[index])
    record["text"] = record["text"].replace("BATCH TOTAL", "BATCH TOTXL")
    lines[index] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        _generate(tampered, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out")
    assert caught.value.code == "E_CASSETTE_MISMATCH"

def test_tampered_trailer_is_rejected(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    lines = synthetic_cassette.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps({"frame_count": 99, "record_type": "trailer"}, sort_keys=True)
    tampered = tmp_path / "tampered_trailer.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(HarvestError) as caught:
        _generate(tampered, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out")
    assert caught.value.code == "E_CASSETTE_INCOMPLETE"

def test_tampered_screen_with_fresh_hash_is_rejected(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    cassette = load_cassette(synthetic_cassette)
    broken = ScreenFrame.parse(cassette.frames[0].frame.text.replace("MAIN MENU", "MAIM MENU"))
    frames = [(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in cassette.frames]
    frames[0] = (0, "start", "main_menu", broken)
    tampered = tmp_path / "tampered_screen.jsonl"
    write_cassette(tampered, session_id=cassette.session_id, frames=frames, captured_by="test-tamper")
    with pytest.raises(HarvestError) as caught:
        _generate(tampered, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out")
    assert caught.value.code == "E_SCREEN_UNEXPECTED"

# ---------------------------------------------------------------------------
# replay acceptance, determinism, CLI
# ---------------------------------------------------------------------------

def test_generated_cassette_passes_real_replay_acceptance(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    result = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "out", strategy="amount")
    # cassette integrity: hashes, sequence and trailer all verify
    cassette = load_cassette(result.cassette_path)
    assert [item.frame_seq for item in cassette.frames] == [0, 1, 2, 3]
    assert cassette.session_id == "demo_session"
    run = _replay(result.cassette_path, demo_session, demo_screens, demo_validation, tmp_path)
    assert run["summary"]["status"] == "failed"
    assert run["summary"]["error_codes"] == ["E_BATCH_SUM_MISMATCH"]
    assert [error["rule_id"] for error in run["payload"]["errors"]] == ["bill_total_matches_detail"]
    assert (tmp_path / "replay_report" / "report.json").exists()

def test_regeneration_is_byte_stable(synthetic_cassette, demo_session, demo_screens, demo_validation, tmp_path):
    first = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "a", strategy="amount")
    second = _generate(synthetic_cassette, demo_session, demo_screens, demo_validation, "bill_total_matches_detail", tmp_path / "b", strategy="amount")
    assert Path(first.cassette_path).read_bytes() == Path(second.cassette_path).read_bytes()
    assert Path(first.proof_path).read_bytes() == Path(second.proof_path).read_bytes()

def test_cli_counterexample_success_and_refusal(synthetic_cassette, repo_root, tmp_path, capsys):
    out = tmp_path / "cli_out"
    code = cli_main(
        [
            "counterexample",
            "--session", str(repo_root / "contracts/sessions/demo_session.yaml"),
            "--screens", str(repo_root / "contracts/screens"),
            "--validators", str(repo_root / "contracts/validators/demo_rules.yaml"),
            "--cassette", str(synthetic_cassette),
            "--check-id", "bill_total_matches_detail",
            "--strategy", "fact",
            "--out", str(out),
        ]
    )
    assert code == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("COUNTEREXAMPLE_RESULT ")
    summary = json.loads(line[len("COUNTEREXAMPLE_RESULT "):])
    assert summary["status"] == "ok"
    assert summary["strategy"] == "fact"
    assert Path(summary["cassette"]).is_absolute() and Path(summary["cassette"]).exists()
    assert Path(summary["proof"]).exists()
    assert (out / "counterexample.jsonl").exists()
    assert (out / "proof.json").exists()

    refusal = cli_main(
        [
            "counterexample",
            "--session", str(repo_root / "contracts/sessions/demo_session.yaml"),
            "--screens", str(repo_root / "contracts/screens"),
            "--validators", str(repo_root / "contracts/validators/demo_rules.yaml"),
            "--cassette", str(synthetic_cassette),
            "--check-id", "unique_bills",
            "--out", str(tmp_path / "cli_refused"),
        ]
    )
    assert refusal == 1
    err = capsys.readouterr().err
    assert "E_COUNTEREXAMPLE_UNSUPPORTED" in err
