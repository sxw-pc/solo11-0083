"""Evidence-state semantics for runtime rule evaluation.

Covers the four reviewable states (proven_pass / proven_fail / unknown /
not_applicable), the minimal counterexamples from the audit (invalid amount
offset by the footer, optional missing operand), explicit applicability
conditions, truncated pagination, invalid footers, violation priority and
the strict vs investigation contract policies.
"""

from __future__ import annotations

import json

import pytest

from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.extract_pages import extract_pages
from termharvest.pipeline import _screen_completeness, run_pipeline
from termharvest.store import Store
from termharvest.validate import validate_records
from termharvest.validate_config import load_validation_config

def write_config(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return load_validation_config(path)

OPTIONAL_BALANCE_YAML = """
version: 1
fields:
  demo_customer:
    customer_id: {required: true, type: string}
    credit_limit: {required: true, type: decimal, min: 0}
    balance: {required: false, type: decimal, min: 0}
record_checks:
  - {id: balance_within_credit_limit, screen_id: demo_customer, left: balance, op: lte, right_field: credit_limit, message: balance must not exceed credit_limit}
"""

CONDITIONAL_AMOUNT_YAML = """
version: 1
fields:
  demo_bill_list:
    bill_id: {required: true, type: string}
    amount: {required: false, type: decimal, min: 0}
    status: {required: false, type: string, enum: [OPEN, PAID, VOID]}
record_checks:
  - id: amount_nonnegative_when_open
    screen_id: demo_bill_list
    left: amount
    op: gte
    right: 0
    when: {field: status, equals: OPEN}
    message: open bills must carry a non-negative amount
"""

SUM_YAML = """
version: 1
fields:
  demo_bill_list:
    bill_id: {required: true, type: string}
    amount: {required: true, type: decimal, min: 0}
batch_checks:
  - {id: bill_total_matches_detail, check: sum_equals, screen_id: demo_bill_list, field: amount, fact: batch_total, tolerance: 0.001}
"""

def customer(record_key="c" * 64, balance="10.00", credit_limit="50.00"):
    values = {"customer_id": "C00042", "credit_limit": credit_limit}
    if balance is not None:
        values["balance"] = balance
    return {
        "screen_id": "demo_customer",
        "frame_seq": 1,
        "frame_sha256": "a" * 64,
        "primary_key": {"customer_id": "C00042"},
        "raw_values": dict(values),
        "normalized_values": values,
        "record_key": record_key,
    }

def bill(record_key, amount, *, frame_seq=2, status="OPEN", bill_id="B000001"):
    values = {"bill_id": bill_id, "status": status}
    if amount is not None:
        values["amount"] = amount
    return {
        "screen_id": "demo_bill_list",
        "frame_seq": frame_seq,
        "frame_sha256": "b" * 64,
        "primary_key": {"bill_id": bill_id},
        "raw_values": dict(values),
        "normalized_values": values,
        "record_key": record_key,
    }

def evidence_for(result, rule_id):
    return [item for item in result.evidence if item["rule_id"] == rule_id]

def codes(result):
    return [error["code"] for error in result.errors]

# ---------------------------------------------------------------------------
# record_check operand states
# ---------------------------------------------------------------------------

def test_optional_missing_operand_is_unknown_not_skipped(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    record = customer(balance=None, credit_limit="50.00")
    result = validate_records([record], config)
    assert not result.errors
    entries = evidence_for(result, "balance_within_credit_limit")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "unknown"
    assert entry["record_key"] == record["record_key"]
    assert "balance" in entry["message"]
    assert entry["depends_on"] == ["balance", "credit_limit"]
    assert not result.ok

def test_missing_operand_is_not_reported_as_a_comparison_failure(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    result = validate_records([customer(balance=None)], config)
    assert "E_RECORD_CONSISTENCY" not in codes(result)
    assert "E_FIELD_REQUIRED" not in codes(result)

def test_invalid_operand_type_is_unknown(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    result = validate_records([customer(balance="not-a-decimal")], config)
    assert "E_FIELD_TYPE" in codes(result)
    entries = evidence_for(result, "balance_within_credit_limit")
    assert entries[0]["status"] == "unknown"
    assert "not a valid decimal" in entries[0]["message"]
    assert "E_RECORD_CONSISTENCY" not in codes(result)

def test_proven_violation_and_pass_both_produce_evidence(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    result = validate_records([customer(balance="60.00", credit_limit="50.00")], config)
    assert "E_RECORD_CONSISTENCY" in codes(result)
    assert evidence_for(result, "balance_within_credit_limit")[0]["status"] == "proven_fail"
    passing = validate_records([customer(balance="40.00", credit_limit="50.00")], config)
    assert passing.ok
    assert evidence_for(passing, "balance_within_credit_limit")[0]["status"] == "proven_pass"

# ---------------------------------------------------------------------------
# explicit applicability conditions (when)
# ---------------------------------------------------------------------------

def test_condition_not_satisfied_is_not_applicable(tmp_path):
    config = write_config(tmp_path, CONDITIONAL_AMOUNT_YAML)
    result = validate_records([bill("k1", None, status="VOID")], config)
    assert not result.errors
    entries = evidence_for(result, "amount_nonnegative_when_open")
    assert entries[0]["status"] == "not_applicable"
    assert result.ok
    assert result.run_status() == "success"

def test_condition_undetermined_is_unknown(tmp_path):
    config = write_config(tmp_path, CONDITIONAL_AMOUNT_YAML)
    record = bill("k1", "5.00")
    del record["normalized_values"]["status"]
    result = validate_records([record], config)
    entries = evidence_for(result, "amount_nonnegative_when_open")
    assert entries[0]["status"] == "unknown"
    assert "applicability condition undetermined" in entries[0]["message"]
    assert not result.ok

def test_condition_satisfied_then_operands_decide(tmp_path):
    config = write_config(tmp_path, CONDITIONAL_AMOUNT_YAML)
    missing = validate_records([bill("k1", None, status="OPEN")], config)
    assert evidence_for(missing, "amount_nonnegative_when_open")[0]["status"] == "unknown"
    violating = validate_records([bill("k1", "-5.00", status="OPEN")], config)
    assert evidence_for(violating, "amount_nonnegative_when_open")[0]["status"] == "proven_fail"
    assert "E_RECORD_CONSISTENCY" in codes(violating)
    passing = validate_records([bill("k1", "5.00", status="OPEN")], config)
    assert evidence_for(passing, "amount_nonnegative_when_open")[0]["status"] == "proven_pass"
    assert passing.ok

def test_when_in_list_and_mixed_records(tmp_path):
    yaml_text = CONDITIONAL_AMOUNT_YAML.replace("when: {field: status, equals: OPEN}", "when: {field: status, in: [OPEN, PAID]}")
    config = write_config(tmp_path, yaml_text)
    records = [bill("k1", "5.00", status="PAID"), bill("k2", None, status="VOID"), bill("k3", "1.00", status="OPEN")]
    result = validate_records(records, config)
    statuses = {item["record_key"]: item["status"] for item in evidence_for(result, "amount_nonnegative_when_open")}
    assert statuses == {"k1": "proven_pass", "k2": "not_applicable", "k3": "proven_pass"}
    assert result.ok

def test_when_value_that_can_never_match_is_rejected_at_load(tmp_path):
    yaml_text = CONDITIONAL_AMOUNT_YAML.replace("when: {field: status, equals: OPEN}", "when: {field: amount, equals: not-a-number}")
    with pytest.raises(HarvestError) as caught:
        write_config(tmp_path, yaml_text)
    assert caught.value.code == "E_VALIDATION_CONFIG"

def test_when_requires_exactly_one_of_equals_or_in(tmp_path):
    yaml_text = CONDITIONAL_AMOUNT_YAML.replace("when: {field: status, equals: OPEN}", "when: {field: status, equals: OPEN, in: [OPEN]}")
    with pytest.raises(HarvestError) as caught:
        write_config(tmp_path, yaml_text)
    assert caught.value.code == "E_VALIDATION_CONFIG"

def test_right_literal_that_cannot_coerce_is_rejected_at_load(tmp_path):
    yaml_text = CONDITIONAL_AMOUNT_YAML.replace("right: 0", "right: not-a-number")
    with pytest.raises(HarvestError) as caught:
        write_config(tmp_path, yaml_text)
    assert caught.value.code == "E_VALIDATION_CONFIG"
    assert "right" in caught.value.context["field"]

def test_non_finite_tolerance_is_rejected_at_load(tmp_path):
    for bad in (".inf", ".nan"):
        yaml_text = SUM_YAML.replace("tolerance: 0.001", f"tolerance: {bad}")
        with pytest.raises(HarvestError) as caught:
            write_config(tmp_path, yaml_text)
        assert caught.value.code == "E_VALIDATION_CONFIG"
        assert "tolerance" in caught.value.context["field"]

def test_uncomparable_operand_types_are_unknown_not_a_crash(tmp_path):
    yaml_text = """
version: 1
fields:
  demo_customer:
    customer_id: {required: true, type: string}
    credit_limit: {required: true, type: decimal}
record_checks:
  - {id: mixed_types, screen_id: demo_customer, left: customer_id, op: lte, right_field: credit_limit}
"""
    config = write_config(tmp_path, yaml_text)
    result = validate_records([customer()], config)
    entries = evidence_for(result, "mixed_types")
    assert entries[0]["status"] == "unknown"
    assert "not comparable" in entries[0]["message"]
    assert not result.errors

def test_satisfied_conditional_rule_lists_condition_field_in_depends_on(tmp_path):
    config = write_config(tmp_path, CONDITIONAL_AMOUNT_YAML)
    result = validate_records([bill("k1", "5.00", status="OPEN")], config)
    entry = evidence_for(result, "amount_nonnegative_when_open")[0]
    assert entry["status"] == "proven_pass"
    assert entry["depends_on"] == ["status", "amount"]

# ---------------------------------------------------------------------------
# sum_equals: footer, operands, truncation
# ---------------------------------------------------------------------------

def test_invalid_amount_offset_by_footer_is_unknown(tmp_path):
    """Minimal counterexample: 20 + unconvertible text, footer BATCH TOTAL=20."""
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20", frame_seq=2, bill_id="B000001"), bill("k2", "not-a-number", frame_seq=3, bill_id="B000002")]
    result = validate_records(records, config, facts_by_screen={"demo_bill_list": {"batch_total": "20"}})
    assert "E_BATCH_SUM_MISMATCH" not in codes(result)
    assert "E_FIELD_TYPE" in codes(result)
    entries = evidence_for(result, "bill_total_matches_detail")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "unknown"
    assert entry["operands"] == [{"record_key": "k2", "frame_seq": 3, "reason": "invalid_decimal", "value": "not-a-number"}]
    assert entry["frames"] == [2, 3]
    assert entry["fact"] == "batch_total"
    assert entry["fact_value"] == "20"
    assert not result.ok

def test_non_finite_amount_is_unknown(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20", frame_seq=2, bill_id="B000001"), bill("k2", "NaN", frame_seq=3, bill_id="B000002")]
    result = validate_records(records, config, facts_by_screen={"demo_bill_list": {"batch_total": "20"}})
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "unknown"
    assert entry["operands"][0]["reason"] == "non_finite"
    assert entry["operands"][0]["record_key"] == "k2"

def test_missing_amount_operand_is_unknown(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    record = bill("k1", None, frame_seq=2, bill_id="B000001")
    result = validate_records([record], config, facts_by_screen={"demo_bill_list": {"batch_total": "0"}})
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "unknown"
    assert entry["operands"][0]["reason"] == "missing"

def test_invalid_footer_is_unknown_not_zero(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20", frame_seq=2, bill_id="B000001")]
    result = validate_records(records, config, facts_by_screen={"demo_bill_list": {"batch_total": "not-a-number"}})
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "unknown"
    assert "not a finite decimal" in entry["message"]
    assert "E_BATCH_SUM_MISMATCH" not in codes(result)

def test_missing_footer_is_unknown(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20", frame_seq=2, bill_id="B000001")]
    result = validate_records(records, config, facts_by_screen={"demo_bill_list": {}})
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "unknown"
    assert "missing" in entry["message"]

def test_truncated_list_is_unknown_even_when_partial_sum_matches(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20", frame_seq=2, bill_id="B000001")]
    facts = {"demo_bill_list": {"batch_total": "20"}}
    truncated = {"demo_bill_list": {"complete": False, "stop_reason": "max_pages", "page_count": 3}}
    result = validate_records(records, config, facts_by_screen=facts, screen_completeness=truncated)
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "unknown"
    assert entry["stop_reason"] == "max_pages"
    assert "not proven complete" in entry["message"]
    complete = {"demo_bill_list": {"complete": True, "stop_reason": "stop_marker", "page_count": 1}}
    proven = validate_records(records, config, facts_by_screen=facts, screen_completeness=complete)
    assert evidence_for(proven, "bill_total_matches_detail")[0]["status"] == "proven_pass"

def test_complete_sum_is_proven_pass_with_frame_summary(tmp_path):
    config = write_config(tmp_path, SUM_YAML)
    records = [bill("k1", "20.00", frame_seq=2, bill_id="B000001"), bill("k2", "30.00", frame_seq=3, bill_id="B000002")]
    result = validate_records(records, config, facts_by_screen={"demo_bill_list": {"batch_total": "50.00"}})
    entry = evidence_for(result, "bill_total_matches_detail")[0]
    assert entry["status"] == "proven_pass"
    assert entry["fact"] == "batch_total"
    assert entry["total"] == "50.00"
    assert entry["expected"] == "50.00"
    assert entry["frames"] == [2, 3]
    assert result.ok

# ---------------------------------------------------------------------------
# violation priority and contract policies
# ---------------------------------------------------------------------------

def test_proven_violation_wins_over_unknown(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    records = [customer("c" * 64, balance="60.00"), customer("d" * 64, balance=None)]
    result = validate_records(records, config)
    statuses = {item["record_key"]: item["status"] for item in evidence_for(result, "balance_within_credit_limit")}
    assert statuses == {"c" * 64: "proven_fail", "d" * 64: "unknown"}
    assert result.run_status() == "failed"

def test_strict_and_investigation_contracts_diverge_on_unknown(tmp_path):
    strict = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    investigation = write_config(tmp_path, "policy: investigation\n" + OPTIONAL_BALANCE_YAML)
    records = [customer(balance=None)]
    strict_result = validate_records(records, strict)
    investigation_result = validate_records(records, investigation)
    assert strict_result.run_status() == "inconclusive"
    assert investigation_result.run_status() == "diagnostic"
    assert not strict_result.ok and not investigation_result.ok
    assert strict_result.run_status() != "success"
    assert investigation_result.run_status() != "success"
    assert [item["status"] for item in strict_result.evidence] == [item["status"] for item in investigation_result.evidence]

def test_default_policy_is_strict(tmp_path):
    config = write_config(tmp_path, OPTIONAL_BALANCE_YAML)
    assert config.policy == "strict"

def test_invalid_policy_is_rejected(tmp_path):
    with pytest.raises(HarvestError) as caught:
        write_config(tmp_path, "policy: lenient\n" + OPTIONAL_BALANCE_YAML)
    assert caught.value.code == "E_VALIDATION_CONFIG"

# ---------------------------------------------------------------------------
# pipeline integration: completeness mapping, ledger and report evidence
# ---------------------------------------------------------------------------

def test_screen_completeness_maps_stop_reasons():
    class _Extraction:
        def __init__(self, screen_id):
            self.screen_id = screen_id

    extractions = [_Extraction("demo_customer"), _Extraction("demo_bill_list")]
    page_details = [{"screen_id": "demo_bill_list", "page_count": 3, "stop_reason": "max_pages", "pages_seen": [2, 3, 4]}]
    completeness = _screen_completeness(extractions, page_details)
    assert completeness["demo_customer"] == {"complete": True, "stop_reason": "single_frame", "page_count": 1}
    assert completeness["demo_bill_list"]["complete"] is False
    assert completeness["demo_bill_list"]["stop_reason"] == "max_pages"
    page_details[0]["stop_reason"] = "stop_marker"
    assert _screen_completeness(extractions, page_details)["demo_bill_list"]["complete"] is True

def test_extract_pages_reports_max_pages_as_truncation(cassette_frames, demo_screens):
    page1 = cassette_frames["open_bills"]
    page2 = cassette_frames["next_page"]
    paged = extract_pages([(2, page1), (3, page2)], demo_screens["demo_bill_list"], max_pages=1, stop_marker="END OF LIST")
    assert paged.stop_reason == "max_pages"
    assert paged.page_count == 1

def test_pipeline_records_evidence_in_ledger_and_report(tmp_path, live_session, demo_screens, demo_validation):
    clock = FixedClock("2026-05-04T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        result = run_pipeline(
            session=live_session, screens=demo_screens, validation_config=demo_validation, store=store, output_dir=tmp_path / "report", clock=clock, evidence_dir=tmp_path / "evidence"
        )
        summary = result["summary"]
        assert summary["status"] == "success"
        assert summary["validation_policy"] == "strict"
        assert summary["evidence"] == {"proven_pass": 3, "proven_fail": 0, "unknown": 0, "not_applicable": 0}
        assert len(result["payload"]["evidence"]) == 3
        rows = store.evidence(result["run_id"])
        assert len(rows) == 3
        assert {row["status"] for row in rows} == {"proven_pass"}
        detail = json.loads(rows[0]["detail_json"])
        assert detail["rule_id"] == rows[0]["rule_id"]
        assert detail["depends_on"]
        report = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
        assert report["evidence"] == result["payload"]["evidence"]
        assert report["summary"]["evidence"]["proven_pass"] == 3
    finally:
        store.close()

def test_pipeline_evidence_is_idempotent_across_reruns(tmp_path, live_session, demo_screens, demo_validation):
    clock = FixedClock("2026-05-05T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    kwargs = dict(session=live_session, screens=demo_screens, validation_config=demo_validation, store=store, output_dir=tmp_path / "report", clock=clock, evidence_dir=tmp_path / "evidence")
    try:
        first = run_pipeline(**kwargs)
        second = run_pipeline(**kwargs)
        assert first["run_id"] == second["run_id"]
        assert len(store.evidence(first["run_id"])) == 3
    finally:
        store.close()

def test_failed_rerun_clears_stale_evidence(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    from dataclasses import replace

    from termharvest.mockhost import MockTerminalServer

    server = MockTerminalServer(demo_paths["demo"], host="127.0.0.1", port=0, frame_timeout=2.0)
    server.start()
    clock = FixedClock("2026-05-06T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        session = replace(demo_session, target_port=server.actual_port)
        kwargs = dict(session=session, screens=demo_screens, validation_config=demo_validation, store=store, output_dir=tmp_path / "report", clock=clock, evidence_dir=tmp_path / "evidence")
        first = run_pipeline(**kwargs)
        assert len(store.evidence(first["run_id"])) == 3
        server.stop()
        with pytest.raises(HarvestError):
            run_pipeline(**kwargs)
        assert store.evidence(first["run_id"]) == []
        assert store.runs()[0]["status"] == "failed"
    finally:
        store.close()
        server.stop()

def test_failure_path_report_keeps_configured_policy(tmp_path, live_session, demo_screens):
    config = write_config(tmp_path, "policy: investigation\n" + OPTIONAL_BALANCE_YAML)
    store = Store(tmp_path / "ledger.sqlite")
    try:
        with pytest.raises(HarvestError):
            run_pipeline(
                session=live_session, screens=demo_screens, validation_config=config, store=store, output_dir=tmp_path / "report", clock=FixedClock("2026-05-07T09:00:00.000Z"),
                evidence_dir=tmp_path / "evidence", host_override="192.0.2.10",
            )
        summary = json.loads((tmp_path / "report" / "report_summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "failed"
        assert summary["validation_policy"] == "investigation"
        assert summary["evidence"] == {"proven_pass": 0, "proven_fail": 0, "unknown": 0, "not_applicable": 0}
    finally:
        store.close()

@pytest.fixture()
def cassette_frames(demo_paths):
    from termharvest.cassette import load_cassette

    cassette = load_cassette(demo_paths["replay_cassette"])
    return {item.step_id: item.frame for item in cassette.frames}
